"""One-shot ECS worker for a single immutable S3 uploader v2 job.

The worker deliberately owns no listening socket and no mutable local session
state. Its temporary filesystem is disposable; S3 is the source of truth.
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
from pathlib import Path
from typing import Any

import boto3
import pyarrow as pa
import pyarrow.parquet as pq

from .config import WorkerSettings
from .job_store import JobAlreadyClaimed, S3JobStore
from .models import JobStatus
from .sanitization import encryption_key, sanitise_table


class WorkerError(RuntimeError):
    pass


def _prepared_key(settings: WorkerSettings, job_id: str) -> str:
    return f"{settings.landing_prefix}/jobs/{job_id}/prepared/input.parquet"


def _manifest_key(settings: WorkerSettings, job_id: str) -> str:
    return f"{settings.landing_prefix}/jobs/{job_id}/prepared/manifest.json"


def _iceberg_type(field: pa.Field) -> str:
    """Translate Arrow output types to the Glue ingestion manifest contract."""
    data_type = field.type
    if pa.types.is_boolean(data_type):
        return "BOOLEAN"
    if pa.types.is_integer(data_type):
        return "BIGINT"
    if pa.types.is_floating(data_type) or pa.types.is_decimal(data_type):
        return "DOUBLE"
    if pa.types.is_date(data_type):
        return "DATE"
    if pa.types.is_timestamp(data_type):
        return "TIMESTAMP"
    return "STRING"


def _write_prepared_parquet(source: Path, destination: Path, key: Any | None = None) -> tuple[pa.Schema, int, dict[str, Any]]:
    """Sanitise bounded Parquet batches; never materialise the entire file."""
    parquet = pq.ParquetFile(source)
    writer: pq.ParquetWriter | None = None
    output_schema: pa.Schema | None = None
    rows = 0
    audits: list[dict[str, Any]] = []
    active_key = key or encryption_key()
    try:
        for batch in parquet.iter_batches(batch_size=50_000):
            sanitized, audit = sanitise_table(pa.Table.from_batches([batch]), active_key)
            if writer is None:
                output_schema = sanitized.schema
                writer = pq.ParquetWriter(destination, output_schema, compression="snappy")
            elif sanitized.schema != output_schema:
                raise WorkerError("sanitisation changed the schema across Parquet batches")
            writer.write_table(sanitized)
            rows += sanitized.num_rows
            audits.append(audit)
    finally:
        if writer is not None:
            writer.close()
    if output_schema is None:
        raise WorkerError("empty uploads are not accepted")
    return output_schema, rows, {
        "dropped_columns": sorted({column for item in audits for column in item["dropped_columns"]}),
        "encrypted_columns": sorted({column for item in audits for column in item["encrypted_columns"]}),
        "postal_columns": sorted({column for item in audits for column in item["postal_columns"]}),
        "age_banded_columns": sorted({column for item in audits for column in item["age_banded_columns"]}),
        "newly_encrypted_values": sum(item["newly_encrypted_values"] for item in audits),
        "already_encrypted_values": sum(item["already_encrypted_values"] for item in audits),
    }


def process_job(job_id: str, settings: WorkerSettings, s3_client: Any | None = None, glue_client: Any | None = None) -> str:
    s3 = s3_client or boto3.client("s3", region_name=settings.region)
    glue = glue_client or boto3.client("glue", region_name=settings.region)
    store = S3JobStore(s3, settings.landing_bucket, settings.landing_prefix)
    worker_id = f"{socket.gethostname()}-{os.getpid()}"
    try:
        store.claim(job_id, worker_id)
    except JobAlreadyClaimed:
        return "duplicate"
    store.put_status(JobStatus(job_id=job_id, phase="CLAIMED", message="Worker claimed the job."))
    request = store.get_request(job_id)
    with tempfile.TemporaryDirectory(prefix="s3-uploader-v2-") as directory:
        source = Path(directory) / "source.parquet"
        prepared = Path(directory) / "prepared.parquet"
        store.put_status(JobStatus(job_id=job_id, phase="PROFILING", message="Validating and sanitising Parquet in bounded batches."))
        s3.download_file(settings.landing_bucket, request.source_key, str(source), ExtraArgs={"VersionId": request.source_version_id})
        schema, row_count, audit = _write_prepared_parquet(source, prepared)
        store.put_status(JobStatus(job_id=job_id, phase="PREPARING", message="Writing sanitised staging artifact."))
        prepared_key = _prepared_key(settings, job_id)
        s3.upload_file(str(prepared), settings.landing_bucket, prepared_key, ExtraArgs={"ServerSideEncryption": "aws:kms", "ContentType": "application/octet-stream"})
        manifest = {
            "schema_version": 1,
            "files": [f"s3://{settings.landing_bucket}/{prepared_key}"],
            "schema": [{"name": field.name, "type": _iceberg_type(field)} for field in schema],
            "prepared_contract_types": True,
            "incoming_row_count": row_count,
            "prepared_row_count": row_count,
            "deduplication_mode": "none",
            "sanitization": [audit],
        }
        manifest_key = _manifest_key(settings, job_id)
        s3.put_object(Bucket=settings.landing_bucket, Key=manifest_key, Body=json.dumps(manifest, sort_keys=True).encode(), ContentType="application/json", ServerSideEncryption="aws:kms")
        store.put_status(JobStatus(job_id=job_id, phase="STARTING_GLUE", message="Starting the S3 Tables ingestion job."))
        response = glue.start_job_run(
            JobName=settings.glue_job_name,
            Arguments={
                "--MODE": request.operation.upper(), "--MANIFEST_URI": f"s3://{settings.landing_bucket}/{manifest_key}",
                "--TABLE_BUCKET_ARN": request.destination.table_bucket_arn, "--NAMESPACE": request.destination.namespace,
                "--TABLE": request.destination.table, "--RUN_ID": job_id, "--UPLOAD_ID": job_id,
                "--UPLOADED_BY": request.owner_user_id, "--QC_PREFIX": f"s3://{settings.landing_bucket}/{settings.landing_prefix}/qc",
                "--AUDIT_PREFIX": f"s3://{settings.landing_bucket}/{settings.landing_prefix}/audit",
                "--REPORTING_MONTH": "", "--FILENAMES_JSON": "[]", "--ROLLBACK_SNAPSHOT_ID": "",
                "--ORIGINAL_UPLOADED_BY": "", "--ORIGINAL_UPLOADED_AT": "",
            },
        )
    glue_run_id = response["JobRunId"]
    store.put_status(JobStatus(job_id=job_id, phase="RUNNING_GLUE", message="Glue ingestion is running.", glue_run_id=glue_run_id))
    return glue_run_id


def main() -> None:
    job_id = os.environ["S3_UPLOADER_V2_JOB_ID"]
    process_job(job_id, WorkerSettings.from_environ())


if __name__ == "__main__":
    main()

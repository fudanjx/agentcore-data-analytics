"""One-shot ECS worker for a single immutable S3 uploader v2 job.

The worker deliberately owns no listening socket and no mutable local session
state. Its temporary filesystem is disposable; S3 is the source of truth.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import socket
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import boto3
import pyarrow as pa
import pyarrow.parquet as pq

from .config import WorkerSettings
from .contract import TARGET_COLUMNS, TIMESTAMP_TARGET_COLUMNS
from .job_store import JobAlreadyClaimed, S3JobStore
from .models import JobStatus
from .sanitization import encryption_key, sanitise_table
from .worker_analysis import profile_files, raw_key_impact_metrics


class WorkerError(RuntimeError):
    pass


def _normalise_names(names: list[str]) -> list[str]:
    """Apply v1's stable S3 Tables column-name contract without UI imports."""
    used: set[str] = set(); result: list[str] = []
    for source in names:
        base = re.sub(r"_+", "_", re.sub(r"[ /()\-]", "_", source)).strip("_").lower()
        if not base:
            raise WorkerError(f"Column name normalises to an empty value: {source!r}")
        candidate = base; index = 1
        while candidate in used:
            candidate = f"{base}_{index:02d}"; index += 1
        used.add(candidate); result.append(candidate)
    return result


def _prepared_key(settings: WorkerSettings, job_id: str) -> str:
    return f"{settings.landing_prefix}/jobs/{job_id}/prepared/input.parquet"


def _manifest_key(settings: WorkerSettings, job_id: str) -> str:
    return f"{settings.landing_prefix}/jobs/{job_id}/prepared/manifest.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _save_compat_session(store: S3JobStore, session: dict[str, Any], **changes: Any) -> dict[str, Any]:
    now = _now()
    if "phase" in changes and changes["phase"] != session.get("phase"):
        changes["phase_started_at"] = now
    session.update(changes); session["updated_at"] = now
    store.put_compat_session(session)
    return session


def _load_append_contract(s3: Any, settings: WorkerSettings, table_bucket_arn: str, namespace: str, table: str) -> dict[str, Any]:
    """Load the same durable v1 table contract used by the Glue path."""
    if table == "soc":
        return {"contract_version": 1, "schema": [{"name": column, "type": "TIMESTAMP" if column in TIMESTAMP_TARGET_COLUMNS else "BIGINT" if column == "cnt" else "STRING"} for column in TARGET_COLUMNS]}
    scope = hashlib.sha256(f"{table_bucket_arn}|{namespace}".encode()).hexdigest()[:16]
    key = f"{settings.contract_prefix}/{scope}/{table}.json"
    try:
        return json.loads(s3.get_object(Bucket=settings.contract_bucket, Key=key)["Body"].read())
    except Exception as error:
        raise WorkerError(f"No uploader schema contract is available for table {table!r}") from error


def process_compat_work(work_id: str, settings: WorkerSettings, s3_client: Any | None = None) -> str:
    """Run a profile or key-impact request in the disposable large worker."""
    action, separator, session_id = work_id.partition(":")
    if not separator or action not in {"profile", "key"} or not session_id:
        raise WorkerError("invalid compatibility worker request")
    s3 = s3_client or boto3.client("s3", region_name=settings.region)
    store = S3JobStore(s3, settings.landing_bucket, settings.landing_prefix)
    session = store.get_compat_session(session_id)
    with tempfile.TemporaryDirectory(prefix="s3-uploader-v2-review-") as directory:
        paths: list[tuple[Path, str, str]] = []
        for number, item in enumerate(session.get("files", [])):
            path = Path(directory) / f"{number:02d}-{Path(item['name']).name}"
            extra = {"VersionId": item["source_version_id"]} if item.get("source_version_id") else None
            if extra:
                s3.download_file(settings.landing_bucket, item["source_key"], str(path), ExtraArgs=extra)
            else:
                s3.download_file(settings.landing_bucket, item["source_key"], str(path))
            paths.append((path, item["name"], item["sha256"]))
        try:
            if action == "profile":
                _save_compat_session(store, session, phase="PROFILING", progress_message="Analysing file structure and proposed schema in the isolated worker.")
                contract = _load_append_contract(s3, settings, session["table_bucket_arn"], session["namespace"], session["table"]) if session["mode"] == "append" else None
                preview = profile_files(paths, session["mode"], session["table_bucket_arn"], session["namespace"], session["table"], contract)
                _save_compat_session(store, session, phase="READY_FOR_REVIEW", progress_message="Data structure analysis is complete.", preflight=preview)
                return session_id
            request = session.get("key_analysis_request") or {}
            columns = request.get("deduplication_columns") or []
            _save_compat_session(store, session, phase="KEY_ANALYSING", progress_message="Analysing the selected composite key in the isolated worker.")
            metrics = raw_key_impact_metrics([(path, name) for path, name, _ in paths], columns)
            expires_at = datetime.now(timezone.utc) + timedelta(minutes=30)
            token = uuid.uuid4().hex
            impact = {
                "metrics": metrics, "deduplication_columns": columns, "type_overrides": request.get("type_overrides", {}),
                "acknowledgement_token": token, "token": token, "expires_at": expires_at.isoformat(),
                "no_storage_or_glue_side_effects": True, "analysis_basis": "raw-s3-object-pre-sanitization",
            }
            _save_compat_session(store, session, phase="READY_FOR_ACKNOWLEDGEMENT", progress_message="Composite-key impact analysis is complete; acknowledge it before upload.", key_impact=impact, key_analysis_request=None)
            return session_id
        except Exception as error:
            _save_compat_session(store, session, phase="FAILED", progress_message="Worker review failed.", error={"code": f"{action.upper()}_ANALYSIS_FAILED", "message": str(error)})
            raise


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


def _write_prepared_parquet(
    source: Path, destination: Path, key: Any | None = None, manual_encryption_columns: list[str] | None = None,
) -> tuple[pa.Schema, int, dict[str, Any]]:
    """Sanitise bounded Parquet batches; never materialise the entire file."""
    parquet = pq.ParquetFile(source)
    writer: pq.ParquetWriter | None = None
    output_schema: pa.Schema | None = None
    rows = 0
    audits: list[dict[str, Any]] = []
    active_key = key or encryption_key()
    try:
        for batch in parquet.iter_batches(batch_size=50_000):
            sanitized, audit = sanitise_table(
                pa.Table.from_batches([batch]), active_key,
                manual_encryption_columns=manual_encryption_columns or (),
            )
            # Generic Glue ingestion receives the same stable, S3 Tables-safe
            # names used by v1's manifest contract.  Keep the transformation
            # at the bounded-batch boundary so a 300 MB upload is never held
            # in the API process or materialised as one Arrow table.
            sanitized = sanitized.rename_columns(_normalise_names(sanitized.schema.names))
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
        "manual_encryption_columns": sorted({column for item in audits for column in item.get("manual_encryption_columns", [])}),
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
        schema, row_count, audit = _write_prepared_parquet(
            source, prepared, manual_encryption_columns=request.manual_encryption_columns,
        )
        store.put_status(JobStatus(job_id=job_id, phase="PREPARING", message="Writing sanitised staging artifact."))
        prepared_key = _prepared_key(settings, job_id)
        s3.upload_file(str(prepared), settings.landing_bucket, prepared_key, ExtraArgs={"ServerSideEncryption": "aws:kms", "ContentType": "application/octet-stream"})
        manifest = {
            "manifest_version": 2,
            "files": [f"s3://{settings.landing_bucket}/{prepared_key}"],
            "schema": [{"name": field.name, "type": _iceberg_type(field)} for field in schema],
            "prepared_contract_types": True,
            "incoming_row_count": row_count,
            "prepared_row_count": row_count,
            "deduplication_mode": request.deduplication_mode,
            "deduplication_columns": request.deduplication_columns,
            "deduplication_policy": "keyed-composite-contract" if request.deduplication_mode == "keyed" else "none",
            "local_key_deduplication": False,
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
                "--REPORTING_MONTH": request.reporting_month, "--FILENAMES_JSON": json.dumps([Path(request.source_key).name]), "--ROLLBACK_SNAPSHOT_ID": "",
                "--ORIGINAL_UPLOADED_BY": "", "--ORIGINAL_UPLOADED_AT": "",
            },
        )
    glue_run_id = response["JobRunId"]
    store.put_status(JobStatus(job_id=job_id, phase="RUNNING_GLUE", message="Glue ingestion is running.", glue_run_id=glue_run_id))
    return glue_run_id


def main() -> None:
    job_id = os.environ["S3_UPLOADER_V2_JOB_ID"].strip()
    # EventBridge Pipes renders an SQS string body as a JSON string in an ECS
    # environment override (for example, '"profile:abc"').  Directly launched
    # tasks receive the raw value.  Accept both forms without changing the
    # queue message contract used by the unchanged v1 client API.
    try:
        decoded = json.loads(job_id)
    except json.JSONDecodeError:
        decoded = job_id
    if isinstance(decoded, str):
        job_id = decoded
    settings = WorkerSettings.from_environ()
    if job_id.startswith(("profile:", "key:")):
        process_compat_work(job_id, settings)
    else:
        process_job(job_id, settings)


if __name__ == "__main__":
    main()

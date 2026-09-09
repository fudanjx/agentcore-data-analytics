"""One-shot ECS worker for a single immutable S3 uploader v2 job.

The worker deliberately owns no listening socket and no mutable local session
state. Its temporary filesystem is disposable; S3 is the source of truth.
"""

from __future__ import annotations

import json
import hashlib
import os
import socket
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import boto3
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from .config import WorkerSettings
from .contract import TARGET_COLUMNS, TIMESTAMP_TARGET_COLUMNS
from .job_store import JobAlreadyClaimed, S3JobStore
from .models import JobStatus
from .sanitization import encryption_key, sanitise_table
from .ingest_contract import normalise_names, temporal_array
from .worker_analysis import profile_files, raw_key_impact_metrics, raw_key_row_selection, read_upload_table


class WorkerError(RuntimeError):
    pass


def _prepared_key(settings: WorkerSettings, job_id: str) -> str:
    return f"{settings.landing_prefix}/jobs/{job_id}/prepared/input.parquet"


def _manifest_key(settings: WorkerSettings, job_id: str) -> str:
    return f"{settings.landing_prefix}/jobs/{job_id}/prepared/manifest.json"


def _contract_key(settings: WorkerSettings, table_bucket_arn: str, namespace: str, table: str) -> str:
    scope = hashlib.sha256(f"{table_bucket_arn}|{namespace}".encode()).hexdigest()[:16]
    return f"{settings.contract_prefix}/{scope}/{table}.json"


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
    key = _contract_key(settings, table_bucket_arn, namespace, table)
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


def _arrow_contract_type(target_type: str) -> pa.DataType:
    return {
        "STRING": pa.string(), "BIGINT": pa.int64(), "DOUBLE": pa.float64(),
        "BOOLEAN": pa.bool_(), "DATE": pa.date32(), "TIMESTAMP": pa.timestamp("us"),
    }[target_type]


def _cast_contract_column(column: pa.ChunkedArray, target_type: str) -> pa.Array | pa.ChunkedArray:
    """Port of V1's typed staging conversion, before Spark sees Parquet."""
    if target_type in {"DATE", "TIMESTAMP"}:
        parsed = temporal_array(column, target_type)
        invalid_count = int(pc.count(column).as_py()) - (len(parsed) - parsed.null_count)
        if invalid_count:
            raise WorkerError(f"{target_type} conversion would discard {invalid_count} value(s)")
        return parsed
    if target_type == "BOOLEAN" and not pa.types.is_boolean(column.type):
        import polars as pl
        text = pl.from_arrow(column).cast(pl.String, strict=False).str.strip_chars().str.to_lowercase()
        return pl.DataFrame({"v": text}).select(
            pl.when(pl.col("v").is_in(["true", "1"])).then(True)
            .when(pl.col("v").is_in(["false", "0"])).then(False).otherwise(None)
        ).to_series().to_arrow()
    return pc.cast(column, _arrow_contract_type(target_type), safe=False)


def _project_to_contract(table: pa.Table, target_schema: list[dict[str, str]] | None) -> pa.Table:
    """Apply V1's reviewed, ordered table contract to a sanitised batch."""
    if not target_schema:
        return table.rename_columns(normalise_names(table.schema.names))
    table = table.rename_columns(normalise_names(table.schema.names))
    arrays = []
    for field in target_schema:
        name, target_type = field["name"], field["type"]
        if name not in table.schema.names:
            arrays.append(pa.nulls(len(table), type=_arrow_contract_type(target_type)))
        else:
            arrays.append(_cast_contract_column(table[name], target_type))
    return pa.table(arrays, names=[field["name"] for field in target_schema])


def _write_create_contract(s3: Any, settings: WorkerSettings, request: Any, target_schema: list[dict[str, str]], audit: dict[str, Any]) -> None:
    """Persist the same immutable create contract V1 requires for appends."""
    automatic = sorted(set(audit["encrypted_columns"] + audit["postal_columns"] + audit["age_banded_columns"] + audit.get("nric_encrypted_columns", [])))
    payload = {
        "contract_version": 3,
        "schema": target_schema,
        "deduplication_columns": request.deduplication_columns if request.deduplication_mode == "keyed" else [],
        "deduplication_mode": "keyed" if request.deduplication_mode == "keyed" else "unconfigured",
        "deduplication_policy": "skip-existing-key-report-conflict-v2",
        "temporal_invalid_value_policy": {"version": 1, "invalid_values": "NULL", "columns": []},
        "manual_encryption_columns": request.manual_encryption_columns,
        "automatic_sanitization_columns": automatic,
        "created_by": request.owner_user_id,
        "created_at": _now(),
    }
    s3.put_object(
        Bucket=settings.contract_bucket,
        Key=_contract_key(settings, request.destination.table_bucket_arn, request.destination.namespace, request.destination.table),
        Body=json.dumps(payload, sort_keys=True).encode(), ContentType="application/json", ServerSideEncryption="AES256",
    )


def _glue_compatible_table(table: pa.Table) -> pa.Table:
    """Emit only Parquet physical types accepted by the Glue/Spark reader.

    Glue/Spark cannot read Parquet TIME(MICROS), while the v1 preflight
    explicitly stores time-only fields as strings.  Preserve their textual
    value before staging rather than emitting an invalid physical Parquet type.
    """
    arrays, fields = [], []
    for field in table.schema:
        column = table[field.name]
        if pa.types.is_time(field.type):
            arrays.append(pc.cast(column, pa.string(), safe=False))
            fields.append(pa.field(field.name, pa.string(), nullable=True, metadata=field.metadata))
        elif pa.types.is_timestamp(field.type):
            # Spark rejects Parquet TIMESTAMP(NANOS).  The V1 S3 Tables
            # contract uses microsecond precision, so make that conversion
            # explicit before writing the staging artifact.
            arrays.append(pc.cast(column, pa.timestamp("us"), safe=False))
            fields.append(pa.field(field.name, pa.timestamp("us"), nullable=True, metadata=field.metadata))
        else:
            arrays.append(column)
            fields.append(field)
    return pa.Table.from_arrays(arrays, schema=pa.schema(fields, metadata=table.schema.metadata))


def _write_prepared_parquet(
    source: Path, destination: Path, key: Any | None = None, manual_encryption_columns: list[str] | None = None,
    filename: str | None = None, row_indices: list[int] | None = None,
    target_schema: list[dict[str, str]] | None = None, nric_columns: list[str] | None = None,
) -> tuple[pa.Schema, int, dict[str, Any]]:
    """Sanitise supported upload formats inside the disposable large worker."""
    lower = (filename or source.name).lower()
    if lower.endswith((".parquet", ".parquet.gzip")):
        batches = pq.ParquetFile(source).iter_batches(batch_size=50_000)
    else:
        # Spreadsheet and delimited uploads use the exact v1-compatible reader
        # already used during review.  This is intentionally worker-only;
        # the small API task never parses user files.
        batches = read_upload_table(source, filename or source.name).to_batches(max_chunksize=50_000)
    writer: pq.ParquetWriter | None = None
    output_schema: pa.Schema | None = None
    rows = 0
    source_row_offset = 0
    selected_rows = sorted(row_indices) if row_indices is not None else None
    selected_cursor = 0
    audits: list[dict[str, Any]] = []
    active_key = key or encryption_key()
    try:
        for batch in batches:
            if selected_rows is not None:
                batch_end = source_row_offset + batch.num_rows
                while selected_cursor < len(selected_rows) and selected_rows[selected_cursor] < source_row_offset:
                    selected_cursor += 1
                next_cursor = selected_cursor
                while next_cursor < len(selected_rows) and selected_rows[next_cursor] < batch_end:
                    next_cursor += 1
                local_indices = [index - source_row_offset for index in selected_rows[selected_cursor:next_cursor]]
                selected_cursor = next_cursor
                source_row_offset += batch.num_rows
                if not local_indices:
                    continue
                batch = batch.take(pa.array(sorted(local_indices), type=pa.int64()))
            sanitized, audit = sanitise_table(
                pa.Table.from_batches([batch]), active_key,
                manual_encryption_columns=manual_encryption_columns or (), nric_columns=nric_columns or (),
            )
            sanitized = _glue_compatible_table(_project_to_contract(sanitized, target_schema))
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
    session = store.get_compat_session(request.session_id)
    preflight = session.get("preflight") or {}
    if (preflight.get("table_bucket_arn"), preflight.get("namespace"), preflight.get("table")) != (
        request.destination.table_bucket_arn, request.destination.namespace, request.destination.table,
    ):
        raise WorkerError("Reviewed preflight destination does not match the immutable job request")
    target_schema = preflight.get("target_schema") or []
    nric_columns = list((preflight.get("files") or [{}])[0].get("nric_detected_columns") or [])
    with tempfile.TemporaryDirectory(prefix="s3-uploader-v2-") as directory:
        source = Path(directory) / "source.parquet"
        prepared = Path(directory) / "prepared.parquet"
        store.put_status(JobStatus(job_id=job_id, phase="PROFILING", message="Validating and sanitising Parquet in bounded batches."))
        s3.download_file(settings.landing_bucket, request.source_key, str(source), ExtraArgs={"VersionId": request.source_version_id})
        row_indices: list[int] | None = None
        local_deduplication_metrics: dict[str, int] = {}
        if request.deduplication_mode == "keyed":
            store.put_status(JobStatus(job_id=job_id, phase="PROFILING", message="Selecting raw keyed rows before sanitisation."))
            selections, local_deduplication_metrics = raw_key_row_selection(
                [(source, Path(request.source_key).name)], request.deduplication_columns,
            )
            row_indices = selections[0]
        schema, row_count, audit = _write_prepared_parquet(
            source, prepared, manual_encryption_columns=request.manual_encryption_columns,
            filename=Path(request.source_key).name, row_indices=row_indices,
            target_schema=target_schema, nric_columns=nric_columns,
        )
        if local_deduplication_metrics and row_count != local_deduplication_metrics["rows_retained_after_local_deduplication"]:
            raise WorkerError("Prepared row count does not match raw local de-duplication result")
        if request.operation == "create":
            _write_create_contract(s3, settings, request, target_schema, audit)
        store.put_status(JobStatus(job_id=job_id, phase="PREPARING", message="Writing sanitised staging artifact."))
        prepared_key = _prepared_key(settings, job_id)
        s3.upload_file(str(prepared), settings.landing_bucket, prepared_key, ExtraArgs={"ServerSideEncryption": "aws:kms", "ContentType": "application/octet-stream"})
        manifest = {
            "manifest_version": 2,
            "files": [f"s3://{settings.landing_bucket}/{prepared_key}"],
            "schema": [{"name": field.name, "type": _iceberg_type(field)} for field in schema],
            "prepared_contract_types": True,
            "incoming_row_count": local_deduplication_metrics.get("incoming_rows", row_count),
            "prepared_row_count": row_count,
            "deduplication_mode": request.deduplication_mode,
            "deduplication_columns": request.deduplication_columns,
            "deduplication_policy": "keyed-composite-contract" if request.deduplication_mode == "keyed" else "none",
            "local_key_deduplication": bool(local_deduplication_metrics),
            "local_deduplication_metrics": local_deduplication_metrics,
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
                "--REPORTING_MONTH": request.reporting_month or "not-applicable", "--FILENAMES_JSON": json.dumps([Path(request.source_key).name]), "--ROLLBACK_SNAPSHOT_ID": "not-applicable",
                "--ORIGINAL_UPLOADED_BY": request.owner_user_id, "--ORIGINAL_UPLOADED_AT": request.created_at.isoformat(),
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
        try:
            process_job(job_id, settings)
        except Exception as error:
            # The browser polls durable S3 state.  Never strand it in an
            # in-progress phase merely because the disposable Fargate process
            # exits before Glue starts.
            s3 = boto3.client("s3", region_name=settings.region)
            store = S3JobStore(s3, settings.landing_bucket, settings.landing_prefix)
            store.put_status(JobStatus(job_id=job_id, phase="FAILED", message="Worker preparation failed.", error_code=type(error).__name__))
            raise


if __name__ == "__main__":
    main()

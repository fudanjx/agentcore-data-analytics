"""One-shot ECS worker for a single immutable S3 uploader v2 job.

The worker deliberately owns no listening socket and no mutable local session
state. Its temporary filesystem is disposable; S3 is the source of truth.
"""

from __future__ import annotations

import json
import hashlib
import multiprocessing
import os
import queue
import shutil
import socket
import tempfile
import time
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
from .job_store import JobAlreadyClaimed, RecordStateConflict, S3JobStore
from .models import JobStatus
from .sanitization import encryption_key, sanitise_table
from .ingest_contract import normalise_names, temporal_array
from .worker_analysis import profile_files, raw_key_impact_metrics, raw_key_row_selection, read_upload_table


class WorkerError(RuntimeError):
    pass


_LEASE_POLL_SECONDS = 2
_LEASE_HEARTBEAT_SECONDS = 10
_BASE_RSS_LIMIT_BYTES = 12 * 1024 * 1024 * 1024
_BASE_STORAGE_PERCENT = 70
_HISTORY_BUCKET = "ah-data-analytics"
_HISTORY_PREFIX = "temp_s3_update/web_ingest/upload_history"


def _prepared_key(settings: WorkerSettings, job_id: str, number: int | None = None) -> str:
    name = "input.parquet" if number is None else f"input-{number:02d}.parquet"
    return f"{settings.landing_prefix}/jobs/{job_id}/prepared/{name}"


def _manifest_key(settings: WorkerSettings, job_id: str) -> str:
    return f"{settings.landing_prefix}/jobs/{job_id}/prepared/manifest.json"


def _contract_key(settings: WorkerSettings, table_bucket_arn: str, namespace: str, table: str) -> str:
    scope = hashlib.sha256(f"{table_bucket_arn}|{namespace}".encode()).hexdigest()[:16]
    return f"{settings.contract_prefix}/{scope}/{table}.json"


def _history_prefix(table_bucket_arn: str, namespace: str, table: str) -> str:
    """Return the V1 per-table, value-free audit projection prefix."""
    scope = hashlib.sha256(f"{table_bucket_arn}|{namespace}".encode()).hexdigest()[:16]
    return f"{_HISTORY_PREFIX}/{scope}/{table}/"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _save_compat_session(store: S3JobStore, session: dict[str, Any], **changes: Any) -> dict[str, Any]:
    now = _now()
    if "phase" in changes and changes["phase"] != session.get("phase"):
        changes["phase_started_at"] = now
    try:
        updated = store.update_compat_session(
            str(session["session_id"]), {**changes, "updated_at": now},
            guard=lambda current: current.get("phase") != "DELETED",
        )
    except RecordStateConflict:
        updated = store.get_compat_session(str(session["session_id"]))
    session.clear(); session.update(updated)
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


def _cached_session_paths(s3: Any, settings: WorkerSettings, session: dict[str, Any], directory: Path) -> list[tuple[Path, str, str]]:
    paths: list[tuple[Path, str, str]] = []
    for number, item in enumerate(session.get("files", [])):
        path = directory / f"{number:02d}-{Path(item['name']).name}"
        if not path.exists():
            extra = {"VersionId": item["source_version_id"]} if item.get("source_version_id") else None
            if extra:
                s3.download_file(settings.landing_bucket, item["source_key"], str(path), ExtraArgs=extra)
            else:
                s3.download_file(settings.landing_bucket, item["source_key"], str(path))
        paths.append((path, item["name"], item["sha256"]))
    return paths


def _run_compat_action(action: str, store: S3JobStore, s3: Any, settings: WorkerSettings, session: dict[str, Any], paths: list[tuple[Path, str, str]]) -> str:
    if action == "profile":
        _save_compat_session(store, session, phase="PROFILING", progress_message="Analysing file structure and proposed schema in the isolated worker.")
        contract = _load_append_contract(s3, settings, session["table_bucket_arn"], session["namespace"], session["table"]) if session["mode"] == "append" else None
        preview = profile_files(paths, session["mode"], session["table_bucket_arn"], session["namespace"], session["table"], contract)
        _save_compat_session(store, session, phase="READY_FOR_REVIEW", progress_message="Data structure analysis is complete.", preflight=preview)
        return str(session["session_id"])
    if action == "key":
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
        return str(session["session_id"])
    raise WorkerError(f"Unsupported compatibility action: {action}")


def process_compat_work(work_id: str, settings: WorkerSettings, s3_client: Any | None = None) -> str:
    """Run a profile or key-impact request in the disposable legacy worker."""
    action, separator, session_id = work_id.partition(":")
    if not separator or action not in {"profile", "key"} or not session_id:
        raise WorkerError("invalid compatibility worker request")
    s3 = s3_client or boto3.client("s3", region_name=settings.region)
    store = S3JobStore(s3, settings.landing_bucket, settings.landing_prefix)
    session = store.get_compat_session(session_id)
    with tempfile.TemporaryDirectory(prefix="s3-uploader-v2-review-") as directory:
        try:
            return _run_compat_action(action, store, s3, settings, session, _cached_session_paths(s3, settings, session, Path(directory)))
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


def _job_sources(request: Any) -> list[dict[str, Any]]:
    sources = list(request.source_files or [])
    if sources:
        return [source.model_dump() for source in sources]
    return [{
        "name": Path(request.source_key).name, "source_key": request.source_key,
        "source_version_id": request.source_version_id, "source_sha256": request.source_sha256,
        "source_size_bytes": request.source_size_bytes,
    }]


def _combined_audit(audits: list[dict[str, Any]]) -> dict[str, Any]:
    list_fields = ("dropped_columns", "encrypted_columns", "postal_columns", "age_banded_columns", "manual_encryption_columns")
    return {
        **{field: sorted({column for audit in audits for column in audit.get(field, [])}) for field in list_fields},
        "newly_encrypted_values": sum(int(audit.get("newly_encrypted_values", 0)) for audit in audits),
        "already_encrypted_values": sum(int(audit.get("already_encrypted_values", 0)) for audit in audits),
    }


def process_job(job_id: str, settings: WorkerSettings, s3_client: Any | None = None, glue_client: Any | None = None, source_overrides: list[tuple[Path, str]] | None = None) -> str:
    s3 = s3_client or boto3.client("s3", region_name=settings.region)
    # The small FIFO dispatcher starts Glue after this disposable worker has
    # prepared immutable staging artefacts. Keep the argument for callers
    # that still pass the historical dependency.
    _ = glue_client
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
    sources = _job_sources(request)
    if source_overrides is not None:
        expected_names = [source["name"] for source in sources]
        if [name for _, name in source_overrides] != expected_names:
            raise WorkerError("Cached sources do not match the immutable worker job manifest")
    with tempfile.TemporaryDirectory(prefix="s3-uploader-v2-") as directory:
        if source_overrides is None:
            local_sources: list[tuple[Path, str]] = []
            for number, source in enumerate(sources):
                path = Path(directory) / f"{number:02d}-{Path(source['name']).name}"
                s3.download_file(settings.landing_bucket, source["source_key"], str(path), ExtraArgs={"VersionId": source["source_version_id"]})
                local_sources.append((path, source["name"]))
        else:
            local_sources = source_overrides
        store.put_status(JobStatus(job_id=job_id, phase="PROFILING", message="Validating and sanitising Parquet in bounded batches."))
        local_deduplication_metrics: dict[str, int] = {}
        selections: dict[int, list[int]] = {}
        if request.deduplication_mode == "keyed":
            store.put_status(JobStatus(job_id=job_id, phase="PROFILING", message="Selecting raw keyed rows before sanitisation."))
            selections, local_deduplication_metrics = raw_key_row_selection(
                local_sources, request.deduplication_columns,
            )
        schema: pa.Schema | None = None
        row_count = 0
        audits: list[dict[str, Any]] = []
        prepared_keys: list[str] = []
        profiles = preflight.get("files") or []
        for number, (source, filename) in enumerate(local_sources):
            # A V1 raw-key conflict may correctly exclude every row from one
            # source in a multi-file submission. Skip that source; fail only
            # if the complete submission has no retained rows.
            if request.deduplication_mode == "keyed" and not selections.get(number):
                continue
            prepared = Path(directory) / f"prepared-{number:02d}.parquet"
            file_schema, file_rows, audit = _write_prepared_parquet(
                source, prepared, manual_encryption_columns=request.manual_encryption_columns,
                filename=filename, row_indices=selections.get(number), target_schema=target_schema,
                nric_columns=list((profiles[number] if number < len(profiles) else {}).get("nric_detected_columns") or []),
            )
            if schema is None:
                schema = file_schema
            elif schema != file_schema:
                raise WorkerError("Multi-file preparation produced different schemas; submit the files separately")
            row_count += file_rows
            audits.append(audit)
            key = _prepared_key(settings, job_id, number if len(local_sources) > 1 else None)
            s3.upload_file(str(prepared), settings.landing_bucket, key, ExtraArgs={"ServerSideEncryption": "aws:kms", "ContentType": "application/octet-stream"})
            prepared_keys.append(key)
        if schema is None:
            raise WorkerError("empty uploads are not accepted")
        audit = _combined_audit(audits)
        if local_deduplication_metrics and row_count != local_deduplication_metrics["rows_retained_after_local_deduplication"]:
            raise WorkerError("Prepared row count does not match raw local de-duplication result")
        if request.operation == "create":
            _write_create_contract(s3, settings, request, target_schema, audit)
        store.put_status(JobStatus(job_id=job_id, phase="PREPARING", message="Writing sanitised staging artifact."))
        manifest = {
            "manifest_version": 2,
            "files": [f"s3://{settings.landing_bucket}/{key}" for key in prepared_keys],
            "schema": [{"name": field.name, "type": _iceberg_type(field)} for field in schema],
            "prepared_contract_types": True,
            "incoming_row_count": local_deduplication_metrics.get("incoming_rows", row_count),
            "prepared_row_count": row_count,
            "deduplication_mode": request.deduplication_mode,
            "deduplication_columns": request.deduplication_columns,
            "deduplication_policy": "keyed-composite-contract" if request.deduplication_mode == "keyed" else "none",
            "local_key_deduplication": bool(local_deduplication_metrics),
            "local_deduplication_metrics": local_deduplication_metrics,
            "sanitization": audits,
        }
        manifest_key = _manifest_key(settings, job_id)
        s3.put_object(Bucket=settings.landing_bucket, Key=manifest_key, Body=json.dumps(manifest, sort_keys=True).encode(), ContentType="application/json", ServerSideEncryption="aws:kms")
    store.put_status(JobStatus(
        job_id=job_id, phase="READY_FOR_MUTATION",
        message="Sanitised staging is ready; waiting for the per-table FIFO Glue dispatcher.",
    ))
    return job_id


def _rss_bytes(pid: int) -> int:
    """Read Linux child RSS without adding a worker dependency."""
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    except (FileNotFoundError, OSError, ValueError, IndexError):
        pass
    return 0


def _lease_expired(lease: dict[str, Any]) -> bool:
    expires_at = lease.get("expires_at")
    return bool(expires_at and datetime.fromisoformat(expires_at) <= datetime.now(timezone.utc))


def _save_lease(store: S3JobStore, lease: dict[str, Any], *, state: str, message: str, **changes: Any) -> dict[str, Any]:
    try:
        updated = store.update_lease(
            str(lease["lease_id"]),
            {**changes, "state": state, "message": message, "updated_at": _now(), "heartbeat_at": _now()},
            guard=lambda current: current.get("state") != "CANCELLED",
        )
    except RecordStateConflict:
        updated = store.get_lease(str(lease["lease_id"]))
    lease.clear(); lease.update(updated)
    return lease


def _heartbeat_lease(store: S3JobStore, lease: dict[str, Any]) -> dict[str, Any]:
    """Refresh liveness without replaying a stale worker state."""
    try:
        updated = store.update_lease(
            str(lease["lease_id"]),
            {"heartbeat_at": _now(), "updated_at": _now()},
            guard=lambda current: current.get("state") != "CANCELLED",
        )
    except RecordStateConflict:
        updated = store.get_lease(str(lease["lease_id"]))
    lease.clear(); lease.update(updated)
    return lease


def _mark_resource_limit(store: S3JobStore, lease: dict[str, Any], session: dict[str, Any], phase: str, reason: str) -> None:
    _save_compat_session(
        store, session, phase="FAILED", progress_message="Base worker reached its resource safety limit.",
        error={"code": "RESOURCE_LIMIT_EXCEEDED", "message": reason},
    )
    _save_lease(store, lease, state="RESOURCE_LIMIT_EXCEEDED", message=reason, resume_phase=phase, can_retry_large=True)


def _lease_phase_entry(action: str, lease_id: str, settings: WorkerSettings, cache_directory: str, result_queue: Any) -> None:
    """Child process entry point; each phase gets a fresh Python process."""
    try:
        s3 = boto3.client("s3", region_name=settings.region)
        store = S3JobStore(s3, settings.landing_bucket, settings.landing_prefix)
        lease = store.get_lease(lease_id)
        session = store.get_compat_session(str(lease["session_id"]))
        paths = _cached_session_paths(s3, settings, session, Path(cache_directory))
        if action in {"profile", "key"}:
            _run_compat_action(action, store, s3, settings, session, paths)
            result_queue.put({"ok": True})
            return
        if action != "ingestion":
            raise WorkerError(f"Unsupported leased worker action: {action}")
        job_id = str((session.get("ingestion") or {}).get("job_id") or "")
        if not job_id:
            raise WorkerError("leased ingestion has no job id")
        process_job(job_id, settings, s3, source_overrides=[(path, name) for path, name, _ in paths])
        session = store.get_compat_session(str(lease["session_id"]))
        ingestion = {**(session.get("ingestion") or {}), "state": "READY_FOR_MUTATION", "job_run_id": None}
        _save_compat_session(store, session, phase="QUEUED", progress_message="Sanitised staging is ready; waiting for the per-table FIFO Glue dispatcher.", ingestion=ingestion)
        result_queue.put({"ok": True})
    except Exception as error:
        result_queue.put({"ok": False, "error_type": type(error).__name__, "error": str(error)})


def _stop_child(child: Any) -> None:
    if not child.is_alive():
        return
    child.terminate()
    child.join(timeout=10)
    if child.is_alive():
        child.kill()
        child.join(timeout=10)


def _run_leased_phase(action: str, lease_id: str, settings: WorkerSettings, cache_directory: Path, store: S3JobStore, lease: dict[str, Any]) -> tuple[str, str]:
    context = multiprocessing.get_context("spawn")
    results = context.Queue()
    child = context.Process(target=_lease_phase_entry, args=(action, lease_id, settings, str(cache_directory), results))
    child.start()
    last_heartbeat = time.monotonic()
    try:
        while child.is_alive():
            latest_lease = store.get_lease(lease_id)
            lease.clear(); lease.update(latest_lease)
            if lease.get("state") == "CANCELLED":
                _stop_child(child)
                return "cancelled", ""
            if lease.get("worker_size") == "BASE":
                if _rss_bytes(child.pid) >= _BASE_RSS_LIMIT_BYTES:
                    _stop_child(child)
                    return "resource_limit", "Child process reached the 12 GiB base-worker memory safety limit."
                disk = shutil.disk_usage(cache_directory)
                if disk.total and (disk.used * 100 / disk.total) >= _BASE_STORAGE_PERCENT:
                    _stop_child(child)
                    return "resource_limit", "Worker ephemeral storage reached the 70% base-worker safety limit."
            if time.monotonic() - last_heartbeat >= _LEASE_HEARTBEAT_SECONDS:
                _heartbeat_lease(store, lease)
                last_heartbeat = time.monotonic()
            time.sleep(1)
        child.join(timeout=10)
        try:
            result = results.get(timeout=2)
        except queue.Empty:
            return "resource_limit", "Worker child exited without a result."
        if result.get("ok"):
            return "completed", ""
        error = str(result.get("error", "Worker phase failed."))
        resource_words = ("memory", "allocation", "out of space", "no space")
        if lease.get("worker_size") == "BASE" and any(word in error.lower() for word in resource_words):
            return "resource_limit", error
        raise WorkerError(error)
    finally:
        _stop_child(child)


def run_leased_worker(lease_id: str, settings: WorkerSettings, s3_client: Any | None = None) -> str:
    """Keep one deterministic-size worker alive across reusable idle sessions."""
    s3 = s3_client or boto3.client("s3", region_name=settings.region)
    store = S3JobStore(s3, settings.landing_bucket, settings.landing_prefix)
    with tempfile.TemporaryDirectory(prefix=f"s3-uploader-v3-{lease_id[:8]}-") as raw_directory:
        cache_directory = Path(raw_directory)
        cached_session_id: str | None = None
        while True:
            lease = store.get_lease(lease_id)
            if lease.get("state") == "CANCELLED":
                return "cancelled"
            if _lease_expired(lease):
                session_id = str(lease.get("session_id") or "")
                if session_id:
                    try:
                        session = store.get_compat_session(session_id)
                        if session.get("phase") in {"RECEIVED", "PROFILING", "KEY_ANALYSING", "QUEUED"}:
                            _save_compat_session(
                                store, session, phase="FAILED",
                                progress_message="Worker lease expired before the current phase completed.",
                                error={"code": "WORKER_LEASE_EXPIRED", "message": "Select the files and review the upload again."},
                            )
                    except MissingRecord:
                        pass
                _save_lease(store, lease, state="EXPIRED", message="Worker lease expired.")
                return "expired"
            session_id = str(lease.get("session_id") or "")
            if session_id != cached_session_id:
                # A changed selection may keep this Fargate task alive.  Its
                # raw objects must not be reused if the next selection has the
                # same filenames as the abandoned review.
                for path in cache_directory.iterdir():
                    if path.is_dir():
                        shutil.rmtree(path)
                    else:
                        path.unlink()
                cached_session_id = session_id
            if not session_id:
                _save_lease(store, lease, state="AWAITING_UPLOAD", message="Worker is ready for the selected upload.")
                time.sleep(_LEASE_POLL_SECONDS)
                continue
            session = store.get_compat_session(str(session_id))
            phase = str(session.get("phase"))
            try:
                if phase == "RECEIVED":
                    _save_lease(store, lease, state="PROFILING", message="Analysing uploaded file structure.")
                    phase_result, reason = _run_leased_phase("profile", lease_id, settings, cache_directory, store, lease)
                    if phase_result == "cancelled":
                        return "cancelled"
                    if phase_result != "completed":
                        _mark_resource_limit(store, lease, session, "RECEIVED", reason)
                        return "resource-limit"
                    continue
                if phase == "KEY_ANALYSING":
                    _save_lease(store, lease, state="ANALYSING_KEY", message="Analysing selected composite key.")
                    phase_result, reason = _run_leased_phase("key", lease_id, settings, cache_directory, store, lease)
                    if phase_result == "cancelled":
                        return "cancelled"
                    if phase_result != "completed":
                        _mark_resource_limit(store, lease, session, "KEY_ANALYSING", reason)
                        return "resource-limit"
                    continue
                if phase == "QUEUED":
                    _save_lease(store, lease, state="PREPARING", message="Preparing sanitised staging data.")
                    phase_result, reason = _run_leased_phase("ingestion", lease_id, settings, cache_directory, store, lease)
                    if phase_result == "cancelled":
                        return "cancelled"
                    if phase_result != "completed":
                        _mark_resource_limit(store, lease, session, "QUEUED", reason)
                        return "resource-limit"
                    _save_lease(store, lease, state="COMPLETED", message="Sanitised staging is ready for the FIFO Glue dispatcher.")
                    if lease.get("state") == "CANCELLED":
                        return "cancelled"
                    return "completed"
            except Exception as error:
                job_id = str((session.get("ingestion") or {}).get("job_id") or "")
                if job_id:
                    store.put_status(JobStatus(job_id=job_id, phase="FAILED", message=str(error), error_code=type(error).__name__))
                _save_compat_session(store, session, phase="FAILED", progress_message="Leased worker phase failed.", error={"code": type(error).__name__, "message": str(error)})
                _save_lease(store, lease, state="FAILED", message=str(error), can_retry_large=False)
                raise
            if phase in {"GLUE_RUNNING", "SUCCEEDED", "FAILED", "DELETED"}:
                return phase.lower()
            waiting_state = "AWAITING_KEY" if phase == "READY_FOR_REVIEW" else "AWAITING_CONFIRMATION"
            _save_lease(store, lease, state=waiting_state, message="Waiting for the next upload action.")
            time.sleep(_LEASE_POLL_SECONDS)


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
    if job_id.startswith("lease:"):
        run_leased_worker(job_id.partition(":")[2], settings)
    elif job_id.startswith(("profile:", "key:")):
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

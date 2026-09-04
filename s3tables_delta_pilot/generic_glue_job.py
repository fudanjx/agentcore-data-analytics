"""Append-only ingestion, snapshot audit, and rollback job for the local UI.

Each master-table mutation is protected by an Iceberg snapshot recorded before
the mutation. The audit table is append-only; the small JSON projection is only
a UI read model and contains no healthcare row values.
"""

from __future__ import annotations

import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from functools import reduce
from operator import and_

import boto3
from awsglue.utils import getResolvedOptions
from pyspark import StorageLevel
from pyspark.sql import SparkSession, functions as F
from pyspark.sql.types import LongType, StringType, StructField, StructType


ARGS = getResolvedOptions(
    sys.argv,
    [
        "JOB_NAME", "MODE", "MANIFEST_URI", "TABLE_BUCKET_ARN", "NAMESPACE", "TABLE",
        "QC_PREFIX", "RUN_ID", "UPLOAD_ID", "UPLOADED_BY", "REPORTING_MONTH",
        "FILENAMES_JSON", "AUDIT_PREFIX", "ROLLBACK_SNAPSHOT_ID",
        "ORIGINAL_UPLOADED_BY", "ORIGINAL_UPLOADED_AT",
    ],
)
MODE = ARGS["MODE"].lower()
if MODE not in {"create", "append", "rollback"}:
    raise ValueError("MODE must be create, append, or rollback")

AUDIT_TABLE = "uploader_upload_history"


def _quoted(identifier: str) -> str:
    return "`" + identifier.replace("`", "``") + "`"


TARGET = ".".join(_quoted(value) for value in ("s3_rest_catalog", ARGS["NAMESPACE"], ARGS["TABLE"]))
AUDIT_TARGET = ".".join(_quoted(value) for value in ("s3_rest_catalog", ARGS["NAMESPACE"], AUDIT_TABLE))
s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION", "ap-southeast-1"))
spark = SparkSession.builder.getOrCreate()
spark.conf.set("spark.sql.catalog.s3_rest_catalog.warehouse", ARGS["TABLE_BUCKET_ARN"])
spark.sparkContext.setLogLevel("WARN")

AUDIT_COLUMNS = (
    "event_id", "upload_id", "target_table", "namespace", "table_bucket_arn", "reporting_month",
    "filenames", "uploaded_by", "uploaded_at", "previous_snapshot_id", "new_snapshot_id",
    "rows_before", "rows_uploaded", "rows_after", "status", "rollback_at", "rollback_by", "error_message",
)
AUDIT_SCHEMA = StructType([
    StructField(column, LongType(), True) if column in {"rows_before", "rows_uploaded", "rows_after"}
    else StructField(column, StringType(), True)
    for column in AUDIT_COLUMNS
])


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(uri: str) -> dict:
    bucket, key = uri.removeprefix("s3://").split("/", 1)
    return json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())


def _write_qc(report: dict) -> str:
    bucket, prefix = ARGS["QC_PREFIX"].removeprefix("s3://").split("/", 1)
    key = f"{prefix.rstrip('/')}/web/{ARGS['RUN_ID']}/report.json"
    report.update({"run_id": ARGS["RUN_ID"], "generated_at": _now()})
    s3.put_object(Bucket=bucket, Key=key, Body=json.dumps(report, indent=2, sort_keys=True).encode(), ContentType="application/json", ServerSideEncryption="AES256")
    return f"s3://{bucket}/{key}"


def _audit_projection_uri() -> str:
    prefix = ARGS["AUDIT_PREFIX"].removeprefix("s3://").rstrip("/")
    return f"s3://{prefix}/{ARGS['UPLOAD_ID']}.json"


def _write_audit_projection(event: dict) -> str:
    bucket, key = _audit_projection_uri().removeprefix("s3://").split("/", 1)
    s3.put_object(Bucket=bucket, Key=key, Body=json.dumps(event, indent=2, sort_keys=True).encode(), ContentType="application/json", ServerSideEncryption="AES256")
    return f"s3://{bucket}/{key}"


def _exists(target: str = TARGET) -> bool:
    try:
        spark.table(target).limit(1).collect()
        return True
    except Exception as error:
        message = str(error).lower()
        if "not found" in message or "cannot be found" in message or "does not exist" in message:
            return False
        raise


def _ensure_audit_table() -> None:
    definition = ", ".join(
        f"`{column}` {'BIGINT' if column in {'rows_before', 'rows_uploaded', 'rows_after'} else 'STRING'}"
        for column in AUDIT_COLUMNS
    )
    spark.sql(f"CREATE TABLE IF NOT EXISTS {AUDIT_TARGET} ({definition}) USING iceberg")
    actual = tuple(field.name for field in spark.table(AUDIT_TARGET).schema)
    if actual != AUDIT_COLUMNS:
        raise ValueError(f"Audit table {AUDIT_TARGET} has an unexpected schema; expected {AUDIT_COLUMNS}, got {actual}")


def _record_event(**values) -> dict:
    event = {column: None for column in AUDIT_COLUMNS}
    event.update({
        "event_id": str(uuid.uuid4()), "upload_id": ARGS["UPLOAD_ID"], "target_table": ARGS["TABLE"],
        "namespace": ARGS["NAMESPACE"], "table_bucket_arn": ARGS["TABLE_BUCKET_ARN"],
        "reporting_month": ARGS["REPORTING_MONTH"] or None, "filenames": ARGS["FILENAMES_JSON"] or "[]",
        # ``uploaded_*`` remains the original ingestion actor/timestamp even
        # after a rollback overwrites the compact UI projection.  ``rollback_*``
        # records the distinct administrator and time that performed rollback.
        "uploaded_by": ARGS["ORIGINAL_UPLOADED_BY"] or ARGS["UPLOADED_BY"] or None,
        "uploaded_at": ARGS["ORIGINAL_UPLOADED_AT"] or _now(), **values,
    })
    _ensure_audit_table()
    spark.createDataFrame([event], AUDIT_SCHEMA).writeTo(AUDIT_TARGET).append()
    _write_audit_projection(event)
    return event


def _snapshot_state() -> tuple[str | None, int]:
    rows = int(spark.table(TARGET).count())
    snapshots = spark.table(f"{TARGET}.snapshots").orderBy(F.col("committed_at").desc())
    latest = snapshots.select("snapshot_id").first()
    return (str(latest["snapshot_id"]) if latest else None, rows)


def _snapshot_row_count(snapshot_id: str) -> int:
    row = spark.table(f"{TARGET}.snapshots").where(F.col("snapshot_id") == int(snapshot_id)).select("summary").first()
    if not row:
        raise ValueError(f"Snapshot {snapshot_id} is not available for {TARGET}")
    total = (row["summary"] or {}).get("total-records")
    if total is None:
        raise ValueError(f"Snapshot {snapshot_id} has no total-records metric")
    return int(total)


def _create(schema: list[dict[str, str]]) -> None:
    fields = ", ".join(f"`{field['name']}` {field['type']}" for field in schema)
    spark.sql(f"CREATE TABLE {TARGET} ({fields}) USING iceberg")


def _source_lookup(columns: list[str]) -> dict[str, str]:
    lookup = {}
    for column in columns:
        base = re.sub(r"_+", "_", re.sub(r"[ /()\-]", "_", column)).strip("_").lower()
        if not base:
            raise ValueError(f"Input column normalises to an empty value: {column!r}")
        normalised, number = base, 1
        while normalised in lookup:
            normalised = f"{base}_{number:02d}"
            number += 1
        lookup[normalised] = column
    return lookup


def _project_incoming(manifest: dict):
    frames, unsafe_casts = [], 0
    for uri in manifest["files"]:
        raw = spark.read.parquet(uri)
        lookup, expressions = _source_lookup(raw.columns), []
        for field in manifest["schema"]:
            source_column = field.get("source_name") if field.get("source_name") in raw.columns else lookup.get(field["name"])
            if source_column is None:
                expressions.append(F.lit(None).cast(field["type"]).alias(field["name"]))
                continue
            value = F.col(source_column)
            cast = value.cast(field["type"])
            unsafe_casts += raw.where(value.isNotNull() & cast.isNull()).count()
            expressions.append(cast.alias(field["name"]))
        frames.append(raw.select(*expressions))
    if unsafe_casts:
        raise ValueError(f"Unsafe casts found: {unsafe_casts}. The uploader rejects unsafe casts and does not permit an override.")
    incoming = frames[0]
    for frame in frames[1:]:
        incoming = incoming.unionByName(frame)
    return incoming.persist(StorageLevel.MEMORY_AND_DISK), unsafe_casts


def _with_row_fingerprint(frame, columns: list[str]):
    """Add a deterministic, null-preserving full-row fingerprint.

    The hash narrows the distributed target-table join.  It is not trusted on
    its own: the join below also verifies every column with null-safe equality,
    so a theoretical hash collision can never remove a distinct row.
    """
    encoded = F.to_json(
        F.struct(*[F.col(column).alias(column) for column in columns]),
        options={"ignoreNullFields": "false"},
    )
    return frame.withColumn("__uploader_row_hash", F.sha2(encoded, 256))


def _exclude_existing_rows(incoming, columns: list[str]):
    """Return rows not already present in the target by complete row value."""
    candidate = _with_row_fingerprint(incoming, columns).alias("candidate")
    existing = _with_row_fingerprint(spark.table(TARGET).select(*columns), columns).alias("existing")
    equal_columns = [
        F.col(f"candidate.`{column.replace('`', '``')}`").eqNullSafe(
            F.col(f"existing.`{column.replace('`', '``')}`")
        )
        for column in columns
    ]
    condition = (F.col("candidate.__uploader_row_hash") == F.col("existing.__uploader_row_hash")) & reduce(and_, equal_columns)
    return candidate.join(existing, condition, "left_anti").select(
        *[F.col(f"candidate.`{column.replace('`', '``')}`").alias(column) for column in columns]
    )


def _qualified(alias: str, column: str):
    return F.col(f"{alias}.`{column.replace('`', '``')}`")


def _with_composite_key(frame, key_columns: list[str]):
    """Make a fixed-width composite key, retaining missing components.

    The selected columns are a tuple.  Each missing value gets an explicit
    sentinel, therefore ``(A, B, C, NULL)`` is distinct from
    ``(NULL, B, C, D)``.  Prefixing the base64-encoded components by name
    avoids ambiguous concatenations without exposing their values.
    """
    components = []
    for column in key_columns:
        value = F.col(column).cast("string")
        encoded = F.when(
            value.isNull() | (F.trim(value) == ""), F.lit("~")
        ).otherwise(F.base64(F.encode(value, "UTF-8")))
        components.append(F.concat(F.lit(f"{column}="), encoded))
    return frame.withColumn("__uploader_composite_key", F.concat_ws("|", *components))


def _deduplicate_incoming_by_keys(incoming, columns: list[str], key_columns: list[str]):
    """Apply the selected fixed composite tuple within one upload."""
    candidate = _with_composite_key(_with_row_fingerprint(incoming, columns), key_columns).withColumn(
        "__uploader_row_payload",
        F.to_json(F.struct(*[F.col(column).alias(column) for column in columns]), {"ignoreNullFields": "false"}),
    ).persist(StorageLevel.MEMORY_AND_DISK)
    grouped = candidate.groupBy("__uploader_composite_key").agg(
        F.count("*").alias("__uploader_key_rows"),
        F.countDistinct("__uploader_row_payload").alias("__uploader_key_variants"),
    )
    classified = candidate.join(grouped, "__uploader_composite_key", "inner").persist(StorageLevel.MEMORY_AND_DISK)
    conflict_rows = int(classified.where(F.col("__uploader_key_variants") > 1).count())
    conflict_keys = int(classified.where(F.col("__uploader_key_variants") > 1).select("__uploader_composite_key").distinct().count())
    exact_duplicate_rows = int(classified.where(F.col("__uploader_key_variants") == 1).count()) - int(
        classified.where(F.col("__uploader_key_variants") == 1).select("__uploader_composite_key").distinct().count()
    )
    ready = classified.where(F.col("__uploader_key_variants") == 1).dropDuplicates(["__uploader_composite_key"]).select(*columns).persist(StorageLevel.MEMORY_AND_DISK)
    classified.unpersist()
    candidate.unpersist()
    return ready, {
        "duplicate_rows_within_upload": exact_duplicate_rows,
        "within_upload_key_conflicts": conflict_rows,
        "within_upload_conflict_keys": conflict_keys,
    }


def _keyed_rows_to_append(incoming, columns: list[str], key_columns: list[str]):
    """Classify selected-key overlap without disclosing any healthcare values.

    A key match is never appended.  Full-row matches are ordinary duplicates;
    a non-key difference is a conflict which is also skipped.  The returned
    metrics deliberately contain only counts and column names.
    """
    candidate_source = _with_composite_key(incoming, key_columns).persist(StorageLevel.MEMORY_AND_DISK)
    existing_source = _with_composite_key(spark.table(TARGET).select(*columns), key_columns).persist(StorageLevel.MEMORY_AND_DISK)
    candidate = candidate_source.alias("candidate")
    duplicate_target_keys = int(existing_source.groupBy("__uploader_composite_key").count().where(F.col("count") > 1).count())
    if duplicate_target_keys:
        existing_source.unpersist()
        candidate_source.unpersist()
        raise ValueError(f"Target table has {duplicate_target_keys} duplicate composite de-duplication keys")
    existing = existing_source.withColumn("__uploader_existing_key", F.lit(1)).alias("existing")
    joined = candidate.join(existing, _qualified("candidate", "__uploader_composite_key") == _qualified("existing", "__uploader_composite_key"), "left").persist(StorageLevel.MEMORY_AND_DISK)
    existing_match = F.col("existing.__uploader_existing_key").isNotNull()
    full_match = reduce(and_, [
        _qualified("candidate", column).eqNullSafe(_qualified("existing", column))
        for column in columns
    ])
    keyed_rows_to_append = joined.where(~existing_match).select(
        *[_qualified("candidate", column).alias(column) for column in columns]
    )
    exact_duplicates = int(joined.where(existing_match & full_match).count())
    conflicts = joined.where(existing_match & ~full_match).persist(StorageLevel.MEMORY_AND_DISK)
    conflict_rows = int(conflicts.count())
    conflict_column_counts = {
        column: int(conflicts.where(~_qualified("candidate", column).eqNullSafe(_qualified("existing", column))).count())
        for column in columns if column not in key_columns
    }
    conflicts.unpersist()
    joined.unpersist()
    rows_to_append = keyed_rows_to_append.persist(StorageLevel.MEMORY_AND_DISK)
    existing_source.unpersist()
    candidate_source.unpersist()
    return rows_to_append, {
        "existing_key_exact_duplicates": exact_duplicates,
        "existing_key_conflicts": conflict_rows,
        "conflict_column_counts": {column: count for column, count in conflict_column_counts.items() if count},
    }


def _run_ingestion() -> dict:
    manifest = _read_json(ARGS["MANIFEST_URI"])
    if not manifest.get("schema"):
        raise ValueError("Manifest has no target schema")
    if MODE == "create" and _exists():
        raise ValueError(f"Refusing create: {TARGET} already exists")
    if MODE == "append" and not _exists():
        raise ValueError(f"Target {TARGET} does not exist")

    incoming, unsafe_casts = _project_incoming(manifest)
    incoming_rows = int(incoming.count())
    columns = [field["name"] for field in manifest["schema"]]
    deduplication_columns = manifest.get("deduplication_columns") or []
    if deduplication_columns:
        invalid_keys = sorted(set(deduplication_columns) - set(columns))
        if invalid_keys:
            raise ValueError(f"Manifest de-duplication columns are not in the table schema: {invalid_keys}")
    # Legacy tables retain the original full-row contract.  New tables choose
    # an immutable key at creation, which governs both within-file and target
    # table duplicate handling.
    if deduplication_columns:
        unique_incoming, incoming_key_metrics = _deduplicate_incoming_by_keys(incoming, columns, deduplication_columns)
    else:
        unique_incoming = incoming.dropDuplicates().persist(StorageLevel.MEMORY_AND_DISK)
        incoming_key_metrics = {"duplicate_rows_within_upload": incoming_rows - int(unique_incoming.count())}
    unique_incoming_rows = int(unique_incoming.count())
    duplicate_rows_within_upload = incoming_key_metrics["duplicate_rows_within_upload"]
    duplicate_metrics = {}
    if MODE == "create":
        _create(manifest["schema"])
        before_snapshot, before_rows = None, 0
        rows_to_append = unique_incoming
        duplicate_rows_already_in_table = 0
    else:
        before_snapshot, before_rows = _snapshot_state()
        if deduplication_columns:
            rows_to_append, duplicate_metrics = _keyed_rows_to_append(unique_incoming, columns, deduplication_columns)
            duplicate_rows_already_in_table = (
                duplicate_metrics["existing_key_exact_duplicates"]
                + duplicate_metrics["existing_key_conflicts"]
            )
        else:
            rows_to_append = _exclude_existing_rows(unique_incoming, columns).persist(StorageLevel.MEMORY_AND_DISK)
            duplicate_rows_already_in_table = unique_incoming_rows - int(rows_to_append.count())
    rows_appended = int(rows_to_append.count())
    processing = _record_event(
        previous_snapshot_id=before_snapshot, rows_before=before_rows, rows_uploaded=rows_appended,
        status="PROCESSING",
    )
    try:
        if rows_appended:
            rows_to_append.writeTo(TARGET).append()
            spark.catalog.clearCache()
            spark.catalog.refreshTable(TARGET)
        after_snapshot, after_rows = _snapshot_state()
        if after_rows != before_rows + rows_appended:
            raise ValueError(f"Post-append reconciliation failed: before={before_rows}, appended={rows_appended}, after={after_rows}")
        final = _record_event(
            previous_snapshot_id=before_snapshot, new_snapshot_id=after_snapshot, rows_before=before_rows,
            rows_uploaded=rows_appended, rows_after=after_rows, status="SUCCESS",
        )
        return {
            "processing_event_id": processing["event_id"], "audit_event_id": final["event_id"],
            "incoming_rows": incoming_rows, "unique_incoming_rows": unique_incoming_rows,
            "duplicate_rows_within_upload": duplicate_rows_within_upload,
            **{key: value for key, value in incoming_key_metrics.items() if key != "duplicate_rows_within_upload"},
            "duplicate_rows_already_in_table": duplicate_rows_already_in_table,
            "deduplication_policy": manifest.get("deduplication_policy", "legacy-full-row-v1"),
            "deduplication_columns": deduplication_columns,
            **duplicate_metrics,
            "rows_appended": rows_appended, "unsafe_cast_values": unsafe_casts,
            "target_before_rows": before_rows, "target_after_rows": after_rows,
            "previous_snapshot_id": before_snapshot, "new_snapshot_id": after_snapshot,
            "status": "committed",
        }
    except Exception as error:
        _, after_rows = _snapshot_state() if _exists() else (None, 0)
        _record_event(
            previous_snapshot_id=before_snapshot, rows_before=before_rows, rows_uploaded=rows_appended,
            rows_after=after_rows, status="FAILED", error_message=str(error),
        )
        raise
    finally:
        if rows_to_append is not unique_incoming:
            rows_to_append.unpersist()
        unique_incoming.unpersist()
        incoming.unpersist()


def _run_rollback() -> dict:
    if not _exists():
        raise ValueError(f"Target {TARGET} does not exist")
    snapshot_id = ARGS["ROLLBACK_SNAPSHOT_ID"].strip()
    if not snapshot_id.isdigit():
        raise ValueError("Rollback requires a numeric previous snapshot ID")
    expected_rows = _snapshot_row_count(snapshot_id)
    before_snapshot, before_rows = _snapshot_state()
    _record_event(
        previous_snapshot_id=snapshot_id, new_snapshot_id=before_snapshot, rows_before=before_rows,
        rows_uploaded=0, status="ROLLBACK_PROCESSING", rollback_at=_now(), rollback_by=ARGS["UPLOADED_BY"],
    )
    try:
        table_arg = f"{ARGS['NAMESPACE']}.{ARGS['TABLE']}".replace("'", "''")
        spark.sql(f"CALL s3_rest_catalog.system.rollback_to_snapshot(table => '{table_arg}', snapshot_id => {int(snapshot_id)})")
        spark.catalog.clearCache()
        spark.catalog.refreshTable(TARGET)
        after_snapshot, after_rows = _snapshot_state()
        if after_rows != expected_rows:
            raise ValueError(f"Rollback verification failed: expected_rows={expected_rows}, actual_rows={after_rows}")
        _record_event(
            previous_snapshot_id=snapshot_id, new_snapshot_id=after_snapshot, rows_before=before_rows,
            rows_uploaded=0, rows_after=after_rows, status="ROLLED_BACK", rollback_at=_now(), rollback_by=ARGS["UPLOADED_BY"],
        )
        return {"status": "rolled_back", "previous_snapshot_id": snapshot_id, "new_snapshot_id": after_snapshot, "rows_before": before_rows, "rows_after": after_rows}
    except Exception as error:
        _record_event(
            previous_snapshot_id=snapshot_id, new_snapshot_id=before_snapshot, rows_before=before_rows,
            rows_uploaded=0, status="ROLLBACK_FAILED", rollback_at=_now(), rollback_by=ARGS["UPLOADED_BY"], error_message=str(error),
        )
        raise


report = {"mode": MODE, "target": TARGET, "upload_id": ARGS["UPLOAD_ID"], "status": "started"}
try:
    report.update(_run_rollback() if MODE == "rollback" else _run_ingestion())
except Exception as error:
    report.update({"status": "failed", "error": str(error)})
    print(json.dumps({"status": "failed", "qc_uri": _write_qc(report), "error": str(error)}))
    raise
else:
    print(json.dumps({"status": report["status"], "qc_uri": _write_qc(report), **report}))

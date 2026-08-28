"""AWS Glue 5.0 job for the isolated AH SOC full-refresh delta pilot.

This job deliberately supports only explicit ``bootstrap``, ``delta`` and
``verify`` modes. It never calls overwrite, createOrReplace, update or merge.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime, timezone

import boto3
from awsglue.utils import getResolvedOptions
from pyspark import StorageLevel
from pyspark.sql import SparkSession, functions as F

from contract import SOURCE_COLUMNS, TARGET_COLUMNS, TIMESTAMP_TARGET_COLUMNS, assert_source_columns, target_name


REQUIRED = ["JOB_NAME", "MODE", "SOURCE_URI", "TABLE_BUCKET_ARN", "NAMESPACE", "TABLE", "QC_PREFIX", "RUN_ID"]
args = getResolvedOptions(sys.argv, REQUIRED)
MODE = args["MODE"].lower()
if MODE not in {"bootstrap", "delta", "verify"}:
    raise ValueError("MODE must be bootstrap, delta, or verify")

TARGET = f"s3_rest_catalog.{args['NAMESPACE']}.{args['TABLE']}"
REGION = os.environ.get("AWS_REGION", "ap-southeast-1")
PILOT_DELTA_MONTH = "2026-06"
s3 = boto3.client("s3", region_name=REGION)
spark = SparkSession.builder.getOrCreate()
spark.sparkContext.setLogLevel("WARN")


def _empty_to_null(column):
    cleaned = F.trim(column.cast("string"))
    return F.when(cleaned == "", F.lit(None).cast("string")).otherwise(cleaned)


def _parse_timestamp(column):
    raw = _empty_to_null(column)
    return F.coalesce(
        F.to_timestamp(raw, "yyyy-MM-dd HH:mm:ss.SSSSSS"),
        F.to_timestamp(raw, "yyyy-MM-dd HH:mm:ss"),
        F.to_timestamp(raw, "yyyy-MM-dd"),
        F.to_timestamp(raw, "dd.MM.yyyy"),
        F.to_timestamp(raw, "d/M/yyyy H:mm"),
        F.to_timestamp(raw, "d/M/yyyy"),
    )


def _prepare_source(uri: str):
    raw = spark.read.parquet(uri)
    assert_source_columns(raw.columns)
    projected = []
    invalid_conditions = []
    for source, target in zip(SOURCE_COLUMNS, TARGET_COLUMNS):
        if target in TIMESTAMP_TARGET_COLUMNS:
            parsed = _parse_timestamp(F.col(source))
            raw_value = _empty_to_null(F.col(source))
            invalid_conditions.append(raw_value.isNotNull() & parsed.isNull())
            projected.append(parsed.alias(target))
        elif target == "cnt":
            projected.append(F.col(source).cast("long").alias(target))
        else:
            projected.append(F.col(source).cast("string").alias(target))
    frame = raw.select(*projected)
    invalid = None
    for condition in invalid_conditions:
        invalid = condition if invalid is None else invalid | condition
    # Conditions refer to original raw fields, so evaluate before projection.
    invalid_dates = raw.where(invalid).count() if invalid is not None else 0
    if invalid_dates:
        raise ValueError(f"Source contains {invalid_dates} invalid timestamp values")
    return frame


def _with_key_and_hash(frame):
    epic = _empty_to_null(F.col("pat_enc_csn_id"))
    case_no = _empty_to_null(F.col("case_no"))
    visit_no = _empty_to_null(F.col("visit_no"))
    date = F.date_format(F.col("visit_date"), "yyyy-MM-dd")
    legacy_complete = case_no.isNotNull() & visit_no.isNotNull() & date.isNotNull()
    key = (
        F.when(epic.isNotNull(), F.concat(F.lit("E|"), epic))
        .when(legacy_complete, F.concat(F.lit("L|"), case_no, F.lit("|"), visit_no, F.lit("|"), date))
        .otherwise(F.lit(None).cast("string"))
    )
    row_json = F.to_json(F.struct(*[F.col(column) for column in TARGET_COLUMNS]), {"ignoreNullFields": "false"})
    return frame.withColumn("_business_key", key).withColumn("_row_hash", F.sha2(row_json, 256))


def _target_exists() -> bool:
    try:
        spark.table(TARGET).limit(1).collect()
        return True
    except Exception as error:  # Glue catalog throws an engine-specific analysis exception.
        message = str(error).lower()
        if (
            "not found" in message
            or "cannot be found" in message
            or "does not exist" in message
            or "table_or_view_not_found" in message
        ):
            return False
        raise


def _snapshot_details() -> dict:
    try:
        snapshots = spark.table(f"{TARGET}.snapshots")
        row = snapshots.agg(
            F.count("*").alias("snapshot_count"), F.max("committed_at").alias("last_committed_at")
        ).collect()[0]
        return {"snapshot_count": int(row["snapshot_count"]), "last_committed_at": str(row["last_committed_at"])}
    except Exception:
        return {"snapshot_count": None, "last_committed_at": None}


def _snapshot_count() -> int:
    """Return the exact Iceberg snapshot count or fail closed for recovery."""
    try:
        return int(spark.table(f"{TARGET}.snapshots").count())
    except Exception as error:
        raise ValueError(f"Cannot determine existing target snapshot count: {error}") from error


def _create_empty_target() -> None:
    """Create the S3 Table with SQL; REST catalogs do not support staged create."""
    definitions = []
    for column in TARGET_COLUMNS:
        data_type = "TIMESTAMP" if column in TIMESTAMP_TARGET_COLUMNS else "BIGINT" if column == "cnt" else "STRING"
        definitions.append(f"`{column}` {data_type}")
    spark.sql(
        f"CREATE TABLE {TARGET} ({', '.join(definitions)}) "
        "USING iceberg PARTITIONED BY (months(visit_date))"
    )


def _write_qc(report: dict) -> str:
    prefix = args["QC_PREFIX"].replace("s3://", "", 1)
    bucket, key_prefix = prefix.split("/", 1)
    key = f"{key_prefix.rstrip('/')}/{args['RUN_ID']}/report.json"
    report["run_id"] = args["RUN_ID"]
    report["generated_at"] = datetime.now(timezone.utc).isoformat()
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(report, sort_keys=True, indent=2).encode("utf-8"),
        ContentType="application/json",
        ServerSideEncryption="AES256",
    )
    return f"s3://{bucket}/{key}"


def _validate_key(frame, label: str) -> tuple[int, int]:
    null_count = frame.where(F.col("_business_key").isNull()).count()
    duplicate_groups = frame.groupBy("_business_key").count().where(F.col("count") > 1).count()
    if null_count or duplicate_groups:
        raise ValueError(f"{label} key validation failed: null_keys={null_count}, duplicate_key_groups={duplicate_groups}")
    return int(null_count), int(duplicate_groups)


def _verify_schema(target_frame) -> None:
    if tuple(target_frame.columns) != TARGET_COLUMNS:
        raise ValueError(f"Target schema columns drifted: {target_frame.columns}")
    target_types = {field.name: field.dataType.simpleString() for field in target_frame.schema.fields}
    expected_timestamps = {name: "timestamp" for name in TIMESTAMP_TARGET_COLUMNS}
    for name, expected in expected_timestamps.items():
        if target_types.get(name) != expected:
            raise ValueError(f"Target {name} type must be {expected}; found {target_types.get(name)}")
    if target_types.get("cnt") != "bigint":
        raise ValueError(f"Target cnt type must be bigint; found {target_types.get('cnt')}")


source = _with_key_and_hash(_prepare_source(args["SOURCE_URI"])).persist(StorageLevel.MEMORY_AND_DISK)
report = {
    "mode": MODE,
    "source_uri": args["SOURCE_URI"],
    "target": TARGET,
    "status": "started",
}

try:
    source_rows = source.count()
    source_null_keys, source_duplicate_keys = _validate_key(source, "source")
    date_bounds = source.agg(F.min("visit_date").alias("min"), F.max("visit_date").alias("max")).collect()[0]
    report.update({
        "source_rows": int(source_rows),
        "source_null_keys": source_null_keys,
        "source_duplicate_key_groups": source_duplicate_keys,
        "source_min_visit_date": str(date_bounds["min"]),
        "source_max_visit_date": str(date_bounds["max"]),
    })

    if MODE == "bootstrap":
        if _target_exists():
            recovery_target = spark.table(TARGET).select(*TARGET_COLUMNS)
            _verify_schema(recovery_target)
            if recovery_target.limit(1).count() != 0 or _snapshot_count() != 0:
                raise ValueError(f"Refusing bootstrap: target {TARGET} is not an exact-schema empty table without snapshots")
        else:
            _create_empty_target()
        # The table is now present. Append creates the single initial data snapshot atomically.
        source.select(*TARGET_COLUMNS).writeTo(TARGET).append()
        target = spark.table(TARGET).select(*TARGET_COLUMNS).persist(StorageLevel.MEMORY_AND_DISK)
        _verify_schema(target)
        final_rows = target.count()
        final_keys = _with_key_and_hash(target).select("_business_key").distinct().count()
        if final_rows != source_rows or final_keys != source_rows:
            raise ValueError(f"Bootstrap reconciliation failed: target_rows={final_rows}, target_keys={final_keys}")
        report.update({"target_before_rows": 0, "new_rows": int(source_rows), "target_after_rows": int(final_rows), "distinct_business_keys": int(final_keys), "status": "committed", **_snapshot_details()})
        target.unpersist()

    else:
        if not _target_exists():
            raise ValueError(f"Target {TARGET} does not exist; run bootstrap first")
        target = spark.table(TARGET).select(*TARGET_COLUMNS).persist(StorageLevel.MEMORY_AND_DISK)
        _verify_schema(target)
        target_keyed = _with_key_and_hash(target).persist(StorageLevel.MEMORY_AND_DISK)
        target_rows = target_keyed.count()
        _, target_duplicate_keys = _validate_key(target_keyed, "target")
        if target_duplicate_keys:
            raise ValueError("Target contains duplicate business keys")

        source_keys = source.select("_business_key", F.col("_row_hash").alias("_source_row_hash"))
        target_keys = target_keyed.select("_business_key", F.col("_row_hash").alias("_target_row_hash"))
        new_keyed = source.join(target_keys.select("_business_key"), "_business_key", "left_anti").persist(StorageLevel.MEMORY_AND_DISK)
        new_rows = new_keyed.count()
        new_date_bounds = new_keyed.agg(F.min("visit_date").alias("min"), F.max("visit_date").alias("max")).collect()[0]
        overlap = source_keys.join(target_keys, "_business_key", "inner").persist(StorageLevel.MEMORY_AND_DISK)
        overlap_rows = overlap.count()
        changed_rows = overlap.where(F.col("_source_row_hash") != F.col("_target_row_hash")).count()
        missing_rows = target_keys.join(source_keys.select("_business_key"), "_business_key", "left_anti").count()
        report.update({
            "target_before_rows": int(target_rows), "new_rows": int(new_rows), "overlap_rows": int(overlap_rows),
            "changed_overlap_rows": int(changed_rows), "missing_prior_rows": int(missing_rows),
            "new_min_visit_date": str(new_date_bounds["min"]), "new_max_visit_date": str(new_date_bounds["max"]),
            "target_duplicate_key_groups": int(target_duplicate_keys), **_snapshot_details(),
        })
        if changed_rows:
            raise ValueError(f"Unexpected changed overlap rows: {changed_rows}")
        if MODE == "delta" and new_rows and (
            new_date_bounds["min"].strftime("%Y-%m") != PILOT_DELTA_MONTH
            or new_date_bounds["max"].strftime("%Y-%m") != PILOT_DELTA_MONTH
        ):
            raise ValueError(f"Pilot delta contains rows outside {PILOT_DELTA_MONTH}")

        if MODE == "delta" and new_rows:
            new_keyed.select(*TARGET_COLUMNS).writeTo(TARGET).append()
            # Spark's REST catalog caches table metadata in this session. Reload
            # after the atomic Iceberg append before reconciliation.
            spark.catalog.clearCache()
            spark.catalog.refreshTable(TARGET)
        final = spark.table(TARGET).select(*TARGET_COLUMNS).persist(StorageLevel.MEMORY_AND_DISK)
        final_rows = final.count()
        final_keys = _with_key_and_hash(final).select("_business_key").distinct().count()
        expected_rows = target_rows + (new_rows if MODE == "delta" else 0)
        if final_rows != expected_rows or final_keys != final_rows:
            raise ValueError(f"Post-run reconciliation failed: rows={final_rows}, keys={final_keys}, expected_rows={expected_rows}")
        if MODE == "verify":
            june_rows = final.where(F.date_format(F.col("visit_date"), "yyyy-MM") == PILOT_DELTA_MONTH).count()
            report["pilot_delta_month_rows"] = int(june_rows)
            if june_rows != 20_777:
                raise ValueError(f"Expected 20777 rows in {PILOT_DELTA_MONTH}; found {june_rows}")
        report.update({"target_after_rows": int(final_rows), "distinct_business_keys": int(final_keys), "status": "committed" if MODE == "delta" and new_rows else "verified_noop", **_snapshot_details()})
        for frame in (target, target_keyed, new_keyed, overlap, final):
            frame.unpersist()

except Exception as error:
    report.update({"status": "failed", "error": str(error)})
    qc_uri = _write_qc(report)
    print(json.dumps({"qc_uri": qc_uri, "status": "failed", "error": str(error)}))
    raise
else:
    qc_uri = _write_qc(report)
    print(json.dumps({"qc_uri": qc_uri, **report}, default=str))
finally:
    source.unpersist()

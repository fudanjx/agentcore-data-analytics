"""Append-only generic Parquet ingestion job for the local S3 Tables pilot UI."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

import boto3
from awsglue.utils import getResolvedOptions
from pyspark import StorageLevel
from pyspark.sql import SparkSession, functions as F


ARGS = getResolvedOptions(sys.argv, ["JOB_NAME", "MODE", "MANIFEST_URI", "TABLE_BUCKET_ARN", "NAMESPACE", "TABLE", "QC_PREFIX", "RUN_ID"])
MODE = ARGS["MODE"].lower()
if MODE not in {"create", "append"}:
    raise ValueError("MODE must be create or append")

def _quoted(identifier: str) -> str:
    return "`" + identifier.replace("`", "``") + "`"


TARGET = ".".join(_quoted(value) for value in ("s3_rest_catalog", ARGS["NAMESPACE"], ARGS["TABLE"]))
s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION", "ap-southeast-1"))
spark = SparkSession.builder.getOrCreate()
# The web UI reuses this job for each user-authorized bucket. Set the catalog
# warehouse before any table operation so the selected request scope wins over
# the job definition's pilot default.
spark.conf.set("spark.sql.catalog.s3_rest_catalog.warehouse", ARGS["TABLE_BUCKET_ARN"])
spark.sparkContext.setLogLevel("WARN")


def _read_json(uri: str) -> dict:
    bucket, key = uri.removeprefix("s3://").split("/", 1)
    return json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())


def _write_qc(report: dict) -> str:
    bucket, prefix = ARGS["QC_PREFIX"].removeprefix("s3://").split("/", 1)
    key = f"{prefix.rstrip('/')}/web/{ARGS['RUN_ID']}/report.json"
    report.update({"run_id": ARGS["RUN_ID"], "generated_at": datetime.now(timezone.utc).isoformat()})
    s3.put_object(Bucket=bucket, Key=key, Body=json.dumps(report, indent=2, sort_keys=True).encode(), ContentType="application/json", ServerSideEncryption="AES256")
    return f"s3://{bucket}/{key}"


def _exists() -> bool:
    try:
        spark.table(TARGET).limit(1).collect()
        return True
    except Exception as error:
        message = str(error).lower()
        if "not found" in message or "cannot be found" in message or "does not exist" in message:
            return False
        raise


def _create(schema: list[dict[str, str]]) -> None:
    fields = ", ".join(f"`{field['name']}` {field['type']}" for field in schema)
    spark.sql(f"CREATE TABLE {TARGET} ({fields}) USING iceberg")


def _source_lookup(columns: list[str]) -> dict[str, str]:
    import re
    lookup = {}
    for column in columns:
        base = re.sub(r"_+", "_", re.sub(r"[ /()\-]", "_", column)).strip("_").lower()
        if not base:
            raise ValueError(f"Input column normalises to an empty value: {column!r}")
        normalised = base
        number = 1
        while normalised in lookup:
            normalised = f"{base}_{number:02d}"
            number += 1
        lookup[normalised] = column
    return lookup


def _cast_expression(column, target_type: str):
    return column.cast(target_type)


manifest = _read_json(ARGS["MANIFEST_URI"])
schema = manifest["schema"]
if not schema:
    raise ValueError("Manifest has no target schema")
report = {"mode": MODE, "target": TARGET, "status": "started", "input_file_count": len(manifest["files"])}

try:
    if MODE == "create" and _exists():
        raise ValueError(f"Refusing create: {TARGET} already exists")
    if MODE == "append" and not _exists():
        raise ValueError(f"Target {TARGET} does not exist")

    frames = []
    unsafe_casts = 0
    for uri in manifest["files"]:
        raw = spark.read.parquet(uri)
        lookup = _source_lookup(raw.columns)
        expressions = []
        for field in schema:
            # UI-created contracts retain the original source spelling. Prefer
            # it so reordered `BILL_NUM`/`Bill_Num` columns cannot be swapped.
            source_column = field.get("source_name") if field.get("source_name") in raw.columns else lookup.get(field["name"])
            if source_column is None:
                expressions.append(F.lit(None).cast(field["type"]).alias(field["name"]))
                continue
            value = F.col(source_column)
            cast = _cast_expression(value, field["type"])
            unsafe_casts += raw.where(value.isNotNull() & cast.isNull()).count()
            expressions.append(cast.alias(field["name"]))
        frames.append(raw.select(*expressions))
    if unsafe_casts and not manifest.get("allow_unsafe_casts", False):
        raise ValueError(f"Unsafe casts found: {unsafe_casts}. Re-run only after explicit user confirmation.")

    incoming = frames[0]
    for frame in frames[1:]:
        incoming = incoming.unionByName(frame)
    incoming = incoming.persist(StorageLevel.MEMORY_AND_DISK)
    incoming_rows = incoming.count()
    report.update({"incoming_rows": int(incoming_rows), "unsafe_cast_values": int(unsafe_casts)})

    if MODE == "create":
        _create(schema)
        target_before = 0
    else:
        target_before = spark.table(TARGET).count()
    if incoming_rows:
        incoming.writeTo(TARGET).append()
        spark.catalog.clearCache()
        spark.catalog.refreshTable(TARGET)
    target_after = spark.table(TARGET).count()
    if target_after != target_before + incoming_rows:
        raise ValueError(f"Post-append reconciliation failed: before={target_before}, incoming={incoming_rows}, after={target_after}")
    report.update({"target_before_rows": int(target_before), "target_after_rows": int(target_after), "status": "committed"})
    incoming.unpersist()
except Exception as error:
    report.update({"status": "failed", "error": str(error)})
    print(json.dumps({"status": "failed", "qc_uri": _write_qc(report), "error": str(error)}))
    raise
else:
    print(json.dumps({"status": "committed", "qc_uri": _write_qc(report), **report}))

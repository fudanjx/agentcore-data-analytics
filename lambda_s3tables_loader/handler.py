"""
S3-event-triggered Lambda: load an AH parquet file into an S3 Tables (Iceberg) table.

Trigger: S3 ObjectCreated:* on s3://ah-data-analytics/Combined_*_encoded.parquet.gzip
One invocation per file → one Iceberg table overwrite.

Flow:
  1. Map S3 key basename → target table + partition/date column.
  2. Download parquet.gzip to /tmp; pandas.read_parquet.
  3. Sanitise column names; lowercase all (Glue federation requires lowercase).
  4. Apply parse_mixed_date_fast to date/timestamp string columns.
  5. Convert to pyarrow.Table; align to Iceberg schema (creating table on first run).
  6. table.overwrite(pyarrow_table) — full replace, matches current RDS ETL semantics.
"""

import json
import logging
import os
import sys
import tempfile
import urllib.parse

import boto3
import pandas as pd
import pyarrow as pa

# infra/ is mounted alongside handler.py in the container image
sys.path.insert(0, "/var/task")
from ah_transforms import (
    FILE_TO_TABLE,
    TABLE_METADATA,
    detect_temporal_type,
    parse_mixed_date_fast,
    sanitise_column_name,
)

from pyiceberg.catalog.rest import RestCatalog
from pyiceberg.partitioning import PartitionField, PartitionSpec
from pyiceberg.schema import Schema
from pyiceberg.transforms import MonthTransform
from pyiceberg.types import (
    BooleanType,
    DoubleType,
    LongType,
    NestedField,
    StringType,
    TimestampType,
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

REGION = os.environ.get("AWS_REGION", "ap-southeast-1")
TABLE_BUCKET_ARN = os.environ["TABLE_BUCKET_ARN"]
NAMESPACE = os.environ.get("NAMESPACE", "ah_analytics")

_catalog_cache = None


def _get_catalog() -> RestCatalog:
    global _catalog_cache
    if _catalog_cache is not None:
        return _catalog_cache
    _catalog_cache = RestCatalog(
        name="s3tables",
        **{
            "uri": f"https://s3tables.{REGION}.amazonaws.com/iceberg",
            "warehouse": TABLE_BUCKET_ARN,
            "rest.sigv4-enabled": "true",
            "rest.signing-name": "s3tables",
            "rest.signing-region": REGION,
        },
    )
    return _catalog_cache


# ---------------------------------------------------------------------------
# Type mapping: pandas dtype → Iceberg type
# ---------------------------------------------------------------------------

def _iceberg_type_for(sanitised_col: str, series: pd.Series, table: str):
    dtype = str(series.dtype)
    if dtype == "datetime64[ns]":
        return TimestampType()
    if dtype in ("int32", "int64", "Int8", "Int16", "Int32", "Int64"):
        return LongType()
    if dtype == "float64":
        non_null = series.dropna()
        if len(non_null) > 0 and (non_null % 1 == 0).all():
            return LongType()
        return DoubleType()
    if dtype == "bool":
        return BooleanType()
    if dtype in ("object", "string", "str"):
        temporal = detect_temporal_type(sanitised_col, series, table)
        if temporal == "TIMESTAMP":
            return TimestampType()
        # TIME "HH:MM:SS" values are stored as strings in Iceberg. Arrow's TimeType
        # requires int microseconds, not strings; keeping them as StringType avoids a
        # per-value parse and lets Athena users cast with CAST(col AS TIME) if needed.
        return StringType()
    return StringType()


# ---------------------------------------------------------------------------
# DataFrame prep
# ---------------------------------------------------------------------------

def _prepare_df(df: pd.DataFrame, table: str) -> tuple[pd.DataFrame, dict]:
    """Sanitise + lowercase column names; parse TIMESTAMP strings.

    Returns (df, iceberg_types) where iceberg_types maps lowercase col → Iceberg type.
    """
    rename_map = {}
    seen = {}
    for col in df.columns:
        san = sanitise_column_name(col).lower()
        if san in seen:
            seen[san] += 1
            san = f"{san}_{seen[san]}"
        else:
            seen[san] = 0
        rename_map[col] = san
    df = df.rename(columns=rename_map)

    iceberg_types = {}
    for san_col in df.columns:
        itype = _iceberg_type_for(san_col, df[san_col], table)
        iceberg_types[san_col] = itype

        if isinstance(itype, TimestampType) and str(df[san_col].dtype) in ("object", "string", "str"):
            df[san_col] = parse_mixed_date_fast(df[san_col])
        elif isinstance(itype, LongType) and str(df[san_col].dtype) == "float64":
            df[san_col] = df[san_col].astype("Int64")

    return df, iceberg_types


# ---------------------------------------------------------------------------
# Iceberg schema/table
# ---------------------------------------------------------------------------

def _build_schema(iceberg_types: dict) -> Schema:
    fields = [
        NestedField(field_id=i + 1, name=col, field_type=itype, required=False)
        for i, (col, itype) in enumerate(iceberg_types.items())
    ]
    return Schema(*fields)


def _build_partition_spec(schema: Schema, date_col_lowercase: str) -> PartitionSpec:
    field = schema.find_field(date_col_lowercase)
    return PartitionSpec(
        PartitionField(
            source_id=field.field_id,
            field_id=1000,
            transform=MonthTransform(),
            name=f"{date_col_lowercase}_month",
        )
    )


def _ensure_table(catalog: RestCatalog, table: str, schema: Schema, partition_spec: PartitionSpec):
    identifier = (NAMESPACE, table)
    if catalog.table_exists(identifier):
        return catalog.load_table(identifier)
    logger.info("Creating Iceberg table %s.%s", NAMESPACE, table)
    return catalog.create_table(
        identifier=identifier,
        schema=schema,
        partition_spec=partition_spec,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _process(bucket: str, key: str) -> dict:
    basename = os.path.basename(key)
    if basename not in FILE_TO_TABLE:
        logger.warning("Skipping unrecognised key: %s", key)
        return {"status": "skipped", "reason": "unrecognised_key", "key": key}

    table_name = FILE_TO_TABLE[basename]
    _, _, date_col_orig = TABLE_METADATA[table_name]
    date_col = sanitise_column_name(date_col_orig).lower()

    logger.info("Processing s3://%s/%s → %s (partition on %s)", bucket, key, table_name, date_col)

    s3 = boto3.client("s3", region_name=REGION)
    with tempfile.NamedTemporaryFile(suffix=".parquet.gzip", delete=False) as f:
        tmp_path = f.name
    try:
        s3.download_file(bucket, key, tmp_path)
        df = pd.read_parquet(tmp_path)
        logger.info("Loaded %d rows × %d columns", len(df), len(df.columns))
    finally:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass

    df, iceberg_types = _prepare_df(df, table_name)

    if date_col not in iceberg_types:
        raise ValueError(f"Partition column {date_col!r} missing from prepared df; have {list(iceberg_types)[:10]}...")

    schema = _build_schema(iceberg_types)
    partition_spec = _build_partition_spec(schema, date_col)

    catalog = _get_catalog()
    iceberg_table = _ensure_table(catalog, table_name, schema, partition_spec)

    # Align columns to Iceberg field order and cast into an Arrow table matching the schema
    arrow_schema = iceberg_table.schema().as_arrow()
    df = df[[f.name for f in iceberg_table.schema().fields]]
    arrow_table = pa.Table.from_pandas(df, schema=arrow_schema, preserve_index=False, safe=False)

    logger.info("Overwriting %s.%s with %d rows...", NAMESPACE, table_name, len(df))
    iceberg_table.overwrite(arrow_table)
    logger.info("Overwrite complete")

    return {"status": "ok", "table": table_name, "rows": len(df)}


def lambda_handler(event, context):
    logger.info("Event: %s", json.dumps(event, default=str)[:800])
    results = []
    for record in event.get("Records", []):
        s3_ev = record.get("s3", {})
        bucket = s3_ev.get("bucket", {}).get("name")
        key = urllib.parse.unquote_plus(s3_ev.get("object", {}).get("key", ""))
        if not bucket or not key:
            logger.warning("Skipping record with missing bucket/key")
            continue
        try:
            results.append(_process(bucket, key))
        except Exception as e:
            logger.error("Failed to process s3://%s/%s: %s", bucket, key, e, exc_info=True)
            results.append({"status": "error", "key": key, "error": str(e)})
            raise
    return {"processed": results}

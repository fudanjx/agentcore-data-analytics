"""Manually invoked, one-time NUH Parquet to S3 Tables migration.

This function intentionally has no S3 notification.  It moves one selected
source object into one new Iceberg table in a single overwrite commit, so a
failed write cannot expose a partial batch in that table.

Event examples:
  {"action": "inspect"}               # read-only schema and row-count report
  {"action": "migrate", "table": "emd"}
  {"action": "validate", "table": "emd"}
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import uuid
from dataclasses import dataclass

import boto3
import pyarrow as pa
import pyarrow.parquet as pq
from pyiceberg.catalog.rest import RestCatalog
from pyiceberg.schema import Schema
from pyiceberg.types import (
    BinaryType,
    BooleanType,
    DateType,
    DecimalType,
    DoubleType,
    FixedType,
    FloatType,
    IntegerType,
    LongType,
    NestedField,
    StringType,
    TimeType,
    TimestampType,
    TimestamptzType,
)

LOG = logging.getLogger()
LOG.setLevel(logging.INFO)

REGION = os.environ.get("AWS_REGION", "ap-southeast-1")
SOURCE_BUCKET = os.environ.get("SOURCE_BUCKET", "nuh-analytics")
TABLE_BUCKET_ARN = os.environ["TABLE_BUCKET_ARN"]
NAMESPACE = os.environ.get("NAMESPACE", "nuh")

JOBS = {
    "emd": "em_encoded.parquet.gzip",
    "inpatient": "in_encoded.parquet.gzip",
    "soc": "sc_encoded.parquet.gzip",
    "surgery": "su_encoded.parquet.gzip",
}

# Approved explicit names for source columns that differ only by case in the
# SURGERY Parquet schema.  They must remain distinct after S3 Tables' required
# lowercase normalization.  Every other field is simply lowercased.
SURGERY_COLUMN_RENAMES = {
    "TREATMENT_OU": "treatment_ou_1",
    "Treatment_OU": "treatment_ou_2",
    "POSTAL_CODE": "postal_code_1",
    "Postal_Code": "postal_code_epic_2",
    "BILL_NUM": "bill_num_1",
    "Bill_Num": "bill_num_2",
    "Accident_type": "accident_type_1",
    "Accident_Type": "ccident_type_2",
    "Hsp_Disch_Date_time": "hsp_disch_date_time_1",
    "Hsp_Disch_Date_Time": "hsp_disch_date_time_2",
}

_catalog: RestCatalog | None = None


class MigrationError(RuntimeError):
    """A source cannot be represented safely in an analytics-visible table."""


@dataclass(frozen=True)
class SourceContract:
    table: str
    key: str
    rows: int
    source_schema: pa.Schema
    target_names: tuple[str, ...]


def catalog() -> RestCatalog:
    global _catalog
    if _catalog is None:
        _catalog = RestCatalog(
            name="s3tables",
            **{
                "uri": f"https://s3tables.{REGION}.amazonaws.com/iceberg",
                "warehouse": TABLE_BUCKET_ARN,
                "rest.sigv4-enabled": "true",
                "rest.signing-name": "s3tables",
                "rest.signing-region": REGION,
            },
        )
    return _catalog


def target_names(table: str, schema: pa.Schema) -> tuple[str, ...]:
    """Return required lowercase names, refusing data-lossy name collisions."""
    names = tuple(
        SURGERY_COLUMN_RENAMES.get(field.name, field.name.lower())
        if table == "surgery" else field.name.lower()
        for field in schema
    )
    if len(names) != len(set(names)):
        collisions: dict[str, list[str]] = {}
        for field, target in zip(schema, names):
            collisions.setdefault(target, []).append(field.name)
        conflicts = {k: v for k, v in collisions.items() if len(v) > 1}
        raise MigrationError(f"Lowercase column-name collision: {conflicts}")
    if any(not name for name in names):
        raise MigrationError("Source contains an empty column name")
    return names


def iceberg_type(field: pa.Field):
    typ = field.type
    if pa.types.is_string(typ) or pa.types.is_large_string(typ):
        return StringType()
    if pa.types.is_binary(typ) or pa.types.is_large_binary(typ):
        return BinaryType()
    if pa.types.is_fixed_size_binary(typ):
        return FixedType(typ.byte_width)
    if pa.types.is_boolean(typ):
        return BooleanType()
    if pa.types.is_int8(typ) or pa.types.is_int16(typ) or pa.types.is_int32(typ) or pa.types.is_uint8(typ) or pa.types.is_uint16(typ):
        return IntegerType()
    if pa.types.is_int64(typ) or pa.types.is_uint32(typ) or pa.types.is_uint64(typ):
        return LongType()
    if pa.types.is_float32(typ):
        return FloatType()
    if pa.types.is_float64(typ):
        return DoubleType()
    if pa.types.is_decimal(typ):
        return DecimalType(typ.precision, typ.scale)
    if pa.types.is_date(typ):
        return DateType()
    if pa.types.is_time(typ):
        return TimeType()
    if pa.types.is_timestamp(typ):
        return TimestamptzType() if typ.tz else TimestampType()
    raise MigrationError(f"Unsupported Parquet field {field.name!r}: {typ}")


def iceberg_schema(contract: SourceContract) -> Schema:
    return Schema(*[
        NestedField(
            field_id=index,
            name=target_name,
            field_type=iceberg_type(source_field),
            required=not source_field.nullable,
        )
        for index, (source_field, target_name) in enumerate(
            zip(contract.source_schema, contract.target_names), start=1
        )
    ])


def inspect_source(table: str) -> SourceContract:
    key = JOBS[table]
    s3 = boto3.client("s3", region_name=REGION)
    with tempfile.NamedTemporaryFile(suffix=".parquet.gzip", delete=False) as tmp:
        path = tmp.name
    try:
        LOG.info("Downloading s3://%s/%s for preflight", SOURCE_BUCKET, key)
        s3.download_file(SOURCE_BUCKET, key, path)
        parquet = pq.ParquetFile(path)
        return SourceContract(
            table=table,
            key=key,
            rows=parquet.metadata.num_rows,
            source_schema=parquet.schema_arrow,
            target_names=target_names(table, parquet.schema_arrow),
        )
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def source_report(contract: SourceContract) -> dict:
    return {
        "table": contract.table,
        "source": f"s3://{SOURCE_BUCKET}/{contract.key}",
        "rows": contract.rows,
        "columns": [
            {
                "source_name": source.name,
                "target_name": target,
                "source_type": str(source.type),
                "nullable": source.nullable,
            }
            for source, target in zip(contract.source_schema, contract.target_names)
        ],
    }


def ensure_namespace() -> None:
    client = boto3.client("s3tables", region_name=REGION)
    try:
        client.get_namespace(tableBucketARN=TABLE_BUCKET_ARN, namespace=NAMESPACE)
    except client.exceptions.NotFoundException:
        client.create_namespace(tableBucketARN=TABLE_BUCKET_ARN, namespace=[NAMESPACE])
        LOG.info("Created namespace %s", NAMESPACE)


def write_streamed_initial_snapshot(
    iceberg_table, path: str, contract: SourceContract, batch_size: int = 100_000
) -> None:
    """Write a large source in bounded memory, committing exactly one snapshot.

    PyIceberg's public ``append`` API creates an individual snapshot update for
    each call.  This lower-level single update stages data files from many
    Parquet batches, then commits them together once.  It is used only for the
    SOC source, which cannot be materialised inside Lambda's 10 GB limit.
    """
    from pyiceberg.io.pyarrow import _check_pyarrow_schema_compatible, _dataframe_to_data_files

    parquet = pq.ParquetFile(path)
    transaction = iceberg_table.transaction()
    rows_written = 0
    with transaction.update_snapshot().fast_append() as append_files:
        for batch in parquet.iter_batches(batch_size=batch_size):
            source_batch = pa.Table.from_batches([batch])
            target_batch = source_batch.rename_columns(list(contract.target_names))
            target_batch = target_batch.cast(iceberg_table.schema().as_arrow(), safe=True)
            _check_pyarrow_schema_compatible(
                transaction.table_metadata.schema(), provided_schema=target_batch.schema
            )
            data_files = _dataframe_to_data_files(
                table_metadata=transaction.table_metadata,
                # Each helper call starts its own zero-based data-file index.
                # Use a fresh UUID per batch so their staged object paths do
                # not collide, while the surrounding snapshot remains one
                # transaction and one final commit.
                write_uuid=uuid.uuid4(),
                df=target_batch,
                io=iceberg_table.io,
            )
            for data_file in data_files:
                append_files.append_data_file(data_file)
            rows_written += target_batch.num_rows
    if rows_written != contract.rows:
        raise MigrationError(
            f"Streamed row count mismatch before commit: expected={contract.rows}, actual={rows_written}"
        )
    transaction.commit_transaction()


def migrate(table: str) -> dict:
    contract = inspect_source(table)
    ice_catalog = catalog()
    identifier = (NAMESPACE, table)
    schema = iceberg_schema(contract)
    if ice_catalog.table_exists(identifier):
        # A failed invocation may have created the table definition before its
        # first atomic data snapshot.  It is safe to resume only that exact
        # empty state; any committed snapshot remains overwrite-protected.
        iceberg_table = ice_catalog.load_table(identifier)
        if iceberg_table.current_snapshot() is not None:
            raise MigrationError(
                f"Target {NAMESPACE}.{table} already has a data snapshot; refusing to overwrite it"
            )
        if iceberg_table.schema() != schema:
            raise MigrationError(
                f"Existing empty target {NAMESPACE}.{table} has a schema different from this source"
            )
        LOG.info("Resuming existing empty table %s.%s", NAMESPACE, table)
    else:
        ensure_namespace()
        # Recheck immediately before creation to protect against a concurrent run.
        if ice_catalog.table_exists(identifier):
            raise MigrationError(f"Target {NAMESPACE}.{table} appeared during preflight")
        iceberg_table = ice_catalog.create_table(identifier=identifier, schema=schema)
        LOG.info("Created empty table %s.%s", NAMESPACE, table)

    s3 = boto3.client("s3", region_name=REGION)
    with tempfile.NamedTemporaryFile(suffix=".parquet.gzip", delete=False) as tmp:
        path = tmp.name
    try:
        LOG.info("Downloading source for atomic initial snapshot: s3://%s/%s", SOURCE_BUCKET, contract.key)
        s3.download_file(SOURCE_BUCKET, contract.key, path)
        if table == "soc":
            # The largest source is ~3.34m rows; materialising it exceeds the
            # Lambda memory ceiling. Stream it into one staged snapshot instead.
            if pq.ParquetFile(path).schema_arrow != contract.source_schema:
                raise MigrationError("Source changed between preflight and load; target remains empty")
            LOG.info("Streaming one initial Iceberg snapshot for %s rows", contract.rows)
            write_streamed_initial_snapshot(iceberg_table, path, contract)
        else:
            source = pq.read_table(path)
            if source.num_rows != contract.rows or source.schema != contract.source_schema:
                raise MigrationError("Source changed between preflight and load; target remains empty")
            target = source.rename_columns(list(contract.target_names))
            target = target.cast(iceberg_table.schema().as_arrow(), safe=True)
            LOG.info("Writing one initial Iceberg snapshot: %s rows", target.num_rows)
            iceberg_table.overwrite(target)
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass

    return validate(table, contract)


def validate(table: str, contract: SourceContract | None = None) -> dict:
    contract = contract or inspect_source(table)
    iceberg_table = catalog().load_table((NAMESPACE, table))
    target_schema = iceberg_table.schema()
    expected_schema = iceberg_schema(contract)
    expected = [(field.name, str(field.field_type), field.required) for field in expected_schema.fields]
    actual = [(field.name, str(field.field_type), field.required) for field in target_schema.fields]
    if actual != expected:
        raise MigrationError(f"Schema mismatch for {NAMESPACE}.{table}: expected={expected}, actual={actual}")
    row_count = iceberg_table.scan().count()
    if row_count != contract.rows:
        raise MigrationError(f"Row count mismatch for {NAMESPACE}.{table}: source={contract.rows}, target={row_count}")
    snapshots = list(iceberg_table.snapshots())
    if len(snapshots) != 1:
        raise MigrationError(f"Expected one initial snapshot for {NAMESPACE}.{table}, found {len(snapshots)}")
    report = source_report(contract)
    report.update({"status": "validated", "target": f"{NAMESPACE}.{table}", "target_rows": row_count, "snapshots": len(snapshots)})
    return report


def lambda_handler(event, context):
    event = event or {}
    action = event.get("action", "inspect")
    selected = event.get("table")
    tables = [selected] if selected else list(JOBS)
    unknown = sorted(set(tables) - set(JOBS))
    if unknown:
        raise MigrationError(f"Unsupported table(s): {unknown}; supported={sorted(JOBS)}")

    if action == "inspect":
        return {"action": action, "tables": [source_report(inspect_source(table)) for table in tables]}
    if action == "migrate":
        if not selected:
            raise MigrationError("Migrate exactly one table per invocation; provide event.table")
        return {"action": action, "table": migrate(selected)}
    if action == "validate":
        if not selected:
            raise MigrationError("Validate exactly one table per invocation; provide event.table")
        return {"action": action, "table": validate(selected)}
    raise MigrationError("action must be inspect, migrate, or validate")

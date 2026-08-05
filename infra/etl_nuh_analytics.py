"""Stage and replace the four NUH analytics tables from S3 Parquet sources.

The loader deliberately keeps Parquet physical types and source column names:
whitespace in names becomes ``_`` and ``Unnamed*`` columns are omitted.  The
only content conversion is the approved EMD exception: slash-form
``EVENT_ED_TO_EDTU_DATE`` values in April/June 2026 are parsed day-first and
stored as canonical PostgreSQL timestamps.
"""

import io
import json
import os
import re
import tempfile
from dataclasses import dataclass

import boto3
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import psycopg2


REGION = "ap-southeast-1"
SECRET_ARN = os.environ.get(
    "SECRET_ARN",
    "arn:aws:secretsmanager:ap-southeast-1:964340114883:secret:agentcore-rds-credentials-tlv56J",
)
TARGET_DB = "nuh-analytics"
S3_BUCKET = "nuh-analytics"
STAGING_SCHEMA = "nuh_etl_staging"
BATCH_SIZE = int(os.environ.get("NUH_ETL_BATCH_SIZE", "25000"))

JOBS = (
    ("em_encoded.parquet.gzip", "emd"),
    ("in_encoded.parquet.gzip", "inpatient_movement"),
    ("sc_encoded.parquet.gzip", "soc"),
    ("su_encoded.parquet.gzip", "surgery"),
)

EVENT_ED_TO_EDTU_DATE = "EVENT_ED_TO_EDTU_DATE"
PERIOD = "PERIOD"
DAY_FIRST_SOURCE_PERIODS = {"Apr 2026", "Jun 2026"}
ISO_DATETIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?$"
)
SLASH_DATE_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")


class SourceValidationError(ValueError):
    """The input would require an unapproved or ambiguous transformation."""


@dataclass(frozen=True)
class ColumnSpec:
    source_name: str
    target_name: str
    pg_type: str


def quote_ident(identifier: str) -> str:
    if not identifier or "\x00" in identifier:
        raise SourceValidationError(f"Invalid SQL identifier: {identifier!r}")
    return '"' + identifier.replace('"', '""') + '"'


def normalise_column_name(name: str) -> str:
    """Preserve the source name except that whitespace becomes one underscore."""
    return re.sub(r"\s+", "_", name.strip())


def is_unnamed_column(name: str) -> bool:
    return name.strip().lower().startswith("unnamed")


def arrow_type_to_pg(field: pa.Field, table: str) -> str:
    if table == "emd" and field.name == EVENT_ED_TO_EDTU_DATE:
        return "TIMESTAMP WITHOUT TIME ZONE"
    if pa.types.is_string(field.type) or pa.types.is_large_string(field.type):
        return "TEXT"
    if pa.types.is_float64(field.type) or pa.types.is_float32(field.type):
        return "DOUBLE PRECISION"
    if pa.types.is_int64(field.type) or pa.types.is_int32(field.type):
        return "BIGINT"
    if pa.types.is_int16(field.type):
        return "SMALLINT"
    if pa.types.is_int8(field.type):
        return "SMALLINT"
    if pa.types.is_boolean(field.type):
        return "BOOLEAN"
    if pa.types.is_date(field.type):
        return "DATE"
    if pa.types.is_time(field.type):
        return "TIME WITHOUT TIME ZONE"
    if pa.types.is_timestamp(field.type):
        return (
            "TIMESTAMP WITH TIME ZONE"
            if field.type.tz
            else "TIMESTAMP WITHOUT TIME ZONE"
        )
    raise SourceValidationError(
        f"Unsupported Parquet type for {table}.{field.name}: {field.type}"
    )


def build_column_specs(schema: pa.Schema, table: str) -> tuple[list[ColumnSpec], list[str]]:
    specs: list[ColumnSpec] = []
    dropped: list[str] = []
    seen: set[str] = set()
    for field in schema:
        if is_unnamed_column(field.name):
            dropped.append(field.name)
            continue
        target_name = normalise_column_name(field.name)
        if not target_name:
            raise SourceValidationError(f"Empty column name in {table}")
        if len(target_name.encode("utf-8")) > 63:
            raise SourceValidationError(
                f"PostgreSQL identifier exceeds 63 bytes: {table}.{target_name}"
            )
        if target_name in seen:
            raise SourceValidationError(
                f"Column-name collision after whitespace normalization: {target_name}"
            )
        seen.add(target_name)
        specs.append(
            ColumnSpec(field.name, target_name, arrow_type_to_pg(field, table))
        )
    return specs, dropped


def get_creds() -> dict:
    client = boto3.client("secretsmanager", region_name=REGION)
    return json.loads(client.get_secret_value(SecretId=SECRET_ARN)["SecretString"])


def get_conn(creds: dict):
    return psycopg2.connect(
        host=creds["host"],
        port=int(creds.get("port", 5432)),
        user=creds["username"],
        password=creds["password"],
        dbname=TARGET_DB,
        connect_timeout=30,
    )


def validate_timestamp_precision(batch: pa.RecordBatch, schema: pa.Schema, table: str):
    """Reject nanosecond values PostgreSQL would silently round to microseconds."""
    for index, field in enumerate(schema):
        if not pa.types.is_timestamp(field.type):
            continue
        epoch_ns = pc.cast(batch.column(index), pa.int64()).to_pylist()
        if any(value is not None and abs(value) % 1000 for value in epoch_ns):
            raise SourceValidationError(
                f"{table}.{field.name} contains sub-microsecond timestamps"
            )


def apply_emd_event_date_rule(frame: pd.DataFrame) -> pd.DataFrame:
    """Make the sole approved string-date exception an unambiguous timestamp."""
    if EVENT_ED_TO_EDTU_DATE not in frame.columns or PERIOD not in frame.columns:
        raise SourceValidationError("EMD is missing the date exception or PERIOD column")

    values = frame[EVENT_ED_TO_EDTU_DATE].astype("string")
    periods = frame[PERIOD].astype("string")
    non_null = values.notna()
    iso = values.str.fullmatch(ISO_DATETIME_RE, na=False)
    slash = values.str.fullmatch(SLASH_DATE_RE, na=False)
    allowed_slash = slash & periods.isin(DAY_FIRST_SOURCE_PERIODS)
    invalid = non_null & ~(iso | allowed_slash)
    if invalid.any():
        period_counts = (
            periods[invalid].fillna("<NULL>").value_counts().sort_index().to_dict()
        )
        raise SourceValidationError(
            "Unexpected EVENT_ED_TO_EDTU_DATE format or period; "
            f"rows={int(invalid.sum())}, periods={period_counts}"
        )

    parsed = pd.Series(pd.NaT, index=frame.index, dtype="datetime64[ns]")
    if iso.any():
        parsed.loc[iso] = pd.to_datetime(values.loc[iso], errors="raise")
    if allowed_slash.any():
        parsed.loc[allowed_slash] = pd.to_datetime(
            values.loc[allowed_slash], format="%d/%m/%Y", errors="raise"
        )
    frame[EVENT_ED_TO_EDTU_DATE] = parsed
    return frame


def create_staging_schema(conn):
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {quote_ident(STAGING_SCHEMA)} CASCADE")
        cur.execute(f"CREATE SCHEMA {quote_ident(STAGING_SCHEMA)}")
    conn.commit()


def create_table(conn, table: str, specs: list[ColumnSpec]):
    columns = ", ".join(
        f"{quote_ident(spec.target_name)} {spec.pg_type}" for spec in specs
    )
    with conn.cursor() as cur:
        cur.execute(
            f"CREATE TABLE {quote_ident(STAGING_SCHEMA)}.{quote_ident(table)} ({columns})"
        )
    conn.commit()


def copy_batch(conn, table: str, frame: pd.DataFrame, specs: list[ColumnSpec]):
    buffer = io.StringIO()
    frame.to_csv(
        buffer,
        index=False,
        header=False,
        na_rep="",
        date_format="%Y-%m-%d %H:%M:%S.%f",
    )
    buffer.seek(0)
    column_names = ", ".join(quote_ident(spec.target_name) for spec in specs)
    sql = (
        f"COPY {quote_ident(STAGING_SCHEMA)}.{quote_ident(table)} ({column_names}) "
        "FROM STDIN WITH (FORMAT CSV, NULL '')"
    )
    with conn.cursor() as cur:
        cur.copy_expert(sql, buffer)
    conn.commit()


def download_parquet(s3, s3_key: str) -> str:
    """Download one source object before decoding to avoid S3 range-read issues."""
    with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as temporary:
        path = temporary.name
    try:
        s3.download_file(S3_BUCKET, s3_key, path)
    except Exception:
        os.unlink(path)
        raise
    return path


def load_stage_table(conn, parquet_file: pq.ParquetFile, table: str, specs: list[ColumnSpec]):
    source_names = [spec.source_name for spec in specs]
    target_names = [spec.target_name for spec in specs]
    rows_loaded = 0
    create_table(conn, table, specs)

    for batch in parquet_file.iter_batches(batch_size=BATCH_SIZE):
        validate_timestamp_precision(batch, parquet_file.schema_arrow, table)
        batch_table = pa.Table.from_batches([batch]).select(source_names)
        frame = batch_table.to_pandas()
        if table == "emd":
            frame = apply_emd_event_date_rule(frame)
        frame.columns = target_names
        copy_batch(conn, table, frame, specs)
        rows_loaded += len(frame)

    return rows_loaded


def expected_information_schema_type(pg_type: str) -> str:
    return {
        "TEXT": "text",
        "DOUBLE PRECISION": "double precision",
        "BIGINT": "bigint",
        "SMALLINT": "smallint",
        "BOOLEAN": "boolean",
        "DATE": "date",
        "TIME WITHOUT TIME ZONE": "time without time zone",
        "TIMESTAMP WITHOUT TIME ZONE": "timestamp without time zone",
        "TIMESTAMP WITH TIME ZONE": "timestamp with time zone",
    }[pg_type]


def validate_table(
    conn, schema: str, table: str, specs: list[ColumnSpec], source_rows: int
):
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT COUNT(*) FROM {quote_ident(schema)}.{quote_ident(table)}"
        )
        target_rows = cur.fetchone()[0]
        cur.execute(
            """
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s
            ORDER BY ordinal_position
            """,
            (schema, table),
        )
        actual_schema = cur.fetchall()

    expected_schema = [
        (spec.target_name, expected_information_schema_type(spec.pg_type))
        for spec in specs
    ]
    if target_rows != source_rows:
        raise SourceValidationError(
            f"Row-count mismatch for {table}: source={source_rows}, target={target_rows}"
        )
    if actual_schema != expected_schema:
        raise SourceValidationError(
            f"Schema mismatch for {table}: expected={expected_schema}, actual={actual_schema}"
        )
    return {"source_rows": source_rows, "target_rows": target_rows, "schema_match": True}


def replace_public_tables(conn):
    """Atomically replace only the four user-approved public tables."""
    with conn.cursor() as cur:
        for _, table in JOBS:
            cur.execute(f"DROP TABLE IF EXISTS public.{quote_ident(table)}")
        for _, table in JOBS:
            cur.execute(
                f"ALTER TABLE {quote_ident(STAGING_SCHEMA)}.{quote_ident(table)} "
                "SET SCHEMA public"
            )
        cur.execute(f"DROP SCHEMA {quote_ident(STAGING_SCHEMA)}")
    conn.commit()


def main():
    print("Fetching RDS credentials from Secrets Manager...")
    creds = get_creds()
    s3 = boto3.client("s3", region_name=REGION)
    report: dict[str, dict] = {}
    source_contracts: dict[str, tuple[list[ColumnSpec], int]] = {}

    conn = get_conn(creds)
    try:
        create_staging_schema(conn)
        for s3_key, table in JOBS:
            head = s3.head_object(Bucket=S3_BUCKET, Key=s3_key)
            path = download_parquet(s3, s3_key)
            try:
                parquet_file = pq.ParquetFile(path)
                specs, dropped = build_column_specs(parquet_file.schema_arrow, table)
                source_rows = parquet_file.metadata.num_rows
                print(
                    json.dumps(
                        {
                            "event": "stage_start",
                            "table": table,
                            "s3_key": s3_key,
                            "source_rows": source_rows,
                            "source_columns": len(parquet_file.schema_arrow),
                            "target_columns": len(specs),
                            "dropped_unnamed_columns": dropped,
                            "etag": head["ETag"],
                        },
                        sort_keys=True,
                    )
                )
                loaded_rows = load_stage_table(conn, parquet_file, table, specs)
                if loaded_rows != source_rows:
                    raise SourceValidationError(
                        f"Loaded-row mismatch for {table}: {loaded_rows} != {source_rows}"
                    )
                report[table] = validate_table(
                    conn, STAGING_SCHEMA, table, specs, source_rows
                )
                source_contracts[table] = (specs, source_rows)
            finally:
                os.unlink(path)

        print(json.dumps({"event": "stage_validated", "tables": report}, sort_keys=True))
        replace_public_tables(conn)
        public_report = {}
        for _, table in JOBS:
            specs, source_rows = source_contracts[table]
            public_report[table] = validate_table(
                conn, "public", table, specs, source_rows
            )
        print(
            json.dumps(
                {"event": "cutover_complete", "tables": public_report},
                sort_keys=True,
            )
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()

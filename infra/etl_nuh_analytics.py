"""
ETL: Load S3 parquet files into RDS nuh-analytics database.

Files:
  s3://nuh-analytics/em.parquet → emd            (149,585 rows, 96 cols)
  s3://nuh-analytics/in.parquet → inpatient_movement (479,232 rows, 194 cols)
  s3://nuh-analytics/su.parquet → surgery         (125,949 rows, 114 cols)
  s3://nuh-analytics/sc.parquet → soc             (978,083 rows, 52 cols)

Type mapping:
  datetime64[ns]           → TIMESTAMP
  string "YYYY-MM-DD ..."  → TIMESTAMP (cast via pd.to_datetime)
  string "HH:MM:SS"        → TIME
  string "9999-12-31 ..."  → TIMESTAMP (sentinel preserved)
  int64                    → BIGINT
  float64 (all integers)   → BIGINT
  float64 (mixed)          → DOUBLE PRECISION
  string other             → TEXT
"""

import io
import json
import os
import re
import sys
import tempfile

import boto3
import pandas as pd
import psycopg2
import psycopg2.extras

REGION = "ap-southeast-1"
SECRET_ARN = os.environ.get(
    "SECRET_ARN",
    "arn:aws:secretsmanager:ap-southeast-1:964340114883:secret:agentcore-rds-credentials-tlv56J",
)
TARGET_DB = "nuh-analytics"
S3_BUCKET = "nuh-analytics"

JOBS = [
    ("em.parquet", "emd"),
    ("in.parquet", "inpatient_movement"),
    ("su.parquet", "surgery"),
    ("sc.parquet", "soc"),
]

# Columns that are stored as "HH:MM:SS" strings → TIME
TIME_STRING_COLS = {
    "inpatient_movement": {
        "MOVE_STIME", "MOVE_ETIME", "ATIME", "DTIME",
        "DEATH_TIME", "REPORTING_MOVE_STIME", "REPORTING_MOVE_ETIME",
    },
    "surgery": {
        "ATIME", "DTIME", "ENTOTTIME", "EXOTTIME", "SOPTIME", "EOPTIME",
    },
    "soc": {
        "SOC_APPT_TIME", "SOC_VISIT_TIME",
    },
}

# Columns that are stored as "YYYY-MM-DD HH:MM:SS" strings → TIMESTAMP
TIMESTAMP_STRING_COLS = {
    "emd": {"DATE_BIR"},
    "inpatient_movement": {"DATE_BIR", "DDATE", "MOVE_EDATE"},
}

# Columns to clean URL-encoded spaces
URL_ENCODED_TEXT_COLS = {
    "emd": {"PERIOD"},
}

TIME_RE = re.compile(r"^\d{2}:\d{2}(:\d{2})?$")


def get_creds():
    sm = boto3.client("secretsmanager", region_name=REGION)
    return json.loads(sm.get_secret_value(SecretId=SECRET_ARN)["SecretString"])


def get_conn(creds, dbname=None):
    return psycopg2.connect(
        host=creds["host"],
        port=int(creds.get("port", 5432)),
        user=creds["username"],
        password=creds["password"],
        dbname=dbname or creds.get("dbname", "postgres"),
        connect_timeout=30,
    )


def create_database(creds):
    conn = get_conn(creds, dbname="postgres")
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (TARGET_DB,)
            )
            if cur.fetchone():
                print(f"  Database '{TARGET_DB}' already exists — skipping create")
            else:
                cur.execute(f'CREATE DATABASE "{TARGET_DB}"')
                print(f"  Database '{TARGET_DB}' created")
    finally:
        conn.close()


def is_integer_float(series: pd.Series) -> bool:
    """Return True if all non-null float values are whole numbers."""
    non_null = series.dropna()
    if len(non_null) == 0:
        return True
    return (non_null % 1 == 0).all()


def pandas_dtype_to_pg(col: str, series: pd.Series, table: str) -> str:
    """Map a pandas column to a PostgreSQL type string."""
    dtype = str(series.dtype)

    if dtype == "datetime64[ns]":
        return "TIMESTAMP"

    if dtype in ("object", "string"):
        # Explicit TIME columns
        if col in TIME_STRING_COLS.get(table, set()):
            return "TIME"
        # Explicit TIMESTAMP string columns
        if col in TIMESTAMP_STRING_COLS.get(table, set()):
            return "TIMESTAMP"
        return "TEXT"

    if dtype == "int64":
        return "BIGINT"

    if dtype in ("Int8", "Int16", "Int32", "Int64"):
        return "BIGINT"

    if dtype == "float64":
        if is_integer_float(series):
            return "BIGINT"
        return "DOUBLE PRECISION"

    if dtype == "bool":
        return "BOOLEAN"

    return "TEXT"


def clean_column_value(val, col: str, table: str, pg_type: str):
    """Apply any column-specific cleaning before insertion."""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None

    # Clean URL-encoded spaces in PERIOD column
    if col in URL_ENCODED_TEXT_COLS.get(table, set()):
        return str(val).replace("_x0020_", " ")

    return val


def prepare_df(df: pd.DataFrame, table: str) -> tuple[pd.DataFrame, dict]:
    """Cast columns to correct types and return type map."""
    pg_types = {}

    for col in df.columns:
        pg_type = pandas_dtype_to_pg(col, df[col], table)
        pg_types[col] = pg_type

        if pg_type == "TIMESTAMP" and str(df[col].dtype) in ("object", "string"):
            # Cast string timestamps (including 9999 sentinel) — errors='coerce' for safety
            df[col] = pd.to_datetime(df[col], errors="coerce")

        elif pg_type == "TIME" and str(df[col].dtype) in ("object", "string"):
            # Keep as string — psycopg2 accepts 'HH:MM:SS' strings for TIME columns
            pass  # no conversion needed; psycopg2 handles 'HH:MM:SS' → TIME

        elif pg_type == "BIGINT" and str(df[col].dtype) == "float64":
            # Convert float→Int64 (nullable integer to preserve NaN as NULL)
            df[col] = df[col].astype("Int64")

        # Clean URL-encoded text
        if col in URL_ENCODED_TEXT_COLS.get(table, set()):
            df[col] = df[col].str.replace("_x0020_", " ", regex=False)

    return df, pg_types


def build_ddl(table: str, pg_types: dict) -> str:
    cols = ",\n  ".join(
        f'"{col}" {pg_type}' for col, pg_type in pg_types.items()
    )
    return f'CREATE TABLE "{table}" (\n  {cols}\n)'


def load_table(conn, df: pd.DataFrame, table: str, pg_types: dict):
    """Bulk-load DataFrame into PostgreSQL using COPY."""
    with conn.cursor() as cur:
        cur.execute(f'DROP TABLE IF EXISTS "{table}" CASCADE')
        cur.execute(build_ddl(table, pg_types))
        conn.commit()

    # Use COPY via StringIO for speed
    buf = io.StringIO()
    # Write CSV — NaT/NaN → empty string (NULL in COPY)
    df.to_csv(buf, index=False, header=False, na_rep="")
    buf.seek(0)

    col_list = ", ".join(f'"{c}"' for c in df.columns)
    copy_sql = (
        f'COPY "{table}" ({col_list}) FROM STDIN '
        f"WITH (FORMAT CSV, NULL '')"
    )

    with conn.cursor() as cur:
        cur.copy_expert(copy_sql, buf)
    conn.commit()


def process_file(s3_key: str, table: str, creds: dict):
    print(f"\n{'='*60}")
    print(f"Processing {s3_key} → {table}")

    # Download from S3
    s3 = boto3.client("s3", region_name=REGION)
    with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as f:
        tmp_path = f.name
    print(f"  Downloading s3://{S3_BUCKET}/{s3_key} ...")
    s3.download_file(S3_BUCKET, s3_key, tmp_path)

    # Read parquet
    df = pd.read_parquet(tmp_path)
    os.unlink(tmp_path)
    print(f"  Loaded: {len(df):,} rows × {len(df.columns)} columns")

    # Prepare types
    df, pg_types = prepare_df(df, table)

    # Verify key date/time columns
    dt_cols = [(c, t) for c, t in pg_types.items() if t in ("TIMESTAMP", "TIME")]
    print(f"  Date/time columns: {len(dt_cols)}")
    for col, pg_type in dt_cols[:5]:
        sample = df[col].dropna().iloc[0] if df[col].notna().any() else "all null"
        print(f"    {col} ({pg_type}): {sample}")
    if len(dt_cols) > 5:
        print(f"    ... and {len(dt_cols)-5} more")

    # Load into DB
    conn = get_conn(creds, dbname=TARGET_DB)
    try:
        load_table(conn, df, table, pg_types)
    finally:
        conn.close()

    print(f"  ✓ Loaded {len(df):,} rows into '{table}'")


def verify(creds):
    print("\n" + "="*60)
    print("Verification — row counts:")
    conn = get_conn(creds, dbname=TARGET_DB)
    try:
        with conn.cursor() as cur:
            for _, table in JOBS:
                cur.execute(f'SELECT COUNT(*) FROM "{table}"')
                count = cur.fetchone()[0]
                print(f"  {table}: {count:,} rows")
    finally:
        conn.close()


def main():
    print("Fetching RDS credentials from Secrets Manager...")
    creds = get_creds()
    print(f"  Host: {creds['host']}")

    print(f"\nCreating database '{TARGET_DB}'...")
    create_database(creds)

    for s3_key, table in JOBS:
        process_file(s3_key, table, creds)

    verify(creds)
    print("\nDone.")


if __name__ == "__main__":
    main()

"""
ETL: Load S3 parquet files into RDS ah-analytics database.

Source bucket: s3://ah-data-analytics/
Files → tables:
  Combined_SOC_encoded.parquet.gzip       → outpatient        (1.1M rows, 51 cols)
  Combined_UCC_encoded.parquet.gzip       → urgentcarecenter  (148K rows, 71 cols)
  Combined_adm_encoded.parquet.gzip       → admission         (131K rows, 65 cols)
  Combined_disch_encoded.parquet.gzip     → discharge         (130K rows, 96 cols)
  Combined_inflight_encoded.parquet.gzip  → inflight          (540K rows, 26 cols)
  Combined_procedure_encoded.parquet.gzip → procedure         (70K rows, 143 cols)

Column sanitisation:
  spaces         → underscores
  special chars (/, -, (, )) → underscores, then collapse/strip
  single-char "C" column    → record_type

Date/time detection (auto-sampled per column):
  datetime64[ns]           → TIMESTAMP
  "YYYY-MM-DD ..."         → TIMESTAMP  (ISO, pd.to_datetime)
  "DD.MM.YYYY"             → TIMESTAMP  (European, dayfirst=True)
  "M/D/YYYY H:MM"          → TIMESTAMP  (US, pd.to_datetime)
  "HH:MM:SS"               → TIME       (kept as string, psycopg2 casts)
  int64 / float64 (int)    → BIGINT
  float64 (mixed)          → DOUBLE PRECISION
  other                    → TEXT

A _table_metadata table is created for agent schema discovery.
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
TARGET_DB = "ah-analytics"
S3_BUCKET = "ah-data-analytics"

JOBS = [
    ("Combined_SOC_encoded.parquet.gzip",       "outpatient"),
    ("Combined_UCC_encoded.parquet.gzip",        "urgentcarecenter"),
    ("Combined_adm_encoded.parquet.gzip",        "admission"),
    ("Combined_disch_encoded.parquet.gzip",      "discharge"),
    ("Combined_inflight_encoded.parquet.gzip",   "inflight"),
    ("Combined_procedure_encoded.parquet.gzip",  "procedure"),
]

# Metadata for agent schema discovery
TABLE_METADATA = {
    "outpatient":       ("Specialist Outpatient Clinic (SOC) visits at Alexandra Hospital", "Combined_SOC_encoded.parquet.gzip", "Visit_Date"),
    "urgentcarecenter": ("Urgent Care Centre (UCC/A&E) emergency visits", "Combined_UCC_encoded.parquet.gzip", "Visit_Date"),
    "admission":        ("Inpatient admissions — patient demographics, diagnosis, admission class", "Combined_adm_encoded.parquet.gzip", "Adm_Date"),
    "discharge":        ("Inpatient discharges with full episode details, LOS, DRG", "Combined_disch_encoded.parquet.gzip", "Disch_Date"),
    "inflight":         ("Daily inpatient census — patients occupying beds each day", "Combined_inflight_encoded.parquet.gzip", "Inflight_Date"),
    "procedure":        ("Surgical and procedural cases with surgeon, anaesthesia, OT timing", "Combined_procedure_encoded.parquet.gzip", "Operation_Date"),
}

# Columns stored as "HH:MM:SS" strings → TIME
TIME_STRING_COLS = {
    "outpatient":       {"Visit_Time", "APPT_TIME"},
    "urgentcarecenter": {"Visit_Time", "PACS_Start_Time", "PACS_End_Time",
                         "Trauma_Start_Time", "Trauma_End_Time"},
    "admission":        {"Adm_Time", "Disch_Time"},
    "discharge":        {"Adm_Time", "Disch_Time", "Death_Time"},
    "procedure":        {"OT_Begin_Time", "OT_End_Time"},
}

# Columns stored as "DD.MM.YYYY" European date strings → TIMESTAMP (dayfirst=True)
EURO_DATE_COLS = {
    "outpatient":       {"Movement_Creation_Date"},
    "urgentcarecenter": {"DoB", "PACS_Start_Date"},
    "admission":        {"Birthdate"},
    "discharge":        {"Death_Date", "Physical_Adm_Date"},
    "procedure":        {"OT_Begin_Date", "OT_End_Date"},
}

# ISO / mixed datetime strings → TIMESTAMP (standard pd.to_datetime)
TIMESTAMP_STRING_COLS = {
    "urgentcarecenter": {
        "PACS_End_Date", "Trauma_Start_Date", "Trauma_End_Date",
        "ED_DEPARTURE_DTTM", "HOSPITAL_ADMISSION_DTTM", "ED_DISPOSITION_DTTM",
        "EVENT_ARRIVAL_TIME", "TRIAGE_START_TIME", "TRIAGE_END_TIME",
        "EDTU_BED_REQUEST_TIME", "EDTU_ADMIT_TIME", "EDTU_ORDER_BR_TIME",
        "EDTU_ORDER_NOTED_TIME", "EDTU_ORDER_ASSIGNED_TIME", "EDTU_ORDER_COMPLETED_TIME",
        "IP_BED_REQUEST_TIME", "IP_ADMIT_TIME", "IP_ORDER_BR_TIME",
        "IP_ORDER_NOTED_TIME", "IP_ORDER_ASSIGNED_TIME", "IP_ORDER_COMPLETED_TIME",
        "ED_DISCHARGE_TIME", "ED_DEPARTURE_TIME",
        "Visit_Date",
    },
    "outpatient": {
        "Visit_Date", "Appt_Creation_Date", "APPT_REQUEST_DTTM",
    },
    "admission": {
        "Adm_Date", "Disch_Date",
    },
    "discharge": {
        "Adm_Date", "Disch_Date",
    },
    "inflight": {
        "Admit_Date", "Inflight_Date",
    },
    "procedure": {
        "Operation_Date", "Hsp_Admsn_Instant", "Hsp_IP_Admsn_Instant",
        "Hsp_Disch_Instant", "Case_Order_Created_Date", "Requested_Date",
        "First_Scheduled_Instant", "Last_Scheduled_Instant",
        "Projected_Case_Start_Instant", "Projected_Case_End_Instant",
        "Procedure_Start_Instant", "Procedure_End_Instant",
        "In_Pre_Procedure_Care", "Pre_Procedure_Care_Complete",
        "Called_For", "In_Block_Area", "Out_of_Block_Area",
        "Sent_For", "In_OT_Reception", "In_Procedure_Room",
        "Surgical_Prep_Start", "Sedation_Start", "Anaesthesia_Start",
        "Anaesthesia_Ready", "Prepare_PACU_Bed", "Anaesthesia_Finish",
        "Out_of_Procedure_Room", "Clean_up_Complete",
        "In_PACU", "PACU_Care_Complete", "Out_of_PACU",
        "In_Amb_Unit_Post_Op_In_Recovery",
        "Amb_Unit_Post_Op_Complete_Recovery_Care_Complete",
        "Out_of_Amb_Unit_Out_of_Recovery", "Procedural_Care_Complete",
        "Hsp_Admsn_Instant", "ED_Arrival_Instant",
    },
}

TIME_RE = re.compile(r"^\d{2}:\d{2}(:\d{2})?$")


# ---------------------------------------------------------------------------
# Column name sanitisation
# ---------------------------------------------------------------------------

def sanitise_column_name(col: str) -> str:
    """Sanitise column name for PostgreSQL: spaces/special chars → underscores."""
    if col == "C":
        return "record_type"
    name = col
    # Replace special chars with underscores
    name = re.sub(r"[ /\-\(\)]", "_", name)
    # Collapse consecutive underscores
    name = re.sub(r"_+", "_", name)
    # Strip leading/trailing underscores
    name = name.strip("_")
    return name


# ---------------------------------------------------------------------------
# Credentials / connection
# ---------------------------------------------------------------------------

def get_creds():
    sm = boto3.client("secretsmanager", region_name=REGION)
    return json.loads(sm.get_secret_value(SecretId=SECRET_ARN)["SecretString"])


def get_conn(creds, dbname=None):
    return psycopg2.connect(
        host=creds["host"],
        port=int(creds.get("port", 5432)),
        user=creds["username"],
        password=creds["password"],
        dbname=dbname or "postgres",
        connect_timeout=30,
    )


def create_database(creds):
    conn = get_conn(creds, dbname="postgres")
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (TARGET_DB,))
            if cur.fetchone():
                print(f"  Database '{TARGET_DB}' already exists — skipping create")
            else:
                cur.execute(f'CREATE DATABASE "{TARGET_DB}"')
                print(f"  Database '{TARGET_DB}' created")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Type detection
# ---------------------------------------------------------------------------

def is_integer_float(series: pd.Series) -> bool:
    non_null = series.dropna()
    if len(non_null) == 0:
        return True
    return (non_null % 1 == 0).all()


ISO_DATE_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}"          # YYYY-MM-DD...
    r"|^\d{1,2}/\d{1,2}/\d{4}"    # M/D/YYYY
)
EURO_DATE_RE = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")  # DD.MM.YYYY


def pandas_dtype_to_pg(sanitised_col: str, original_col: str,
                        series: pd.Series, table: str) -> str:
    dtype = str(series.dtype)

    if dtype == "datetime64[ns]":
        return "TIMESTAMP"

    if dtype in ("object", "string", "str"):
        # Explicit TIME columns (use sanitised name for lookup)
        if sanitised_col in TIME_STRING_COLS.get(table, set()):
            return "TIME"
        # Explicit TIMESTAMP sets
        if sanitised_col in TIMESTAMP_STRING_COLS.get(table, set()):
            return "TIMESTAMP"
        if sanitised_col in EURO_DATE_COLS.get(table, set()):
            return "TIMESTAMP"

        # Auto-detect by sampling first non-null, non-NaT value
        sample_vals = series.dropna()
        sample_vals = sample_vals[sample_vals.astype(str).str.strip().str.upper() != "NAT"]
        if len(sample_vals) > 0:
            sample = str(sample_vals.iloc[0]).strip()
            if TIME_RE.match(sample):
                return "TIME"
            if EURO_DATE_RE.match(sample):
                return "TIMESTAMP"
            if ISO_DATE_RE.match(sample):
                return "TIMESTAMP"
        return "TEXT"

    if dtype == "int32":
        return "INTEGER"

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


# ---------------------------------------------------------------------------
# DataFrame preparation
# ---------------------------------------------------------------------------

def prepare_df(df: pd.DataFrame, table: str) -> tuple[pd.DataFrame, list, dict]:
    """
    Sanitise column names, cast types, return (df, ordered_col_list, pg_types_dict).
    pg_types uses sanitised names as keys.
    """
    rename_map = {col: sanitise_column_name(col) for col in df.columns}
    # Handle duplicate sanitised names by appending suffix
    seen = {}
    for orig, san in list(rename_map.items()):
        if san in seen:
            seen[san] += 1
            rename_map[orig] = f"{san}_{seen[san]}"
        else:
            seen[san] = 0

    df = df.rename(columns=rename_map)

    pg_types = {}
    for orig_col, san_col in rename_map.items():
        pg_type = pandas_dtype_to_pg(san_col, orig_col, df[san_col], table)
        pg_types[san_col] = pg_type

        if pg_type == "TIMESTAMP":
            if str(df[san_col].dtype) in ("object", "string", "str"):
                # Replace "NaT" string sentinels before parsing
                df[san_col] = df[san_col].replace({"NaT": None, "NULL": None, "": None})
                if san_col in EURO_DATE_COLS.get(table, set()):
                    df[san_col] = pd.to_datetime(df[san_col], dayfirst=True, errors="coerce")
                else:
                    df[san_col] = pd.to_datetime(df[san_col], errors="coerce")

        elif pg_type == "TIME":
            # Replace NaT/NULL strings with None; keep valid "HH:MM:SS" strings
            df[san_col] = df[san_col].replace({"NaT": None, "NULL": None, "": None})

        elif pg_type in ("BIGINT", "INTEGER") and str(df[san_col].dtype) == "float64":
            df[san_col] = df[san_col].astype("Int64")

    col_order = list(rename_map.values())
    return df, col_order, pg_types


# ---------------------------------------------------------------------------
# DDL + COPY load
# ---------------------------------------------------------------------------

def build_ddl(table: str, col_order: list, pg_types: dict) -> str:
    cols = ",\n  ".join(f'"{col}" {pg_types[col]}' for col in col_order)
    return f'CREATE TABLE "{table}" (\n  {cols}\n)'


def load_table(conn, df: pd.DataFrame, table: str, col_order: list, pg_types: dict):
    with conn.cursor() as cur:
        cur.execute(f'DROP TABLE IF EXISTS "{table}" CASCADE')
        cur.execute(build_ddl(table, col_order, pg_types))
        conn.commit()

    buf = io.StringIO()
    df[col_order].to_csv(buf, index=False, header=False, na_rep="")
    buf.seek(0)

    col_list = ", ".join(f'"{c}"' for c in col_order)
    copy_sql = f'COPY "{table}" ({col_list}) FROM STDIN WITH (FORMAT CSV, NULL \'\')'

    with conn.cursor() as cur:
        cur.copy_expert(copy_sql, buf)
    conn.commit()


# ---------------------------------------------------------------------------
# Metadata table
# ---------------------------------------------------------------------------

def create_metadata_table(conn, row_counts: dict):
    with conn.cursor() as cur:
        cur.execute("""
            DROP TABLE IF EXISTS _table_metadata;
            CREATE TABLE _table_metadata (
                table_name      TEXT PRIMARY KEY,
                description     TEXT,
                source_file     TEXT,
                row_count       BIGINT,
                date_range_col  TEXT,
                loaded_at       TIMESTAMP DEFAULT now()
            )
        """)
        for table, (desc, src_file, date_col) in TABLE_METADATA.items():
            cur.execute(
                """INSERT INTO _table_metadata
                   (table_name, description, source_file, row_count, date_range_col)
                   VALUES (%s, %s, %s, %s, %s)""",
                (table, desc, src_file, row_counts.get(table, 0), date_col),
            )
    conn.commit()
    print("  _table_metadata created")


# ---------------------------------------------------------------------------
# Per-file processing
# ---------------------------------------------------------------------------

def process_file(s3_key: str, table: str, creds: dict) -> int:
    print(f"\n{'='*60}")
    print(f"Processing {s3_key} → {table}")

    s3 = boto3.client("s3", region_name=REGION)
    with tempfile.NamedTemporaryFile(suffix=".parquet.gzip", delete=False) as f:
        tmp_path = f.name
    print(f"  Downloading s3://{S3_BUCKET}/{s3_key} ...")
    s3.download_file(S3_BUCKET, s3_key, tmp_path)

    df = pd.read_parquet(tmp_path)
    os.unlink(tmp_path)
    print(f"  Loaded: {len(df):,} rows × {len(df.columns)} columns")

    df, col_order, pg_types = prepare_df(df, table)

    dt_cols = [(c, t) for c, t in pg_types.items() if t in ("TIMESTAMP", "TIME")]
    print(f"  Date/time columns ({len(dt_cols)}):")
    for col, pg_type in dt_cols[:8]:
        sample = df[col].dropna().iloc[0] if df[col].notna().any() else "all null"
        print(f"    {col} ({pg_type}): {sample}")
    if len(dt_cols) > 8:
        print(f"    ... and {len(dt_cols)-8} more")

    conn = get_conn(creds, dbname=TARGET_DB)
    try:
        load_table(conn, df, table, col_order, pg_types)
    finally:
        conn.close()

    row_count = len(df)
    print(f"  ✓ Loaded {row_count:,} rows into '{table}'")
    return row_count


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def verify(creds, row_counts: dict):
    print(f"\n{'='*60}")
    print("Verification — row counts:")
    conn = get_conn(creds, dbname=TARGET_DB)
    try:
        with conn.cursor() as cur:
            for _, table in JOBS:
                cur.execute(f'SELECT COUNT(*) FROM "{table}"')
                count = cur.fetchone()[0]
                expected = row_counts.get(table, "?")
                match = "✓" if count == expected else "✗"
                print(f"  {match} {table}: {count:,} rows (expected {expected:,})")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("Fetching RDS credentials from Secrets Manager...")
    creds = get_creds()
    print(f"  Host: {creds['host']}")

    print(f"\nCreating database '{TARGET_DB}'...")
    create_database(creds)

    row_counts = {}
    for s3_key, table in JOBS:
        row_counts[table] = process_file(s3_key, table, creds)

    print(f"\nCreating metadata table...")
    conn = get_conn(creds, dbname=TARGET_DB)
    try:
        create_metadata_table(conn, row_counts)
    finally:
        conn.close()

    verify(creds, row_counts)
    print("\nDone.")


if __name__ == "__main__":
    main()

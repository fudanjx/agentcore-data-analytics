"""
Shared column-name sanitisation and mixed-format date parsing for AH data.

Extracted from etl_ah_analytics.py so both the Fargate RDS ETL and the
S3-event-triggered S3 Tables loader can import from a single source.
"""

import re

import pandas as pd


# ---------------------------------------------------------------------------
# File → table lookup (S3 key basename → target table + partition/date col)
# ---------------------------------------------------------------------------

TABLE_METADATA = {
    "outpatient":       ("Specialist Outpatient Clinic (SOC) visits at Alexandra Hospital", "Combined_SOC_encoded.parquet.gzip", "Visit_Date"),
    "urgentcarecenter": ("Urgent Care Centre (UCC/A&E) emergency visits", "Combined_UCC_encoded.parquet.gzip", "Visit_Date"),
    "admission":        ("Inpatient admissions — patient demographics, diagnosis, admission class", "Combined_adm_encoded.parquet.gzip", "Adm_Date"),
    "discharge":        ("Inpatient discharges with full episode details, LOS, DRG", "Combined_disch_encoded.parquet.gzip", "Disch_Date"),
    "inflight":         ("Daily inpatient census — patients occupying beds each day", "Combined_inflight_encoded.parquet.gzip", "Inflight_Date"),
    "procedure":        ("Surgical and procedural cases with surgeon, anaesthesia, OT timing", "Combined_procedure_encoded.parquet.gzip", "Operation_Date"),
}

JOBS = [(src_file, table) for table, (_, src_file, _) in TABLE_METADATA.items()]

FILE_TO_TABLE = {src_file: table for table, (_, src_file, _) in TABLE_METADATA.items()}


# ---------------------------------------------------------------------------
# Explicit per-column type hints (sanitised, original-case names)
# ---------------------------------------------------------------------------

TIME_STRING_COLS = {
    "outpatient":       {"Visit_Time", "APPT_TIME"},
    "urgentcarecenter": {"Visit_Time", "PACS_Start_Time", "PACS_End_Time",
                         "Trauma_Start_Time", "Trauma_End_Time"},
    "admission":        {"Adm_Time", "Disch_Time"},
    "discharge":        {"Adm_Time", "Disch_Time", "Death_Time"},
    "procedure":        {"OT_Begin_Time", "OT_End_Time"},
}

EURO_DATE_COLS = {
    "outpatient":       {"Movement_Creation_Date"},
    "urgentcarecenter": {"DoB", "PACS_Start_Date"},
    "admission":        {"Birthdate"},
    "discharge":        {"Death_Date", "Physical_Adm_Date"},
    "procedure":        {"OT_Begin_Date", "OT_End_Date"},
}

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


# ---------------------------------------------------------------------------
# Regexes
# ---------------------------------------------------------------------------

TIME_RE       = re.compile(r"^\d{2}:\d{2}(:\d{2})?$")
ISO_DATE_RE   = re.compile(r"^\d{4}-\d{2}-\d{2}")
EURO_DATE_RE  = re.compile(r"^\d{1,2}\.\d{2}\.\d{4}$")
SLASH_DATE_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{4}")

NULL_SENTINELS = {"nat", "null", "none", "nan", "", "-", "n/a", "na"}


# ---------------------------------------------------------------------------
# Column name sanitisation
# ---------------------------------------------------------------------------

def sanitise_column_name(col: str) -> str:
    """Sanitise a column name: spaces/special chars → underscores.

    Note: does NOT lowercase. Callers that need lowercase (e.g. Glue
    federation over S3 Tables) should apply .lower() themselves.
    """
    if col == "C":
        return "record_type"
    name = re.sub(r"[ /\-\(\)]", "_", col)
    name = re.sub(r"_+", "_", name)
    return name.strip("_")


# ---------------------------------------------------------------------------
# Mixed-era date parsing
# ---------------------------------------------------------------------------

def _safe_parse(vals, **kwargs) -> pd.Series:
    parsed = pd.to_datetime(vals, errors="coerce", **kwargs)
    oob = parsed.notna() & ((parsed.dt.year < 1678) | (parsed.dt.year > 2262))
    if oob.any():
        parsed = parsed.copy()
        parsed[oob] = pd.NaT
    return parsed.astype("datetime64[ns]")


def parse_mixed_date_fast(series: pd.Series) -> pd.Series:
    """Parse a date column that mixes SAP-era and EPIC-era formats.

    SAP era  (pre-2023): "YYYY-MM-DD HH:MM:SS"  or  "DD.MM.YYYY"  or  "D/M/YYYY H:MM"
    EPIC era (2023+):    "YYYY-MM-DD"
    """
    result = pd.Series(pd.NaT, index=series.index, dtype="datetime64[ns]")
    s = series.astype(str).str.strip()

    null_mask  = s.str.lower().isin(NULL_SENTINELS)
    iso_mask   = s.str.match(r"^\d{4}-\d{2}-\d{2}") & ~null_mask
    euro_mask  = s.str.match(r"^\d{1,2}\.\d{2}\.\d{4}") & ~null_mask & ~iso_mask
    slash_mask = s.str.match(r"^\d{1,2}/\d{1,2}/\d{4}") & ~null_mask & ~iso_mask & ~euro_mask

    if iso_mask.any():
        try:
            parsed = _safe_parse(s[iso_mask], format="ISO8601")
        except Exception:
            parsed = _safe_parse(s[iso_mask], format="mixed")
        result = result.copy()
        result.loc[iso_mask] = parsed.values
    if euro_mask.any():
        parsed = _safe_parse(s[euro_mask], dayfirst=True)
        result.loc[euro_mask] = parsed.values
    if slash_mask.any():
        parsed = _safe_parse(s[slash_mask], dayfirst=True)
        result.loc[slash_mask] = parsed.values

    return result


# ---------------------------------------------------------------------------
# Column-type detection (engine-agnostic — returns "TIMESTAMP" / "TIME" / None)
# ---------------------------------------------------------------------------

def detect_temporal_type(sanitised_col: str, series: pd.Series, table: str) -> str | None:
    """Return "TIMESTAMP", "TIME", or None (leave as-is) for a string column.

    Uses explicit per-table hint sets first, then samples 5 non-null values.
    """
    dtype = str(series.dtype)
    if dtype == "datetime64[ns]":
        return "TIMESTAMP"
    if dtype not in ("object", "string", "str"):
        return None

    if sanitised_col in TIME_STRING_COLS.get(table, set()):
        return "TIME"
    if sanitised_col in TIMESTAMP_STRING_COLS.get(table, set()):
        return "TIMESTAMP"
    if sanitised_col in EURO_DATE_COLS.get(table, set()):
        return "TIMESTAMP"

    clean = series.dropna()
    clean = clean[~clean.astype(str).str.strip().str.lower().isin(NULL_SENTINELS)]
    if len(clean) == 0:
        return None

    step = max(1, len(clean) // 5)
    for val in clean.iloc[::step].head(5):
        s = str(val).strip()
        if TIME_RE.match(s):
            return "TIME"
        if ISO_DATE_RE.match(s) or EURO_DATE_RE.match(s) or SLASH_DATE_RE.match(s):
            return "TIMESTAMP"
    return None

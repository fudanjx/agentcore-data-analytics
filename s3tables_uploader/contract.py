"""Reviewed AH SOC contract shared by the local runner and Glue job."""

from __future__ import annotations

import re

SOURCE_COLUMNS = (
    "Case_No", "Trt_OU", "Name", "Nationality", "Ext_Pat_ID", "Race", "Sex",
    "Age", "Attn_MCR", "Attn_Phy", "Visit_Type", "Movement_Creation_Date",
    "Visit_Date", "Visit_Time", "Trt_OU_ID", "Visit_No", "Class",
    "Clinical_Dept_ID", "Clinical_Dept", "Sub-Specialty_ID", "Sub-Specialty",
    "Postal_Code", "Comments", "Ref_Hosp_Address", "Trt_Cat", "Trt_Room",
    "Trt_Room_Name", "Ref_MCR", "Ref_Phy", "Pri_Diag_Code", "Pri_Diag_Desc",
    "Status", "cnt", "Referral_type", "Referral_Hospital", "prelim_flag",
    "PAT_ENC_CSN_ID", "Visit_Type_Desc", "PRC_Desc", "APPT_TIME",
    "Appt_Creation_Date", "APPT_WT", "ADT_PAT_CLASS", "APPT_STATUS",
    "Other_Diag_Code", "Other_Diag_Desc", "DIAG_CODE_TYPE_ALL",
    "APPT_REQUEST_DTTM", "PAT_PREF_INSTITUTION", "PRC_SUB-SPECIALTY",
    "Appt_Creation_Rationale",
)

TIMESTAMP_TARGET_COLUMNS = frozenset(
    {"movement_creation_date", "visit_date", "appt_creation_date", "appt_request_dttm"}
)


def target_name(source_name: str) -> str:
    """Match the established AH S3 Tables name-normalisation contract."""
    if source_name == "C":
        return "record_type"
    return re.sub(r"_+", "_", re.sub(r"[ /\-()]", "_", source_name)).strip("_").lower()


TARGET_COLUMNS = tuple(target_name(name) for name in SOURCE_COLUMNS)


def assert_source_columns(columns: list[str] | tuple[str, ...]) -> None:
    actual = tuple(columns)
    if actual != SOURCE_COLUMNS:
        missing = sorted(set(SOURCE_COLUMNS) - set(actual))
        unexpected = sorted(set(actual) - set(SOURCE_COLUMNS))
        raise ValueError(
            "SOC source schema drift: "
            f"missing={missing}, unexpected={unexpected}, order_matches={actual == SOURCE_COLUMNS}"
        )

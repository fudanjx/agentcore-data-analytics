---
name: ah-analytics-inflight
description: Column reference and SQL guidance for the ah-analytics inflight table (Combined_inflight — daily inpatient census). Use when writing SQL against the inflight table, or when the user asks about bed occupancy, patient-days, average daily census, beds in use, occupancy rate, or ward utilisation at Alexandra Hospital. Each row represents one patient occupying one bed on one calendar date.
---

# AH Analytics — inflight table (daily inpatient census)

**One row = one patient in one bed on one date. Primary date: `Inflight_Date`.**
Use for occupancy and patient-days. Do NOT use for admissions/discharges — see `references/admission.md` / `references/discharge.md`.

## Two source systems, same table

Like admission and discharge, inflight rows come from two eras with different identifier coverage:

| Era | Identifier | `C` (→ `record_type`) | `Diagnosis_Code`/`Diagnosis_Desc` | `Pri_Diagnosis_Code`/`Sec_Diagnosis_Code` |
|---|---|---|---|---|
| Legacy SAP (before 1 Jan 2023) | `Case_No` populated | Populated — single-letter SAP case-number suffix | Populated | Null |
| NGEMR/EPIC (from 1 Jan 2023) | `PAT_ENC_CSN_ID` populated | Mostly null | Mostly null | Populated (`Pri_Diagnosis_*` always; `Sec_Diagnosis_*` when a secondary diagnosis exists, pipe-delimited) |

**Caution — `Case_No` is not a clean era switch here.** Unlike admission/discharge, `Case_No` in inflight shows up populated for a large share of NGEMR-era rows too (same numeric format as the real SAP case number), not just the SAP era. Don't rely on `Case_No` being present or absent to detect era or to join NGEMR-era rows — use `PAT_ENC_CSN_ID` as the identifier for NGEMR-era joins, same as admission/discharge.

`C` has the same ETL mismatch documented in `references/admission.md`: the loader renames raw column `C` to `record_type`, but it is really the SAP case-number suffix letter — concatenate `C` + `Case_No` to get the full SAP case number, don't treat `record_type` as a category.

## ⚠️ Read this before answering any patient-days question

The production `pt_days_by_ward` report is **not** built from raw `inflight` alone. Patients admitted and discharged on the **same calendar date** never appear in a daily census snapshot. Production adds a synthetic one-row-per-case top-up sourced from `discharge` (same-day rows only, where `Adm_Date = Disch_Date`), joined to `admission` **on `PAT_ENC_CSN_ID`** for `Disch_Acmd_Cat` (discharge's own copy of that field is blank and must be refilled from admission's), filtered using `discharge`'s own mandatory filters, with `LOS = 1` and `Inflight_Date = Disch_Date`.

Because the top-up join key is `PAT_ENC_CSN_ID`, it only reliably picks up **NGEMR-era** same-day cases — legacy SAP-era same-day discharges (`PAT_ENC_CSN_ID` null) won't match and are effectively excluded from the top-up.

**Querying raw `inflight` alone always undercounts patient-days** — this is a structural gap in the table itself, not a magnitude issue that only shows up for high-turnover wards. The same-day top-up union above is required for every patient-days query, regardless of ward or period, to take into account the same-day discharge cases; the gap is simply more visible in high-turnover wards because more rows are missing there.

Conceptual union to replicate production:

```sql
SELECT "Ward", "Inflight_Date", "cnt", "Accom_Category", "Class" FROM inflight
WHERE "prelim_flag" = 'N'
  AND "Ward" NOT IN ('LWEDTU','LWASW','LWDSW','LWVOTU','LOMOT','LCUCC')

UNION ALL

SELECT d."Nrs_OU"        AS "Ward",
       d."Disch_Date"    AS "Inflight_Date",
       d."cnt",
       a."Disch_Acmd_Cat" AS "Accom_Category",
       d."Disch_Class"   AS "Class"
FROM discharge d
JOIN admission a ON d."PAT_ENC_CSN_ID" = a."PAT_ENC_CSN_ID"
WHERE d."Adm_Date" = d."Disch_Date"
  AND d."prelim_flag" = 'N'
  AND d."Adm_Type" IN ('EM','EL','SD','DI','TA','RA')
  AND d."Nrs_OU" NOT IN ('LWEDTU','LWASW','LWDSW','LWVOTU','LOMOT','LCUCC')
```

## Query baseline

Use the `inflight` filters and canonical date in `references/data-ontology.yaml`.

## Key columns

| Column | Type | Meaning |
|--------|------|---------|
| `Case_No` | TEXT | SAP-era episode identifier. See caution above — populated for many NGEMR-era rows too; don't use it to detect era. |
| `PAT_ENC_CSN_ID` | TEXT | NGEMR encounter identifier — use as the primary join key for NGEMR-era rows. |
| `C` (→ `record_type` in the DB) | TEXT | SAP case-number suffix letter, not a record-type category — see note above. |
| `Pat_Name` | TEXT | Patient name — PII. |
| `Ext_Pat_ID` | TEXT | NRIC/FIN-format patient identifier — PII. |
| `Bed` | TEXT | Bed code on census date, e.g. `L003035`. |
| `Ward` | TEXT | Ward code on this census date (apply exclusion here). |
| `Dept_OU` | TEXT | Department code on census date. |
| `Admit_Date` | TIMESTAMP | Original admission date. |
| `Inflight_Date` | TIMESTAMP | Census snapshot date — primary date filter. |
| `LOS` | INTEGER | Days in hospital as of census date. |
| `Attend_Phy` | TEXT | Attending physician on this date. |
| `Diagnosis_Code` / `Diagnosis_Desc` | TEXT | SAP-era diagnosis code/description. Null in NGEMR era — use `Pri_Diagnosis_Code`/`Pri_Diagnosis_Desc` instead. |
| `Pri_Diagnosis_Code` / `Pri_Diagnosis_Desc` | TEXT | NGEMR-era primary diagnosis (ICD-10-style, e.g. `G95.9`). Null in SAP era. |
| `Sec_Diagnosis_Code` / `Sec_Diagnosis_Desc` | TEXT | NGEMR-era secondary diagnoses, pipe-delimited when there are multiple (e.g. `I10 \| B35.6 \| R63.4`). Null in SAP era and when there's no secondary diagnosis. |
| `Age` | NUMERIC | Patient age. |
| `Sex` | TEXT | `M` / `F`. |
| `Trt_Cat` | TEXT | Treatment category, e.g. `CL3`, `B2L3`, `HDC`, `CCUC`. |
| `Class` | TEXT | Raw patient class code — resolve through `pt_class_abc` (see `references/pt-class-lookup.md`) to get `Class_abc`. |
| `Accom_Category` | TEXT | Actual accommodation type on this census date. |
| `Adm_Type` | TEXT | Original admission route. |
| `prelim_flag` | TEXT | `N` = finalised, `Y` = preliminary. Use `prelim_flag = 'N'` unless the user explicitly requests provisional data (see `data-ontology.yaml` global rule). |
| `cnt` | INTEGER | Always 1 — represents one patient-day. |

## Critical: the ICU/HD/ISO override chain

The production `Class_with_icu_iso` field is **not** `Accom_Category` with a `Class` fallback — it's the looked-up `Class_abc` (from `Class` via `pt_class_abc`, see `references/pt-class-lookup.md`) as the base, with ISO/ICU/HD as overrides:

```sql
CASE
  WHEN "Accom_Category" = 'ISO'   THEN 'ISO'
  WHEN LEFT("Trt_Cat", 3) = 'CCU' THEN 'ICU'
  WHEN LEFT("Trt_Cat", 2) = 'HD'  THEN 'HD'
  ELSE Class_abc   -- from pt_class_abc lookup on raw "Class", NOT Accom_Category
END AS effective_class
```

Note this differs from the discharge table's override chain (which checks `Nrs_OU` ward prefix `LW9`/`LW8` for ISO instead of `Accom_Category`) — don't reuse discharge's chain for inflight.

## Counting patterns

```sql
-- Total patient-days in a period
SELECT SUM("cnt") AS patient_days
FROM inflight
WHERE "prelim_flag" = 'N'
  AND "Ward" NOT IN ('LWEDTU','LWASW','LWDSW','LWVOTU','LOMOT','LCUCC')
  AND "Inflight_Date" BETWEEN '2024-01-01' AND '2024-12-31';

-- Average daily census by month
SELECT
  DATE_TRUNC('month', "Inflight_Date") AS month,
  ROUND(COUNT(*)::NUMERIC / COUNT(DISTINCT "Inflight_Date"), 1) AS avg_daily_census
FROM inflight
WHERE "prelim_flag" = 'N'
  AND "Ward" NOT IN ('LWEDTU','LWASW','LWDSW','LWVOTU','LOMOT','LCUCC')
GROUP BY 1 ORDER BY 1;
```

## Lodger identification

Patient whose accommodation class differs from their entitled class. Production first backfills blank/`OTHER` `Accom_Category` from the ward's default class (`Ward_cls` sheet in `Class.xlsx`), then compares against the **looked-up** `Class_abc` (not the raw `Class` code):

```sql
WHERE "Accom_Category" IN ('A1','B1','B2')
  AND Class_abc IN ('B1','B2','C')   -- from pt_class_abc lookup on raw "Class"
  AND "Accom_Category" != Class_abc
```

## Example: monthly patient-days by class

```sql
SELECT
  DATE_TRUNC('month', "Inflight_Date") AS month,
  CASE WHEN "Accom_Category" = 'OTHER' THEN "Class" ELSE "Accom_Category" END AS bed_class,
  SUM("cnt") AS patient_days
FROM inflight
WHERE "prelim_flag" = 'N'
  AND "Ward" NOT IN ('LWEDTU','LWASW','LWDSW','LWVOTU','LOMOT','LCUCC')
  AND "Inflight_Date" >= '2024-01-01'
GROUP BY 1, 2 ORDER BY 1, 2;
```

## Join to admission / discharge

Use the candidate joins in `references/data-ontology.yaml` and validate counts for the requested period. For NGEMR-era joins prefer `PAT_ENC_CSN_ID` (see the `Case_No` caution above).

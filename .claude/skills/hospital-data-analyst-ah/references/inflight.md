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

The production `pt_days_by_ward` report is **not** built from raw `inflight` alone. Patients admitted and discharged on the **same calendar date** never appear in a daily census snapshot. Production adds a synthetic one-row-per-case top-up sourced from `discharge` (same-day rows only, where `Adm_Date = Disch_Date`), joined to `admission` **on `PAT_ENC_CSN_ID`** for `Disch_Acmd_Cat` (discharge's own copy of that field is blank and must be refilled from admission's), filtered using `discharge`'s own mandatory filters, with `LOS = 1` and `Inflight_Date = Disch_Date`. **`Trt_Cat` for these rows uses discharge's own copy (`d."Trt_Cat"`), not admission's** — unlike `Disch_Acmd_Cat`, discharge's `Trt_Cat` is populated and needs no backfill from admission.

Because the top-up join key is `PAT_ENC_CSN_ID`, it only reliably picks up **NGEMR-era** same-day cases — legacy SAP-era same-day discharges (`PAT_ENC_CSN_ID` null) won't match and are effectively excluded from the top-up.

**Querying raw `inflight` alone always undercounts patient-days** — this is a structural gap in the table itself, not a magnitude issue that only shows up for high-turnover wards. The same-day top-up union above is required for every patient-days query, regardless of ward or period, to take into account the same-day discharge cases; the gap is simply more visible in high-turnover wards because more rows are missing there.

Conceptual union to replicate production:

```sql
SELECT "Ward", "Inflight_Date", "cnt", "Accom_Category", "Class" FROM inflight
WHERE "Ward" NOT IN ('LWEDTU','LWASW','LWDSW','LWVOTU','LOMOT','LCUCC')

UNION ALL

SELECT d."Nrs_OU"        AS "Ward",
       d."Disch_Date"    AS "Inflight_Date",
       d."cnt",
       a."Disch_Acmd_Cat" AS "Accom_Category",
       d."Disch_Class"   AS "Class",
       d."Trt_Cat"       AS "Trt_Cat"
FROM discharge d
JOIN admission a ON d."PAT_ENC_CSN_ID" = a."PAT_ENC_CSN_ID"
WHERE d."Adm_Date" = d."Disch_Date"
  AND d."Adm_Type" IN ('EM','EL','SD','DI','TA','RA')
  AND d."Nrs_OU" NOT IN ('LWEDTU','LWASW','LWDSW','LWVOTU','LOMOT','LCUCC')
```

Add `AND "prelim_flag" = 'N'` (both the `inflight` side and the `d.`/discharge side) only if the user explicitly asks to exclude provisional/preliminary records — don't filter on it by default.

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
| `prelim_flag` | TEXT | `N` = finalised, `Y` = preliminary. **Don't filter on this by default** — only add `WHERE "prelim_flag" = 'N'` when the user explicitly asks to exclude provisional records (see `data-ontology.yaml` global rule). |
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

## Ward / bed acuity (Trt_Cat → Acuity)

`Trt_Cat` encodes accommodation class *and* acuity level in one code (e.g. `CL3` = Class C, Level 3; `HDC` = High Dependency, Class C — not `<prefix>L<n>`). To get just the acuity level (`L1`/`L2`/`L3`/`EDTU`) for "how acute is this ward" or "acuity mix" questions, resolve `Trt_Cat` through the mapping below (sourced from `Class.xlsx`'s `Acuity` sheet, confirmed to cover every `Trt_Cat` value seen in the real sample data) — don't string-parse the trailing digit, several codes (`CCUC`, `HDC`, `EDTUB2`, `EDTVS`) don't follow that pattern.

**Note:** this mapping is specific to `inflight`'s `Trt_Cat`. The `trt_cat` column in `outpatient` is a different, unrelated code set (treatment category, e.g. `NC`) — don't cross-apply.

| Trt_Cat | Acuity | Trt_Cat | Acuity | Trt_Cat | Acuity |
|---|---|---|---|---|---|
| `AL1` | `L1` | `CCUA` | `L3` | `SOAL1` | `L1` |
| `AL2` | `L2` | `CCUB1` | `L3` | `SOB1L1` | `L1` |
| `AL3` | `L3` | `CCUB2` | `L3` | `SOB2L3` | `L3` |
| `B1L1` | `L1` | `CCUC` | `L3` | `SOCL1` | `L1` |
| `B1L2` | `L2` | `CL1` | `L1` | `SOCL2` | `L2` |
| `B1L3` | `L3` | `CL2` | `L2` | `SOCL3` | `L3` |
| `B2L1` | `L1` | `CL3` | `L3` | `EDTUB2` | `EDTU` |
| `B2L2` | `L2` | `DSBP` | `L3` | `EDTVS` | `EDTU` |
| `B2L3` | `L3` | `DSBS` | `L3` | `SSBP` | `L3` |
| `IAAL3` | `L3` | `HDA` | `L3` | `SSBS` | `L3` |
| `IAB1L3` | `L3` | `HDB1` | `L3` | `SSRPTE` | `L3` |
| `IAB2L3` | `L3` | `HDB2` | `L3` | `EDVA` | `L3` |
| `IACCB2` | `L3` | `HDC` | `L3` | `EDVB1` | `L3` |
| `IACL1` | `L1` | | | `EDVB2` | `L3` |
| `IACL2` | `L2` | | | `EDVC` | `L3` |
| `IACL3` | `L3` | | | | |

```sql
CASE "Trt_Cat"
  WHEN 'AL1' THEN 'L1' WHEN 'AL2' THEN 'L2' WHEN 'AL3' THEN 'L3'
  WHEN 'B1L1' THEN 'L1' WHEN 'B1L2' THEN 'L2' WHEN 'B1L3' THEN 'L3'
  WHEN 'B2L1' THEN 'L1' WHEN 'B2L2' THEN 'L2' WHEN 'B2L3' THEN 'L3'
  WHEN 'CCUA' THEN 'L3' WHEN 'CCUB1' THEN 'L3' WHEN 'CCUB2' THEN 'L3' WHEN 'CCUC' THEN 'L3'
  WHEN 'CL1' THEN 'L1' WHEN 'CL2' THEN 'L2' WHEN 'CL3' THEN 'L3'
  WHEN 'HDA' THEN 'L3' WHEN 'HDB1' THEN 'L3' WHEN 'HDB2' THEN 'L3' WHEN 'HDC' THEN 'L3'
  WHEN 'SOB2L3' THEN 'L3' WHEN 'SOCL1' THEN 'L1' WHEN 'SOCL2' THEN 'L2' WHEN 'SOCL3' THEN 'L3'
  WHEN 'EDTUB2' THEN 'EDTU' WHEN 'EDTVS' THEN 'EDTU'
  WHEN 'SSBS' THEN 'L3' WHEN 'SSBP' THEN 'L3' WHEN 'SSRPTE' THEN 'L3'
  WHEN 'IACCB2' THEN 'L3' WHEN 'IACL1' THEN 'L1' WHEN 'IACL2' THEN 'L2' WHEN 'IACL3' THEN 'L3'
  WHEN 'EDVA' THEN 'L3' WHEN 'EDVB1' THEN 'L3' WHEN 'EDVB2' THEN 'L3' WHEN 'EDVC' THEN 'L3'
  WHEN 'IAAL3' THEN 'L3' WHEN 'IAB1L3' THEN 'L3' WHEN 'IAB2L3' THEN 'L3'
  WHEN 'DSBS' THEN 'L3' WHEN 'DSBP' THEN 'L3'
  WHEN 'SOAL1' THEN 'L1' WHEN 'SOB1L1' THEN 'L1'
  ELSE NULL  -- unmapped Trt_Cat -- investigate before reporting
END AS acuity

-- Example: acuity mix by ward for a period
SELECT "Ward",
       CASE "Trt_Cat" ... END AS acuity,
       SUM("cnt") AS patient_days
FROM inflight
WHERE "Ward" NOT IN ('LWEDTU','LWASW','LWDSW','LWVOTU','LOMOT','LCUCC')
  AND "Inflight_Date" BETWEEN '2024-01-01' AND '2024-12-31'
GROUP BY 1, 2 ORDER BY 1, 2;
```

## Counting patterns

```sql
-- Total patient-days in a period
SELECT SUM("cnt") AS patient_days
FROM inflight
WHERE "Ward" NOT IN ('LWEDTU','LWASW','LWDSW','LWVOTU','LOMOT','LCUCC')
  AND "Inflight_Date" BETWEEN '2024-01-01' AND '2024-12-31';

-- Average daily census by month
SELECT
  DATE_TRUNC('month', "Inflight_Date") AS month,
  ROUND(COUNT(*)::NUMERIC / COUNT(DISTINCT "Inflight_Date"), 1) AS avg_daily_census
FROM inflight
WHERE "Ward" NOT IN ('LWEDTU','LWASW','LWDSW','LWVOTU','LOMOT','LCUCC')
GROUP BY 1 ORDER BY 1;
```

Add `AND "prelim_flag" = 'N'` only if the user explicitly asks to exclude provisional/preliminary records — don't filter on it by default.

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
WHERE "Ward" NOT IN ('LWEDTU','LWASW','LWDSW','LWVOTU','LOMOT','LCUCC')
  AND "Inflight_Date" >= '2024-01-01'
GROUP BY 1, 2 ORDER BY 1, 2;
```

## Join to admission / discharge

Use the candidate joins in `references/data-ontology.yaml` and validate counts for the requested period. For NGEMR-era joins prefer `PAT_ENC_CSN_ID` (see the `Case_No` caution above).

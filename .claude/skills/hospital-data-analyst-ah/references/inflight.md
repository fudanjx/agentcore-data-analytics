---
name: ah-analytics-inflight
description: Column reference and SQL guidance for the ah-analytics inflight table (Combined_inflight — daily inpatient census). Use when writing SQL against the inflight table, or when the user asks about bed occupancy, patient-days, average daily census, beds in use, occupancy rate, or ward utilisation at Alexandra Hospital. Each row represents one patient occupying one bed on one calendar date.
---

# AH Analytics — inflight table (daily inpatient census)

**One row = one patient in one bed on one date. Primary date: `inflight_date`.**
Use for occupancy and patient-days. Do NOT use for admissions/discharges — see `references/admission.md` / `references/discharge.md`.

## Two source systems, same table

Like admission and discharge, inflight rows come from two eras with different identifier coverage:

| Era | Identifier | `c` (→ `record_type`) | `diagnosis_code`/`diagnosis_desc` | `pri_diagnosis_code`/`sec_diagnosis_code` |
|---|---|---|---|---|
| Legacy SAP (before 1 Jan 2023) | `case_no` populated | Populated — single-letter SAP case-number suffix | Populated | Null |
| NGEMR/EPIC (from 1 Jan 2023) | `pat_enc_csn_id` populated | Mostly null | Mostly null | Populated (`Pri_Diagnosis_*` always; `Sec_Diagnosis_*` when a secondary diagnosis exists, pipe-delimited) |

**Caution — `case_no` is not a clean era switch here.** Unlike admission/discharge, `case_no` in inflight shows up populated for a large share of NGEMR-era rows too (same numeric format as the real SAP case number), not just the SAP era. Don't rely on `case_no` being present or absent to detect era or to join NGEMR-era rows — use `pat_enc_csn_id` as the identifier for NGEMR-era joins, same as admission/discharge.

`c` has the same ETL mismatch documented in `references/admission.md`: the loader renames raw column `c` to `record_type`, but it is really the SAP case-number suffix letter — concatenate `c` + `case_no` to get the full SAP case number, don't treat `record_type` as a category.

## ⚠️ Read this before answering any patient-days question

The production `pt_days_by_ward` report is **not** built from raw `inflight` alone. Patients admitted and discharged on the **same calendar date** never appear in a daily census snapshot. Production adds a synthetic one-row-per-case top-up sourced from `discharge` (same-day rows only, where `adm_date = disch_date`), joined to `admission` **on `pat_enc_csn_id`** for `disch_acmd_cat` (discharge's own copy of that field is blank and must be refilled from admission's), filtered using `discharge`'s own mandatory filters, with `los = 1` and `inflight_date = disch_date`. **`trt_cat` for these rows uses discharge's own copy (`d.trt_cat`), not admission's** — unlike `disch_acmd_cat`, discharge's `trt_cat` is populated and needs no backfill from admission.

Because the top-up join key is `pat_enc_csn_id`, it only reliably picks up **NGEMR-era** same-day cases — legacy SAP-era same-day discharges (`pat_enc_csn_id` null) won't match and are effectively excluded from the top-up.

**Querying raw `inflight` alone always undercounts patient-days** — this is a structural gap in the table itself, not a magnitude issue that only shows up for high-turnover wards. The same-day top-up union above is required for every patient-days query, regardless of ward or period, to take into account the same-day discharge cases; the gap is simply more visible in high-turnover wards because more rows are missing there.

Conceptual union to replicate production:

```sql
SELECT ward, inflight_date, cnt, accom_category, class FROM inflight
WHERE ward NOT IN ('LWEDTU','LWASW','LWDSW','LWVOTU','LOMOT','LCUCC')

UNION ALL

SELECT d.nrs_ou        AS ward,
       d.disch_date    AS inflight_date,
       d.cnt,
       COALESCE(ba.accom_category, a.disch_acmd_cat) AS accom_category,
       d.disch_class   AS class,
       d.trt_cat       AS trt_cat
FROM discharge d
JOIN admission a ON d.pat_enc_csn_id = a.pat_enc_csn_id
LEFT JOIN LATERAL (
  SELECT bo.accom_category
  FROM (SELECT DISTINCT bed, inflight_date, accom_category FROM inflight) bo
  WHERE bo.bed = d.disch_bed AND bo.inflight_date <= d.disch_date
  ORDER BY bo.inflight_date DESC
  LIMIT 1
) ba ON true
WHERE d.adm_date = d.disch_date
  AND d.adm_type IN ('EM','EL','SD','DI','TA','RA')
  AND d.nrs_ou NOT IN ('LWEDTU','LWASW','LWDSW','LWVOTU','LOMOT','LCUCC')
```

`accom_category` here now comes from the `bed_accom` last-recorded lookup (see
`admission.md`'s adm_acmd_cat / disch_acmd_cat correction) on `disch_bed`/`disch_date`,
falling back to admission's `disch_acmd_cat` only when that bed has no `inflight` history
at all before the discharge date -- this resolves the case that the exact-date join can't
reach (a same-day case never has an `inflight` row on its own date by construction), since
the bed will normally still have *earlier* `inflight` history to carry forward. The
`JOIN admission` is now only needed for the `disch_acmd_cat` fallback and `trt_cat`
sourcing note above -- it could be dropped if that fallback is ever deemed unnecessary, but
left in for now.

Add `AND prelim_flag = 'N'` (both the `inflight` side and the `d.`/discharge side) only if the user explicitly asks to exclude provisional/preliminary records — don't filter on it by default.

## Query baseline

Use the `inflight` filters and canonical date in `references/data-ontology.yaml`.

## Key columns

| Column | Type | Meaning |
|--------|------|---------|
| `case_no` | TEXT | SAP-era episode identifier. See caution above — populated for many NGEMR-era rows too; don't use it to detect era. |
| `pat_enc_csn_id` | TEXT | NGEMR encounter identifier — use as the primary join key for NGEMR-era rows. |
| `c` (→ `record_type` in the DB) | TEXT | SAP case-number suffix letter, not a record-type category — see note above. |
| `pat_name` | TEXT | Patient name — PII. |
| `ext_pat_id` | TEXT | NRIC/FIN-format patient identifier — PII. |
| `bed` | TEXT | bed code on census date, e.g. `L003035`. |
| `ward` | TEXT | ward code on this census date (apply exclusion here). |
| `dept_ou` | TEXT | Department code on census date. |
| `admit_date` | TIMESTAMP | Original admission date. |
| `inflight_date` | TIMESTAMP | Census snapshot date — primary date filter. |
| `los` | INTEGER | Days in hospital as of census date. |
| `attend_phy` | TEXT | Attending physician on this date. |
| `diagnosis_code` / `diagnosis_desc` | TEXT | SAP-era diagnosis code/description. Null in NGEMR era — use `pri_diagnosis_code`/`pri_diagnosis_desc` instead. |
| `pri_diagnosis_code` / `pri_diagnosis_desc` | TEXT | NGEMR-era primary diagnosis (ICD-10-style, e.g. `G95.9`). Null in SAP era. |
| `sec_diagnosis_code` / `sec_diagnosis_desc` | TEXT | NGEMR-era secondary diagnoses, pipe-delimited when there are multiple (e.g. `I10 \| B35.6 \| R63.4`). Null in SAP era and when there's no secondary diagnosis. |
| `age` | NUMERIC | Patient age. |
| `sex` | TEXT | `M` / `F`. |
| `trt_cat` | TEXT | Treatment category, e.g. `CL3`, `B2L3`, `HDC`, `CCUC`. |
| `class` | TEXT | Raw patient class code — resolve through `pt_class_abc` (see `references/pt-class-lookup.md`) to get `class_abc`. |
| `accom_category` | TEXT | Actual accommodation type on this census date. |
| `adm_type` | TEXT | Original admission route. |
| `prelim_flag` | TEXT | `N` = finalised, `Y` = preliminary. **Don't filter on this by default** — only add `WHERE prelim_flag = 'N'` when the user explicitly asks to exclude provisional records (see `data-ontology.yaml` global rule). |
| `cnt` | INTEGER | Always 1 — represents one patient-day. |

## Critical: the ICU/HD/ISO override chain

The production `class_with_icu_iso` field is **not** `accom_category` with a `class` fallback — it's the looked-up `class_abc` (from `class` via `pt_class_abc`, see `references/pt-class-lookup.md`) as the base, with ISO/ICU/HD as overrides:

```sql
CASE
  WHEN accom_category = 'ISO'   THEN 'ISO'
  WHEN LEFT(trt_cat, 3) = 'CCU' THEN 'ICU'
  WHEN LEFT(trt_cat, 2) = 'HD'  THEN 'HD'
  ELSE class_abc   -- from pt_class_abc lookup on raw class, NOT accom_category
END AS effective_class
```

Note this differs from the discharge table's override chain (which checks `nrs_ou` ward prefix `LW9`/`LW8` for ISO instead of `accom_category`) — don't reuse discharge's chain for inflight.

## ward / bed acuity (trt_cat → Acuity)

`trt_cat` encodes accommodation class *and* acuity level in one code (e.g. `CL3` = class C, Level 3; `HDC` = High Dependency, class C — not `<prefix>L<n>`). To get just the acuity level (`L1`/`L2`/`L3`/`EDTU`) for "how acute is this ward" or "acuity mix" questions, resolve `trt_cat` through the mapping below (sourced from `Class.xlsx`'s `Acuity` sheet, confirmed to cover every `trt_cat` value seen in the real sample data) — don't string-parse the trailing digit, several codes (`CCUC`, `HDC`, `EDTUB2`, `EDTVS`) don't follow that pattern.

**Note:** this mapping is specific to `inflight`'s `trt_cat`. The `trt_cat` column in `outpatient` is a different, unrelated code set (treatment category, e.g. `NC`) — don't cross-apply.

| trt_cat | Acuity | trt_cat | Acuity | trt_cat | Acuity |
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
CASE trt_cat
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
  ELSE NULL  -- unmapped trt_cat -- investigate before reporting
END AS acuity

-- Example: acuity mix by ward for a period
SELECT ward,
       CASE trt_cat ... END AS acuity,
       SUM("cnt") AS patient_days
FROM inflight
WHERE ward NOT IN ('LWEDTU','LWASW','LWDSW','LWVOTU','LOMOT','LCUCC')
  AND inflight_date BETWEEN '2024-01-01' AND '2024-12-31'
GROUP BY 1, 2 ORDER BY 1, 2;
```

## Counting patterns

```sql
-- Total patient-days in a period
SELECT SUM("cnt") AS patient_days
FROM inflight
WHERE ward NOT IN ('LWEDTU','LWASW','LWDSW','LWVOTU','LOMOT','LCUCC')
  AND inflight_date BETWEEN '2024-01-01' AND '2024-12-31';

-- Average daily census by month
SELECT
  DATE_TRUNC('month', inflight_date) AS month,
  ROUND(COUNT(*)::NUMERIC / COUNT(DISTINCT inflight_date), 1) AS avg_daily_census
FROM inflight
WHERE ward NOT IN ('LWEDTU','LWASW','LWDSW','LWVOTU','LOMOT','LCUCC')
GROUP BY 1 ORDER BY 1;
```

Add `AND prelim_flag = 'N'` only if the user explicitly asks to exclude provisional/preliminary records — don't filter on it by default.

## Lodger identification

Patient whose accommodation class differs from their entitled class. Production first backfills blank/`OTHER` `accom_category` from the ward's default class below, then compares against the **looked-up** `class_abc` (not the raw `class` code):

| ward | ward_cls |
|---|---|
| `LW2W` | `C` |
| `LW3W` | `C` |
| `LW4W` | `B2` |
| `LW5W` | `B2` |
| `LW7W` | `B1` |
| `LW8ISO` | `ISO` |
| `LW9W` | `ISO` |
| `LW10W` | `B2` |
| `LW11W` | `C` |
| `LW12W` | `B2` |
| `LW13W` | `C` |
| `LWASW` | `ASW` |
| `LWEDTU` | `EDTU` |
| `LWICU1` | `ICU` |
| `LWICU2` | `ICU` |

```sql
WHERE accom_category IN ('A1','B1','B2')
  AND class_abc IN ('B1','B2','C')   -- from pt_class_abc lookup on raw class
  AND accom_category != class_abc
```

## Example: monthly patient-days by class

```sql
SELECT
  DATE_TRUNC('month', inflight_date) AS month,
  CASE WHEN accom_category = 'OTHER' THEN class ELSE accom_category END AS bed_class,
  SUM("cnt") AS patient_days
FROM inflight
WHERE ward NOT IN ('LWEDTU','LWASW','LWDSW','LWVOTU','LOMOT','LCUCC')
  AND inflight_date >= '2024-01-01'
GROUP BY 1, 2 ORDER BY 1, 2;
```

## Join to admission / discharge

Use the candidate joins in `references/data-ontology.yaml` and validate counts for the requested period. For NGEMR-era joins prefer `pat_enc_csn_id` (see the `case_no` caution above).

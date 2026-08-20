---
name: ah-analytics-inflight
description: Column reference and SQL guidance for the ah-analytics inflight table (Combined_inflight — daily inpatient census). Use when writing SQL against the inflight table, or when the user asks about bed occupancy, patient-days, average daily census, beds in use, occupancy rate, or ward utilisation at Alexandra Hospital. Each row represents one patient occupying one bed on one calendar date.
---

# AH Analytics — inflight table (daily inpatient census)

**One row = one patient in one bed on one date. Primary date: `Inflight_Date`.**
Use for occupancy and patient-days. Do NOT use for admissions/discharges.

## ⚠️ Read this before answering any patient-days question

The production `pt_days_by_ward` report is **not** built from raw `inflight` alone. Patients admitted and discharged on the **same calendar date** never appear in a daily census snapshot. Production adds a synthetic one-row-per-case top-up sourced from `discharge` (joined to `admission` for `Adm_Date` and `Adm_Acmd_Cat`), filtered using `discharge`'s own mandatory filters, with `LOS = 1` and `Inflight_Date = Disch_Date`.

**Querying raw `inflight` alone undercounts patient-days**, especially for wards with high same-day turnover.

Conceptual union to replicate production:

```sql
SELECT "Ward", "Inflight_Date", "cnt", "Accom_Category", "Class" FROM inflight
WHERE "prelim_flag" = 'N'
  AND "Ward" NOT IN ('LWEDTU','LWASW','LWDSW','LWVOTU','LOMOT','LCUCC')

UNION ALL

SELECT d."Nrs_OU"        AS "Ward",
       d."Disch_Date"    AS "Inflight_Date",
       d."cnt",
       a."Adm_Acmd_Cat"  AS "Accom_Category",
       d."Disch_Class"   AS "Class"
FROM discharge d
JOIN admission a ON d."Case_No" = a."Case_No"
WHERE a."Adm_Date" = d."Disch_Date"
  AND d."prelim_flag" = 'N'
  AND d."Adm_Type" IN ('EM','EL','SD','DI','TA','RA')
  AND d."Nrs_OU" NOT IN ('LWEDTU','LWASW','LWDSW','LWVOTU','LOMOT','LCUCC')
```

## Mandatory WHERE filters

```sql
WHERE "prelim_flag" = 'N'
  AND "Ward" NOT IN ('LWEDTU','LWASW','LWDSW','LWVOTU','LOMOT','LCUCC')
```

## Key columns

| Column | Type | Meaning |
|--------|------|---------|
| `Case_No` | TEXT | Episode identifier — join to `admission`/`discharge`. Blank for new encounters from Feb 2026 onward. |
| `Inflight_Date` | TIMESTAMP | Census snapshot date — primary date filter |
| `Admit_Date` | TIMESTAMP | Original admission date |
| `Ward` | TEXT | Ward code on this census date (apply exclusion here) |
| `Bed` | TEXT | Bed code |
| `Dept_OU` | TEXT | Department code on census date |
| `Trt_Cat` | TEXT | Treatment category |
| `Class` | TEXT | Raw patient class code — resolve through `pt_class_abc` (see `references/pt-class-lookup.md`) |
| `Accom_Category` | TEXT | Actual accommodation type — see `OTHER` warning below |
| `Age` | TEXT | Patient age |
| `Sex` | TEXT | `M` / `F` |
| `Attend_Phy` | TEXT | Attending physician on this date |
| `Adm_Type` | TEXT | Original admission route |
| `LOS` | TEXT | Days in hospital as of census date |
| `PAT_ENC_CSN_ID` | TEXT | NGEMR encounter ID. Null before 2023-01-01. |
| `cnt` | INTEGER | Always 1 — represents one patient-day |

## Critical: Accom_Category = 'OTHER' and the ICU/HD/ISO override chain

Never use `Accom_Category = 'OTHER'` for class analysis. Fall back to `Class`, then apply the override chain:

```sql
CASE
  WHEN "Accom_Category" = 'ISO'             THEN 'ISO'
  WHEN LEFT("Trt_Cat", 3) = 'CCU'           THEN 'ICU'
  WHEN LEFT("Trt_Cat", 2) = 'HD'            THEN 'HD'
  WHEN "Accom_Category" = 'OTHER'           THEN "Class"
  ELSE "Accom_Category"
END AS effective_class
```

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

Patient whose accommodation class differs from entitled class:

```sql
WHERE "Accom_Category" IN ('A1','B1','B2')
  AND "Class" IN ('B1','B2','C')
  AND "Accom_Category" != "Class"
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

## Join to admission

```sql
FROM inflight i JOIN admission a ON i."Case_No" = a."Case_No"
```

⚠️ For pre-2023 data use `Case_No`; for post-Feb-2026 data use `PAT_ENC_CSN_ID`. See SKILL.md for the full era rule.

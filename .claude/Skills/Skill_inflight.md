---
name: ah-analytics-inflight
description: Column reference and SQL guidance for the ah-analytics inflight table (Combined_inflight — daily inpatient census). Use when writing SQL against the inflight table, or when the user asks about bed occupancy, patient-days, average daily census, beds in use, occupancy rate, or ward utilisation at Alexandra Hospital. Each row represents one patient occupying one bed on one calendar date.
---

# AH Analytics — inflight table (daily inpatient census)

**One row = one patient in one bed on one date. Primary date: `Inflight_Date`.**
Use for occupancy and patient-days. Do NOT use for admissions/discharges.

## Mandatory WHERE filters

```sql
WHERE "prelim_flag" = 'N'
  AND "Ward" NOT IN ('LWEDTU','LWASW','LWDSW','LWVOTU','LOMOT','LCUCC')
```

## Key columns

| Column | Type | Meaning |
|--------|------|---------|
| `Case_No` | TEXT | Episode identifier — join to `admission` / `discharge` |
| `Inflight_Date` | TIMESTAMP | Census snapshot date — primary date filter |
| `Admit_Date` | TIMESTAMP | Original admission date |
| `Ward` | TEXT | Ward code on this census date (apply exclusion here) |
| `Bed` | TEXT | Bed code |
| `Dept_OU` | TEXT | Department code on census date |
| `Trt_Cat` | TEXT | Treatment category |
| `Class` | TEXT | Patient's entitled class |
| `Accom_Category` | TEXT | Actual accommodation — see warning below |
| `Age` | TEXT | Patient age |
| `Sex` | TEXT | `M` / `F` |
| `Attend_Phy` | TEXT | Attending physician name on this date |
| `Diagnosis_Code` | TEXT | Primary diagnosis code |
| `Adm_Type` | TEXT | Original admission route |
| `LOS` | TEXT | Days in hospital as of census date |
| `PAT_ENC_CSN_ID` | TEXT | NGEMR encounter ID |
| `cnt` | INTEGER | Always 1 — represents one patient-day |

## Critical: Accom_Category = 'OTHER'

Never use `Accom_Category = 'OTHER'` for class analysis. Fall back to `Class`:

```sql
CASE
  WHEN "Accom_Category" = 'OTHER' THEN "Class"
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

-- Census on a specific date
SELECT COUNT(DISTINCT "Case_No") AS census
FROM inflight
WHERE "Inflight_Date" = '2024-06-30'
  AND "Ward" NOT IN ('LWEDTU','LWASW','LWDSW','LWVOTU','LOMOT','LCUCC')
  AND "prelim_flag" = 'N';

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

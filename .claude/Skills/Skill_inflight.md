---
name: ah-analytics-inflight
description: Column reference and SQL guidance for the ah-analytics inflight table (Combined_inflight — daily inpatient census). Use when writing SQL against the inflight table, or when the user asks about bed occupancy, patient-days, average daily census, beds in use, occupancy rate, or ward utilisation at Alexandra Hospital. Each row represents one patient occupying one bed on one calendar date.
---

# AH Analytics — inflight table (daily inpatient census)

**One row = one patient in one bed on one date. Primary date: `Inflight_Date`.**
Use for occupancy and patient-days. Do NOT use for admissions/discharges.

## ⚠️ Read this before answering any patient-days question

The production `pt_days_by_ward` report (and every other patient-days figure) is **not** built from raw `inflight` alone. Patients admitted and discharged on the **same calendar date** never appear in a daily census snapshot, so production adds a synthetic one-row-per-case top-up sourced from the `admission`/`discharge` tables (`Admit_Date == Disch_Date`), with `LOS = 1` and `Inflight_Date = Disch_Date`. **If you only query the raw `inflight` table, you will undercount patient-days**, especially for wards/specialties with a lot of same-day admit-and-discharge turnover (e.g. day-surgery-turned-inpatient cases).

To replicate this, conceptually union:
1. `inflight` rows, plus
2. one synthetic row per `admission`/`discharge` episode where `Adm_Date = Disch_Date`, using: `Ward = Nrs_OU (discharge)`, `Dept_OU = Disch_Dept_OU`, `LOS = 1`, `Class = Disch_Cls`, `Accom_Category = Adm_Acmd_Cat`, `Inflight_Date = Disch_Date`.

```sql
-- Conceptual union (adapt to your engine)
SELECT "Ward", "Inflight_Date", "cnt", "Accom_Category", "Class" FROM inflight
UNION ALL
SELECT d."Nrs_OU" AS "Ward", d."Disch_Date" AS "Inflight_Date", d."cnt",
       a."Adm_Acmd_Cat" AS "Accom_Category", d."Disch_Class" AS "Class"
FROM discharge d
JOIN admission a ON d."Case_No" = a."Case_No"
WHERE a."Adm_Date" = d."Disch_Date"
```

## Mandatory WHERE filters

```sql
WHERE "prelim_flag" = 'N'
  AND "Ward" NOT IN ('LWEDTU','LWASW','LWDSW','LWVOTU','LOMOT','LCUCC')
```

## Key columns

| Column | Type | Meaning |
|--------|------|---------|
| `Case_No` | TEXT | Episode identifier — join to `admission` / `discharge`. **Blank for new encounters created from Feb 2026 (NBS go-live) onward** — use `PAT_ENC_CSN_ID`. |
| `Inflight_Date` | TIMESTAMP | Census snapshot date — primary date filter |
| `Admit_Date` | TIMESTAMP | Original admission date |
| `Ward` | TEXT | Ward code on this census date (apply exclusion here) |
| `Bed` | TEXT | Bed code |
| `Dept_OU` | TEXT | Department code on census date |
| `Trt_Cat` | TEXT | Treatment category |
| `Class` | TEXT | Raw patient class code — not resolved through the lookup below. For usual reporting, use the derived patient class (`Class_abc`/`Class_abc_MOH`) instead. |
| `Accom_Category` | TEXT | Actual accommodation — see warning below |
| `Age` | TEXT | Patient age |
| `Sex` | TEXT | `M` / `F` |
| `Attend_Phy` | TEXT | Attending physician name on this date |
| `Diagnosis_Code` | TEXT | Primary diagnosis code |
| `Adm_Type` | TEXT | Original admission route |
| `LOS` | TEXT | Days in hospital as of census date |
| `PAT_ENC_CSN_ID` | TEXT | NGEMR encounter ID. **Null before 2023-01-01** (SAP era); use `Case_No` for pre-2023 data. |
| `cnt` | INTEGER | Always 1 — represents one patient-day |

## Patient class & residency lookup (`pt_class_abc` — shared across all tables)

`Class` resolves through the same `pt_class_abc` lookup table used by `admission` (`Adm_Cls`), `discharge` (`Disch_Class`), `procedure` (`Cls`), and `outpatient` (`Class`) — see `Skill_outpatient.md` for the full table and rationale (financial `Class_abc` vs. MOH-facing `Class_abc_MOH`, which reclassifies foreigner `*RF` codes up to `A1`). Reproduced here for `inflight`'s `Class`:

| Raw `Class` | `Class_abc` | `Class_abc_MOH` | `Resident_Type` | `Resident_MOH` |
|---|---|---|---|---|
| `A` | `A1` | `A1` | `SG` | `SG` |
| `AP` | `A1` | `A1` | `PR` | `PR` |
| `ARF` | `A1` | `A1` | `RF` | `FR` |
| `B1` | `B1` | `B1` | `SG` | `SG` |
| `B1P` | `B1` | `B1` | `PR` | `PR` |
| `B1RF` | `B1` | `A1` | `RF` | `FR` |
| `B2` | `B2` | `B2` | `SG` | `SG` |
| `B2P` | `B2` | `B2` | `PR` | `PR` |
| `B2RF` | `B2` | `A1` | `RF` | `FR` |
| `C` | `C` | `C` | `SG` | `SG` |
| `CP` | `C` | `C` | `PR` | `PR` |
| `CRF` | `C` | `A1` | `RF` | `FR` |
| `NR` | `A1` | `A1` | `NR` | `FNR` |
| `PTE` | `Private` | `Private` | `SG` | `SG` |
| `PTEP` | `Private` | `Private` | `PR` | `PR` |
| `PTRF` | `Private` | `Private` | `RF` | `FR` |
| `SUB` | `Subsidized` | `Subsidized` | `SG` | `SG` |
| `SUBP` | `Subsidized` | `Subsidized` | `PR` | `PR` |

## Critical: Accom_Category = 'OTHER' and the ICU/HD/ISO override chain

Never use `Accom_Category = 'OTHER'` for class analysis. Fall back to `Class`:

```sql
CASE
  WHEN "Accom_Category" = 'ISO' THEN 'ISO'
  WHEN LEFT("Trt_Cat", 3) = 'CCU' THEN 'ICU'
  WHEN LEFT("Trt_Cat", 2) = 'HD' THEN 'HD'
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

⚠️ For dates before 2023-01-01, `PAT_ENC_CSN_ID` will be null — fall back to `Case_No`. For encounters created from Feb 2026 (NBS go-live) onward, `Case_No` may be blank — fall back to `PAT_ENC_CSN_ID`. See `SKILL.md` for the full explanation.


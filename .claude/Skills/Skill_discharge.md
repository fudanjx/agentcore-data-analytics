---
name: ah-analytics-discharge
description: Column reference and SQL guidance for the ah-analytics discharge table (Combined_disch — inpatient discharges). Use when writing SQL against the discharge table, or when the user asks about length of stay, discharge disposition, in-hospital mortality, death rate, readmissions, or patient outcomes after inpatient admission at Alexandra Hospital.
---

# AH Analytics — discharge table (inpatient discharges)

**One row per episode. Primary date: `Disch_Date`.**
Use this table (not `admission`) for outcome questions: LOS, death, discharge destination.

## Mandatory WHERE filters

```sql
WHERE "prelim_flag" = 'N'
  AND "Adm_Type" IN ('EM','EL','SD','DI','TA','RA')
  AND "Nrs_OU" NOT IN ('LWEDTU','LWASW','LWDSW','LWVOTU','LOMOT','LCUCC')
```

## Key columns

| Column | Type | Meaning |
|--------|------|---------|
| `Case_No` | TEXT | Episode identifier — join to `admission`, `inflight`, `procedure`. **Blank for new encounters created from Feb 2026 (NBS go-live) onward** — use `PAT_ENC_CSN_ID` for those. |
| `Adm_Date` | TIMESTAMP | Admission date |
| `Adm_Time` | TIME | Admission time |
| `Disch_Date` | TIMESTAMP | Discharge date — primary date filter |
| `Disch_Time` | TIME | Discharge time |
| `Adm_Type` | TEXT | Admission route (same codes as `admission` table) |
| `Adm_Class` | TEXT | Raw patient class code at admission — not resolved through the lookup below. For usual reporting, use the derived patient class (`Class_abc`/`Class_abc_MOH`) instead. |
| `Disch_Class` | TEXT | Raw patient class code at discharge — not resolved through the lookup below. For usual reporting, use the derived patient class (`Class_abc`/`Class_abc_MOH`) instead.  |
| `Nrs_OU` | TEXT | Discharging ward code (apply exclusion filter here) |
| `Disch_Dept_OU_Text` | TEXT | Discharging department name |
| `Discharge_Type_Text` | TEXT | Discharge disposition — see values below |
| `Discharge_w_in_24_hrs` | TEXT | `X` if discharged within 24 hours |
| `LOS` | TEXT | Length of stay in days — cast to NUMERIC for maths |
| `Death_Date` | TIMESTAMP | Date of death (null if survived) |
| `Death_Time` | TIME | Time of death |
| `Pri_Diag_Code` | TEXT | Principal discharge diagnosis ICD code |
| `Other_Diag_Code` | TEXT | Additional diagnoses (pipe `\|` separated) |
| `DRG_Code` | TEXT | DRG code |
| `Attending_Physician_Name` | TEXT | Attending physician name |
| `Age` | TEXT | Age at discharge |
| `PAT_ENC_CSN_ID` | TEXT | NGEMR encounter ID. **Null before 2023-01-01** (SAP era); use `Case_No` instead for pre-2023 data. |
| `cnt` | INTEGER | Always 1 |

## Discharge_Type_Text values

| Value | Meaning |
|-------|---------|
| `Discharged Home` | Standard discharge |
| `Transfer to Community Hospital` | Step-down care |
| `Transfer to Restructured Hospital` | Another acute hospital |
| `Discharge Against Advice` / `AOR` | Left AMA |
| `Death` / `Death in Hospital` | In-hospital death |
| `Discharged to Hospice` | Palliative |

## LOS calculations

```sql
-- Average LOS
AVG(CAST("LOS" AS NUMERIC)) AS avg_los

-- Median LOS
PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY CAST("LOS" AS NUMERIC)) AS median_los
```

## Death flag

```sql
CASE WHEN "Discharge_Type_Text" ILIKE 'Death%' THEN 1 ELSE 0 END AS death_flag

-- Mortality rate
SUM(CASE WHEN "Discharge_Type_Text" ILIKE 'Death%' THEN 1 ELSE 0 END)::FLOAT
  / COUNT(*) * 100 AS mortality_pct
```

## Patient class & residency lookup (`pt_class_abc` — shared across all tables)

`Disch_Class` resolves through the same `pt_class_abc` lookup table used by `admission` (`Adm_Cls`), `inflight` (`Class`), `procedure` (`Cls`), and `outpatient` (`Class`) — see `Skill_outpatient.md` for the full table and rationale (financial `Class_abc` vs. MOH-facing `Class_abc_MOH`, which reclassifies foreigner `*RF` codes up to `A1`). Reproduced here for `Disch_Class` / `Adm_Class`

| Raw `Disch_Class` / `Adm_Class`| `Class_abc` | `Class_abc_MOH` | `Resident_Type` | `Resident_MOH` |
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

This feeds two derived discharge fields used across the finance/MOH reports:
- **`Class_abc`** → used directly by `fin_disch_class_abc`/`fin_disch_resident` (financial workload reports).
- **`cls_icu_iso_fin`** (from `Class_abc`) and **`cls_icu_iso_MOH`** (from `Class_abc_MOH`) → each further overridden to `'ISO'` if `Nrs_OU` starts with `LW9`/`LW8`, to `'HD'` if `Trt_Cat` starts with `HD`, or to `'ICU'` if `Trt_Cat` starts with `CCU` — this is what actually feeds `MOH_F09_Disch` and `Fin_disch_iso_icu`, not raw `Class_abc`/`Class_abc_MOH` alone.

```sql
CASE
  WHEN LEFT("Nrs_OU", 3) IN ('LW9','LW8') THEN 'ISO'
  WHEN LEFT("Trt_Cat", 2) = 'HD' THEN 'HD'
  WHEN LEFT("Trt_Cat", 3) = 'CCU' THEN 'ICU'
  ELSE "Class_abc"          -- or "Class_abc_MOH" for the MOH-facing version
END AS cls_icu_iso
```

## Same-day discharge

```sql
WHERE "Discharge_w_in_24_hrs" = 'X'
```

## Example: average LOS by department

```sql
SELECT
  "Disch_Dept_OU_Text",
  COUNT(*) AS discharges,
  ROUND(AVG(CAST("LOS" AS NUMERIC)), 1) AS avg_los
FROM discharge
WHERE "prelim_flag" = 'N'
  AND "Adm_Type" IN ('EM','EL','SD','DI','TA','RA')
  AND "Nrs_OU" NOT IN ('LWEDTU','LWASW','LWDSW','LWVOTU','LOMOT','LCUCC')
  AND "Disch_Date" >= '2024-01-01'
GROUP BY 1 ORDER BY avg_los DESC;
```

## Joins

```sql
FROM discharge d JOIN admission a ON d."Case_No" = a."Case_No"
FROM discharge d JOIN inflight i  ON d."Case_No" = i."Case_No"
```

⚠️ For dates before 2023-01-01, `PAT_ENC_CSN_ID` will be null — fall back to `Case_No`. For encounters created from Feb 2026 (NBS go-live) onward, `Case_No` may be blank — fall back to `PAT_ENC_CSN_ID`. See `SKILL.md` for the full explanation.

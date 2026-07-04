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
| `Case_No` | TEXT | Episode identifier — join to `admission`, `inflight`, `procedure` |
| `Adm_Date` | TIMESTAMP | Admission date |
| `Adm_Time` | TIME | Admission time |
| `Disch_Date` | TIMESTAMP | Discharge date — primary date filter |
| `Disch_Time` | TIME | Discharge time |
| `Adm_Type` | TEXT | Admission route (same codes as `admission` table) |
| `Adm_Class` | TEXT | Patient class at admission |
| `Disch_Class` | TEXT | Patient class at discharge |
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
| `PAT_ENC_CSN_ID` | TEXT | NGEMR encounter ID |
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

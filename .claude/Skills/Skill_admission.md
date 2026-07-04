---
name: ah-analytics-admission
description: Column reference and SQL guidance for the ah-analytics admission table (Combined_adm — inpatient admissions). Use when writing SQL against the admission table, or when the user asks about admission volume, emergency vs elective admissions, admission source, admission ward, patient class at admission, or inpatient admission trends at Alexandra Hospital.
---

# AH Analytics — admission table (inpatient admissions)

**One row per episode. Primary date: `Adm_Date`.**

## Mandatory WHERE filters

```sql
WHERE "prelim_flag" = 'N'
  AND "Adm_Status" != 'P'
  AND "Adm_Type" IN ('EM','EL','SD','DI','TA','RA')
  AND "Adm_Nrs_OU" NOT IN ('LWEDTU','LWASW','LWDSW','LWVOTU','LOMOT')
```

## Key columns

| Column | Type | Meaning |
|--------|------|---------|
| `Case_No` | TEXT | Episode identifier — join key to `discharge`, `inflight`, `procedure` |
| `Adm_Date` | TIMESTAMP | Admission date — primary date filter |
| `Adm_Time` | TIME | Admission time |
| `Adm_Type` | TEXT | Admission route — see mapping below |
| `Adm_Nrs_OU` | TEXT | Admitting ward code (apply exclusion filter here) |
| `Adm_Dept_OU` | TEXT | Admitting department code |
| `Adm_Cls` | TEXT | Patient class at admission (`A`, `B1`, `B2`, `C`) |
| `Adm_Acmd_Cat` | TEXT | Accommodation category (`ICU`, `HD`, `ISO`, `A1`, `B1`, `B2`, `C`) |
| `Adm_Trt_Cat` | TEXT | Treatment category code |
| `Wish_Cls` | TEXT | Patient's requested class |
| `Disch_Date` | TIMESTAMP | Discharge date (forward reference; null for current inpatients) |
| `Diagnosis_Code` | TEXT | Principal diagnosis ICD code |
| `Prin_Diagnosis_Code` | TEXT | NGEMR refined principal diagnosis code |
| `DRG_Code` | TEXT | DRG code |
| `Adm_Reason` | TEXT | Reason for admission (`SOC`, `A&E`, `Others`) |
| `Ref_Hosp_1` | TEXT | Referring source code |
| `Attn_Phy_Name` | TEXT | Attending physician name |
| `Age` | TEXT | Patient age — cast to INT for ranges |
| `Sex` | TEXT | `M` / `F` |
| `Birthdate` | TIMESTAMP | Date of birth |
| `PAT_ENC_CSN_ID` | TEXT | NGEMR encounter ID — join to `urgentcarecenter` |
| `cnt` | INTEGER | Always 1 |

## Adm_Type codes

| Code | Meaning | MOH Class |
|------|---------|-----------|
| `EM` | Emergency (via A&E/UCC) | Emergency |
| `EL` | Elective (planned) | Elective |
| `SD` | Same-day | Elective |
| `DI` | Direct admit from clinic/GP | Emergency or Elective |
| `TA` | Transfer in from another hospital | — |
| `RA` | Readmission | — |

## Patient class SQL

```sql
CASE
  WHEN "Adm_Cls" IN ('B2','B2P','C') THEN 'Subsidised'
  ELSE 'Paying'
END AS paying_status
```

## Ward exclusions

| Code | Description |
|------|-------------|
| `LWEDTU` | Emergency Dept Treatment Unit |
| `LWASW` | Ambulatory Surgery Ward |
| `LWDSW` | Day Surgery Ward |
| `LWVOTU` | VOTU |
| `LOMOT` | Main OT holding |

## Example: monthly admissions by type

```sql
SELECT
  DATE_TRUNC('month', "Adm_Date") AS month,
  CASE WHEN "Adm_Type" = 'EM' THEN 'Emergency' ELSE 'Elective' END AS adm_category,
  COUNT(*) AS admissions
FROM admission
WHERE "prelim_flag" = 'N'
  AND "Adm_Status" != 'P'
  AND "Adm_Type" IN ('EM','EL','SD','DI','TA','RA')
  AND "Adm_Nrs_OU" NOT IN ('LWEDTU','LWASW','LWDSW','LWVOTU','LOMOT')
  AND "Adm_Date" >= '2024-01-01'
GROUP BY 1, 2 ORDER BY 1;
```

## Joins

```sql
-- To discharge (1:1)
FROM admission a JOIN discharge d ON a."Case_No" = d."Case_No"

-- To inflight (1:many — patient-days)
FROM admission a JOIN inflight i ON a."Case_No" = i."Case_No"

-- From urgentcarecenter (ED → admission pathway)
FROM urgentcarecenter u JOIN admission a ON u."PAT_ENC_CSN_ID" = a."PAT_ENC_CSN_ID"
```

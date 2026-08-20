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
  AND (
    CASE
      WHEN LEFT("Adm_Nrs_OU", 2) = 'LW'
           AND "Adm_Nrs_OU" NOT IN ('LWEDTU','LWASW','LWDSW','LWVOTU')
        THEN "Adm_Nrs_OU"
      ELSE "Current_Ward"
    END
  ) NOT IN ('LWEDTU','LWASW','LWDSW','LWVOTU','LOMOT')
```

## Adm_Ward — derived field used for ward reporting

Ward-level admission reports do **not** group by raw `Adm_Nrs_OU`. Production derives `Adm_Ward` first:

```
Adm_Ward = Current_Ward,  UNLESS Adm_Nrs_OU starts with "LW"
           AND Adm_Nrs_OU NOT IN ('LWEDTU','LWASW','LWDSW','LWVOTU')
           → then Adm_Ward = Adm_Nrs_OU
```

The final exclusion filter (`NOT IN ('LWEDTU','LWASW','LWDSW','LWVOTU','LOMOT')`) is applied to this **derived** `Adm_Ward`, not to raw `Adm_Nrs_OU`.

## Key columns

| Column | Type | Meaning |
|--------|------|---------|
| `Case_No` | TEXT | Episode identifier — join key to `discharge`, `inflight`, `procedure`. Blank for new encounters from Feb 2026 onward. |
| `Adm_Date` | TIMESTAMP | Admission date — primary date filter |
| `Adm_Time` | TIME | Admission time |
| `Adm_Type` | TEXT | Admission route — see mapping below |
| `Adm_Nrs_OU` | TEXT | Raw admitting ward code — do not use directly for ward reporting; see `Adm_Ward` derivation above |
| `Current_Ward` | TEXT | Patient's current/latest ward — fallback in `Adm_Ward` derivation |
| `Adm_Dept_OU` | TEXT | Admitting department code |
| `Adm_Cls` | TEXT | Raw patient class code at admission — resolve through `pt_class_abc` (see `references/pt-class-lookup.md`) |
| `Adm_Acmd_Cat` | TEXT | Accommodation category (`ICU`, `HD`, `ISO`, `A1`, `B1`, `B2`, `C`) |
| `Adm_Trt_Cat` | TEXT | Treatment category code |
| `Wish_Cls` | TEXT | Patient's requested class |
| `Disch_Date` | TIMESTAMP | Discharge date (null for current inpatients) |
| `Diagnosis_Code` | TEXT | Principal diagnosis ICD code |
| `Prin_Diagnosis_Code` | TEXT | NGEMR refined principal diagnosis code |
| `DRG_Code` | TEXT | DRG code |
| `Adm_Reason` | TEXT | Reason for admission (`SOC`, `A&E`, `Others`) |
| `Ref_Hosp_1` | TEXT | Referring source code |
| `Attn_Phy_Name` | TEXT | Attending physician name |
| `Age` | TEXT | Patient age — cast to INT for ranges |
| `Sex` | TEXT | `M` / `F` |
| `PAT_ENC_CSN_ID` | TEXT | NGEMR encounter ID — join to `urgentcarecenter`. Null before 2023-01-01. |
| `cnt` | INTEGER | Always 1 |

## Adm_Type codes

| Code | Meaning |
|------|---------|
| `EM` | Emergency (via A&E/UCC) |
| `EL` | Elective (planned) |
| `SD` | Same-day |
| `DI` | Direct admit from clinic/GP |
| `TA` | Transfer in from another hospital |
| `RA` | Readmission |

## Ward exclusions

| Code | Description |
|------|-------------|
| `LWEDTU` | Emergency Dept Treatment Unit |
| `LWASW` | Ambulatory Surgery Ward |
| `LWDSW` | Day Surgery Ward |
| `LWVOTU` | VOTU |
| `LOMOT` | Main OT holding |

## Patient class

Resolve `Adm_Cls` through `pt_class_abc` (see `references/pt-class-lookup.md`). Quick paying-status split:

```sql
CASE
  WHEN "Adm_Cls" IN ('B2','B2P','C') THEN 'Subsidised'
  ELSE 'Paying'
END AS paying_status
```

## Example: monthly admissions by ward (replicates `adm_by_ward`)

```sql
WITH adm_ward AS (
  SELECT *,
    CASE
      WHEN LEFT("Adm_Nrs_OU", 2) = 'LW'
           AND "Adm_Nrs_OU" NOT IN ('LWEDTU','LWASW','LWDSW','LWVOTU')
        THEN "Adm_Nrs_OU"
      ELSE "Current_Ward"
    END AS "Adm_Ward"
  FROM admission
  WHERE "prelim_flag" = 'N'
    AND "Adm_Status" != 'P'
    AND "Adm_Type" IN ('EM','EL','SD','DI','TA','RA')
)
SELECT
  DATE_TRUNC('month', "Adm_Date") AS month,
  "Adm_Ward",
  COUNT(*) AS admissions
FROM adm_ward
WHERE "Adm_Ward" NOT IN ('LWEDTU','LWASW','LWDSW','LWVOTU','LOMOT')
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

⚠️ For pre-2023 data use `Case_No`/`SAP_IP_CASE_NO`; for post-Feb-2026 data use `PAT_ENC_CSN_ID`. See SKILL.md for the full era rule.

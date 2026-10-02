---
name: ah-analytics-outpatient
description: Column reference and SQL guidance for the ah-analytics outpatient table (Combined_SOC — Specialist Outpatient Clinic visits). Use when writing SQL against the outpatient table, or when the user asks about SOC visits, clinic appointments, new vs repeat patients, first visit rates, or telehealth attendance at Alexandra Hospital.
---

# AH Analytics — outpatient table (SOC visits)

**One row per appointment. Primary date: `Visit_Date`.**

## Query baseline

Use the `outpatient` filters and canonical date in `references/data-ontology.yaml`.

## Key columns

| Column | Type | Meaning |
|--------|------|---------|
| `Case_No` | TEXT | Episode identifier; see the ontology for candidate joins and completeness cautions. |
| `Visit_Date` | TIMESTAMP | Date visit occurred — primary date filter |
| `Visit_Time` | TIME | Actual visit time |
| `APPT_TIME` | TIME | Scheduled appointment time |
| `Visit_Type` | TEXT | Visit classification — see mapping below |
| `APPT_STATUS` | TEXT | Lifecycle status (`Completed`, `Arrived`, `Cancelled`, `Booked`, `Did Not Attend`) |
| `Status` | TEXT | `P` = Planned, `A` = Actual |
| `Trt_Cat` | TEXT | Treatment category; `NC` = non-consult (exclude, except Dental — see above) |
| `Class` | TEXT | Raw patient class code — resolve through `pt_class_abc` (see `references/pt-class-lookup.md`) |
| `Clinical_Dept` | TEXT | Department name |
| `Sub-Specialty` | TEXT | Sub-specialty (hyphen in name — always double-quote in SQL) |
| `Sub-Specialty_ID` | TEXT | Sub-specialty code — used for Dental exclusion and Psych/Cardiology re-tagging |
| `Trt_OU` | TEXT | Clinic name |
| `Attn_Phy` | TEXT | Attending physician name |
| `Attn_MCR` | TEXT | Attending physician MCR number |
| `Age` | TEXT | Patient age — cast to INT for ranges |
| `Sex` | TEXT | `M` / `F` |
| `Referral_type` | TEXT | How patient was referred |
| `Pri_Diag_Code` | TEXT | ICD-10 diagnosis code |
| `PAT_ENC_CSN_ID` | TEXT | Encounter identifier; see the ontology for candidate joins and completeness cautions. |
| `cnt` | INTEGER | Always 1 |

## Trt_OU relabeling

Production renames/regroups `Trt_OU` before pivoting. Apply these relabels before grouping by `Trt_OU` if replicating `Monthly_Att_TrtOU`:

- `'AH ORTHOPAEDIC CENTRE'` → `'ALEX ORTHOPAEDIC CENTRE'` (SAP legacy name → NGEMR name)
- Psych Medicine SOC sessions → `'Psych Medicine (I-Care)'` — identification logic changes on 1 Aug 2026, see below
- Visits under sub-specialty `LSCHCACA` → `'Cardiology (I-Care)'`

### ⚠️ Psych Medicine identification change — effective 1 Aug 2026

```sql
CASE
  WHEN "Visit_Date" < '2026-08-01'
       AND "Attn_MCR" IN ('L11767F','L17139E','L05460G')
       AND "Sub-Specialty_ID" = 'LSCHRO'
    THEN 'Psych Medicine (I-Care)'
  WHEN "Visit_Date" >= '2026-08-01'
       AND "Sub-Specialty_ID" = 'LSHAPSYM'
    THEN 'Psych Medicine (I-Care)'
  ELSE "Trt_OU"
END AS "Trt_OU"
```

Pre-Aug-2026 rows are identified by the MCR + `LSCHRO` rule and are not retroactively retagged. The date-conditioned CASE above is correct as-is.

## Visit_Type codes

| Code | New/Repeat | Mode |
|------|-----------|------|
| `FV` | First Visit | In-person |
| `RV` | Repeat Visit | In-person |
| `FW` | First Visit | Walk-in |
| `RW` | Repeat Visit | Walk-in |
| `DF` | First Visit | Telehealth |
| `DR` | Repeat Visit | Telehealth |
| `FD` | First Visit | Telehealth (alt) |
| `RD` | Repeat Visit | Telehealth (alt) |

```sql
WHERE "Visit_Type" IN ('FV','FW','DF','FD')   -- new visits only
WHERE "Visit_Type" IN ('RV','RW','DR','RD')   -- repeat visits only
WHERE "Visit_Type" IN ('DF','DR','FD','RD')   -- telehealth only
```

## Patient class

Resolve `Class` through `pt_class_abc` (see `references/pt-class-lookup.md`) to get `Class_abc` and `Class_abc_MOH`, then collapse to `Pat_Class` for outpatient reporting:

```sql
-- Step 1: Class_abc (financial) — see pt-class-lookup.md for full CASE

-- Step 2: Class_abc_MOH (MOH-facing) — see pt-class-lookup.md for full CASE

-- Step 3: collapse to Pat_Class (used in Monthly_Att_TrtOU etc.)
CASE
  WHEN "Class_abc" IN ('A1','B1','Private') THEN 'Private'
  WHEN "Class_abc" IN ('B2','C','Subsidized') THEN 'Subsidized'
  ELSE "Class_abc"
END AS "Pat_Class"
```

## Example: monthly new vs repeat trend

```sql
SELECT
  DATE_TRUNC('month', "Visit_Date") AS month,
  SUM(CASE WHEN "Visit_Type" IN ('FV','FW','DF','FD') THEN 1 ELSE 0 END) AS new_visits,
  SUM(CASE WHEN "Visit_Type" IN ('RV','RW','DR','RD') THEN 1 ELSE 0 END) AS repeat_visits
FROM outpatient
WHERE "prelim_flag" = 'N'
  AND ("Status" != 'P' OR "Status" IS NULL)
  AND "Visit_Type" IN ('FV','RV','FW','RW','DF','DR','FD','RD')
  AND ("Trt_Cat" != 'NC' OR "Sub-Specialty_ID" IN ('LSHAPROS','LSHADEN','LSHAGDEN','LSHAGDGD'))
  AND "Visit_Date" >= '2024-01-01'
GROUP BY 1 ORDER BY 1;
```

## Join to procedure

Use the candidate join in `references/data-ontology.yaml` and validate counts for the requested period.

---
name: ah-analytics-procedure
description: Column reference and SQL guidance for the ah-analytics procedure table (Combined_procedure — surgical and procedural cases). Use when writing SQL against the procedure table, or when the user asks about OT utilisation, surgery volume, day surgery, operating theatre, procedure counts, surgeon workload, anaesthesia, or surgical case mix at Alexandra Hospital. Each row is one procedure; a single case can have multiple rows.
---

# AH Analytics — procedure table (surgical procedures)

**One row per procedure. Primary date: `operation_date` (TIMESTAMP).**
A single case can have multiple rows (multi-procedure). Always clarify whether the user wants procedure count or case count.

## Query baseline

Use the `procedure` filters and canonical date in `references/data-ontology.yaml`.

Use the typed `operation_date` field directly for date filtering.

## Critical: episode vs procedure count, and the two case identifiers

**Clarify which the user wants before writing — they give very different numbers:**

```sql
COUNT(*)                              -- procedures (each row = one procedure)
COUNT(DISTINCT "case_no")             -- candidate case count where case_no is populated
```

**Episode-level counting requires an explicit definition.** The live table has no physical `case_identifier` column. Use `COUNT(*)` for procedures; use a validated, period-specific case expression only after checking identifier completeness.

## Key columns

| Column | Type | Meaning |
|--------|------|---------|
| `Case_No` | TEXT | Episode identifier; see the ontology for candidate joins and completeness cautions. |
| `PAT_ENC_CSN_ID` | TEXT | NGEMR encounter identifier; see the ontology for candidate joins and completeness cautions. |
| `operation_date` | TIMESTAMP | Date of procedure |
| `OT_Begin_Date` | TIMESTAMP | OT session start date |
| `OT_Begin_Time` | TIME | OT session start time |
| `OT_End_Date` | TIMESTAMP | OT session end date |
| `OT_End_Time` | TIME | OT session end time |
| `Adm_Type` | TEXT | Determines OP vs IP segmentation — see below |
| `Treatment_OU` | TEXT | Operating theatre location |
| `Treatment_Rm` | TEXT | Specific room name |
| `OpTable` | TEXT | Raw OT table number/text — see transform below |
| `Surgical_Visit_Type` | TEXT | `Elective Oper`, `Emergency Oper`, etc. |
| `Surgery_Case_Type` | TEXT | `Elective` / `Emergency` |
| `Sub-Specialty` | TEXT | Surgical sub-specialty (always double-quote — hyphen in name) |
| `Sub-Specialty_Final` | TEXT (derived) | Harmonized sub-specialty — use instead of raw `Sub-Specialty` |
| `Clinical_Dept` | TEXT | Department |
| `Surgeon` | TEXT | Primary surgeon name |
| `Surgeon_MCR_No` | TEXT | Primary surgeon MCR |
| `Anaesthetist` | TEXT | Anaesthetist name |
| `ASA_Score` | TEXT | ASA physical status (1–5) |
| `Proc_Code` | TEXT | NGEMR procedure code |
| `Proc_Description` | TEXT | NGEMR procedure description |
| `TOSP_Level_Grouping` | TEXT | Surgical complexity level |
| `DRG_Code` | TEXT | DRG code |
| `Cls` | TEXT | Raw patient class code — resolve through `pt_class_abc` (see `references/pt-class-lookup.md`) |
| `Pat_Class` | TEXT (derived) | `Cls` → `Class_abc` → collapsed to `'Private'`/`'Subsidized'` — see below |
| `Age` | TEXT | Patient age |
| `Proc_Row_Num` | TEXT | Row within a multi-procedure case (1 = primary) |
| `cnt` | INTEGER | Always 1 |

## OpTable transform

Whenever `OpTable` is used as a grouping or display dimension, truncate to the **first character only**, then relabel any value starting with `M` as `'Minor Surgical Procedures'`. Do not use the raw multi-character value directly.

```sql
CASE WHEN LEFT("OpTable", 1) = 'M' THEN 'Minor Surgical Procedures'
     ELSE LEFT("OpTable", 1)
END AS op_table
```

## Patient class

Resolve `Cls` through `pt_class_abc` (see `references/pt-class-lookup.md`) to get `Class_abc`, then collapse to `Pat_Class`:

```sql
CASE
  WHEN "Class_abc" IN ('A1','B1') THEN 'Private'
  WHEN "Class_abc" IN ('B2','C')  THEN 'Subsidized'
  ELSE "Class_abc"    -- 'Private'/'Subsidized' from PTE/SUB-family codes pass through unchanged
END AS "Pat_Class"
```

## Sub-Specialty_Final harmonization

| Raw `Sub-Specialty` | Mapped `Sub-Specialty_Final` |
|---|---|
| `Alex Fast General Surgery`, `Alex Chronic General Surgery` | `Alex General Surgery` |
| `Alex HA General Orthopaedic`, `Alex HA Adult Reconstruction` | `Alex Orthopaedic` |

All other values pass through unchanged.

## Adm_Type segmentation

```sql
-- Day surgery / outpatient procedures
WHERE "Adm_Type" IN ('DS','ES','DO')
-- DS=Day Surgery  ES=Endoscopy Surgery (day)  DO=Day Outpatient endoscopy

-- Inpatient procedures
WHERE "Adm_Type" IN ('DI','SD','EM','EL')

-- Main OT only (excludes endoscopy)
WHERE "Treatment_OU" IN ('ALEX DAY SURGERY OT','ALEX MAIN OPERATING THEATRE')
  AND "Adm_Type" NOT IN ('DO','ES')
```

## Surgery duration (minutes)

```sql
EXTRACT(EPOCH FROM (
  (CAST("OT_End_Date" AS DATE) + "OT_End_Time"::TIME) -
  (CAST("OT_Begin_Date" AS DATE) + "OT_Begin_Time"::TIME)
)) / 60 + 15 AS duration_mins
```

## Primary procedure per case

```sql
WHERE CAST("Proc_Row_Num" AS INT) = 1
```

## Example: monthly day surgery procedures

```sql
SELECT
  DATE_TRUNC('month', "operation_date") AS month,
  "Clinical_Dept", "Sub-Specialty", "Treatment_OU",
  COUNT(*) AS procedures
FROM procedure
WHERE "prelim_flag" = 'N'
  AND "Adm_Type" IN ('DS','ES','DO')
  AND "operation_date" >= DATE '2024-01-01'
GROUP BY 1, 2, 3, 4 ORDER BY 1;
```

## Example: monthly OT episodes vs. procedures by sub-specialty

```sql
SELECT
  DATE_TRUNC('month', "operation_date") AS month,
  "Sub-Specialty",
  COUNT(DISTINCT "case_no") AS cases_with_case_no,
  COUNT(*) AS procedures
FROM procedure
WHERE "prelim_flag" = 'N'
  AND UPPER("Treatment_OU") IN ('ALEX DAY SURGERY OT','ALEX MAIN OPERATING THEATRE')
  AND "Adm_Type" NOT IN ('DO','ES')
  AND "operation_date" >= DATE '2024-01-01'
GROUP BY 1, 2 ORDER BY 1, cases_with_case_no DESC;
```

## Joins

Use the candidate joins in `references/data-ontology.yaml` and validate counts for the requested period.

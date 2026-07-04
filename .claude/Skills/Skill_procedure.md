---
name: ah-analytics-procedure
description: Column reference and SQL guidance for the ah-analytics procedure table (Combined_procedure — surgical and procedural cases). Use when writing SQL against the procedure table, or when the user asks about OT utilisation, surgery volume, day surgery, operating theatre, procedure counts, surgeon workload, anaesthesia, or surgical case mix at Alexandra Hospital. Each row is one procedure; a single case can have multiple rows.
---

# AH Analytics — procedure table (surgical procedures)

**One row per procedure. Primary date: `Operation_Date` (TEXT — must cast).**
A single case can have multiple rows (multi-procedure). Always clarify whether the user wants procedure count or case count.

## Mandatory WHERE filters

```sql
WHERE "prelim_flag" = 'N'
  AND CAST("Operation_Date" AS DATE) >= '2024-01-01'
```

## Critical: episode vs procedure count

```sql
COUNT(*)                      -- procedures (each row = one procedure)
COUNT(DISTINCT "Case_No")     -- cases/episodes (deduplicate multi-procedure cases)
```

## Key columns

| Column | Type | Meaning |
|--------|------|---------|
| `Case_No` | TEXT | Episode identifier — join to `admission` / `discharge` |
| `PAT_ENC_CSN_ID` | TEXT | NGEMR encounter — join to `outpatient` |
| `Operation_Date` | TEXT | Date of procedure — **must cast**: `CAST("Operation_Date" AS DATE)` |
| `OT_Begin_Date` | TIMESTAMP | OT session start date |
| `OT_Begin_Time` | TIME | OT session start time |
| `OT_End_Date` | TIMESTAMP | OT session end date |
| `OT_End_Time` | TIME | OT session end time |
| `Adm_Type` | TEXT | Determines OP vs IP segmentation — see below |
| `Treatment_OU` | TEXT | Operating theatre location |
| `Treatment_Rm` | TEXT | Specific room name |
| `OpTable` | TEXT | OT table number; `M` = minor surgical procedure |
| `Surgical_Visit_Type` | TEXT | `Elective Oper`, `Emergency Oper`, etc. |
| `Surgery_Case_Type` | TEXT | `Elective` / `Emergency` |
| `Sub-Specialty` | TEXT | Surgical sub-specialty (always double-quote — hyphen in name) |
| `Clinical_Dept` | TEXT | Department |
| `Surgeon` | TEXT | Primary surgeon name |
| `Surgeon_MCR_No` | TEXT | Primary surgeon MCR |
| `Anaesthetist` | TEXT | Anaesthetist name |
| `ASA_Score` | TEXT | ASA physical status (anaesthetic risk: ASA 1–5) |
| `Proc_Code` | TEXT | NGEMR procedure code |
| `Proc_Description` | TEXT | NGEMR procedure description |
| `TOSP_Level_Grouping` | TEXT | Surgical complexity level |
| `DRG_Code` | TEXT | DRG code |
| `Cls` | TEXT | Patient class |
| `Age` | TEXT | Patient age |
| `Proc_Row_Num` | TEXT | Row within a multi-procedure case (1 = primary) |
| `cnt` | INTEGER | Always 1 |

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

## Example: monthly OT cases by sub-specialty

```sql
SELECT
  DATE_TRUNC('month', CAST("Operation_Date" AS DATE)) AS month,
  "Sub-Specialty",
  COUNT(DISTINCT "Case_No") AS cases,
  COUNT(*) AS procedures
FROM procedure
WHERE "prelim_flag" = 'N'
  AND "Treatment_OU" IN ('ALEX DAY SURGERY OT','ALEX MAIN OPERATING THEATRE')
  AND "Adm_Type" NOT IN ('DO','ES')
  AND CAST("Operation_Date" AS DATE) >= '2024-01-01'
GROUP BY 1, 2 ORDER BY 1, cases DESC;
```

## Joins

```sql
-- To admission (demographics, LOS for surgical inpatients)
FROM procedure p JOIN admission a ON p."Case_No" = a."Case_No"

-- To outpatient (SOC visit that led to day surgery)
FROM procedure p JOIN outpatient o ON p."PAT_ENC_CSN_ID" = o."PAT_ENC_CSN_ID"
```

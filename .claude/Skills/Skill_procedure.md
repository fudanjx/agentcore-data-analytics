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
| `Case_No` | TEXT | Episode identifier — join to `admission` / `discharge`. **Blank for new encounters created from Feb 2026 (NBS go-live) onward** — use `PAT_ENC_CSN_ID`. Also see the `Case_Identifier`/`Admsn CSN` caveat above — `Case_No` is not always what production dedupes episodes on. |
| `PAT_ENC_CSN_ID` | TEXT | NGEMR encounter — join to `outpatient`. **Null before 2023-01-01** (SAP era). |
| `Operation_Date` | TEXT | Date of procedure — **must cast**: `CAST("Operation_Date" AS DATE)` |
| `OT_Begin_Date` | TIMESTAMP | OT session start date |
| `OT_Begin_Time` | TIME | OT session start time |
| `OT_End_Date` | TIMESTAMP | OT session end date |
| `OT_End_Time` | TIME | OT session end time |
| `Adm_Type` | TEXT | Determines OP vs IP segmentation — see below |
| `Treatment_OU` | TEXT | Operating theatre location |
| `Treatment_Rm` | TEXT | Specific room name |
| `OpTable` | TEXT | Raw OT table number/text. **Default whenever a user asks for procedure counts by Op Table**: truncate to the **first character only**, then relabel any value starting `M` as `'Minor Surgical Procedures'`. Apply this transform every time `OpTable` is used as a grouping/display dimension — don't use the raw multi-character value directly. |
| `Surgical_Visit_Type` | TEXT | `Elective Oper`, `Emergency Oper`, etc. |
| `Surgery_Case_Type` | TEXT | `Elective` / `Emergency` |
| `Sub-Specialty` | TEXT | Surgical sub-specialty (always double-quote — hyphen in name) |
| `Sub-Specialty_Final` | TEXT (derived) | Harmonized sub-specialty (see mapping below) — used instead of raw `Sub-Specialty` |
| `Clinical_Dept` | TEXT | Department |
| `Surgeon` | TEXT | Primary surgeon name |
| `Surgeon_MCR_No` | TEXT | Primary surgeon MCR |
| `Anaesthetist` | TEXT | Anaesthetist name |
| `ASA_Score` | TEXT | ASA physical status (anaesthetic risk: ASA 1–5) |
| `Proc_Code` | TEXT | NGEMR procedure code |
| `Proc_Description` | TEXT | NGEMR procedure description |
| `TOSP_Level_Grouping` | TEXT | Surgical complexity level |
| `DRG_Code` | TEXT | DRG code |
| `Cls` | TEXT | Raw patient class code (`A`, `AP`, `ARF`, `B1`, `B1P`, `B1RF`, `B2`, `B2P`, `B2RF`, `C`, `CP`, `CRF`, `NR`, `PTE`, `PTEP`, `PTRF`, `SUB`, `SUBP`, etc.) — see `pt_class_abc` lookup and `Pat_Class` derivation below |
| `Pat_Class` | TEXT (derived) | `Cls` joined against the `pt_class_abc` lookup (below) to get `Class_abc`, then collapsed: `A1`/`B1` → `'Private'`, `B2`/`C` → `'Subsidized'` (`Private`/`Subsidized` pass through unchanged since the lookup already resolves `PTE`/`SUB`-family codes to those). **This, not raw `Cls`, is what every procedure pivot table groups patient class by.** |
| `Age` | TEXT | Patient age |
| `Proc_Row_Num` | TEXT | Row within a multi-procedure case (1 = primary) |
| `cnt` | INTEGER | Always 1 |

## Patient class & residency lookup (`pt_class_abc` — shared across all tables)

`Cls` resolves through the same `pt_class_abc` lookup table used by `admission` (`Adm_Cls`), `discharge` (`Disch_Class`), `inflight` (`Class`), and `outpatient` (`Class`) — see `Skill_outpatient.md` for the full table and rationale. Reproduced here for `Cls`:

| Raw `Cls` | `Class_abc` | `Class_abc_MOH` | `Resident_Type` | `Resident_MOH` |
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

`Pat_Class` (used in every procedure pivot) is built from `Class_abc` only:

```sql
CASE
  WHEN "Class_abc" IN ('A1','B1') THEN 'Private'
  WHEN "Class_abc" IN ('B2','C') THEN 'Subsidized'
  ELSE "Class_abc"      -- 'Private'/'Subsidized' from PTE/SUB-family codes pass through unchanged
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

## Example: Monthly_IPProcedures (inpatient procedures by sub-specialty and OpTable)

```sql
SELECT
  DATE_TRUNC('month', CAST("Operation_Date" AS DATE)) AS month,
  "Sub-Specialty_Final",
  CASE WHEN LEFT("OpTable", 1) = 'M' THEN 'Minor Surgical Procedures' ELSE LEFT("OpTable", 1) END AS op_table,
  COUNT(*) AS procedures
FROM procedure
WHERE "Adm_Type" IN ('DI','SD','EM','EL')
  AND CAST("Operation_Date" AS DATE) >= '2024-01-01'
GROUP BY 1, 2, 3 ORDER BY 1;
```

## Example: Monthly_DSSurgicalProcedure (day surgery procedures)

```sql
SELECT
  DATE_TRUNC('month', CAST("Operation_Date" AS DATE)) AS month,
  "Clinical_Dept", "Sub-Specialty", "Treatment_OU", "Resident_Type", "Pat_Class",
  COUNT(*) AS procedures
FROM procedure
WHERE "Adm_Type" IN ('DS','ES','DO')
  AND CAST("Operation_Date" AS DATE) >= '2024-01-01'
GROUP BY 1, 2, 3, 4, 5, 6 ORDER BY 1;
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

⚠️ For dates before 2023-01-01, `PAT_ENC_CSN_ID` will be null — fall back to `Case_No`. For encounters created from Feb 2026 (NBS go-live) onward, `Case_No` may be blank — fall back to `PAT_ENC_CSN_ID`. See `SKILL.md` for the full explanation.

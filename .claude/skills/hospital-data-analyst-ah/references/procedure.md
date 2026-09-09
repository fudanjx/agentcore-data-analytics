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

## Critical: episode vs procedure count, and the identifiers

**Clarify which the user wants before writing — they give very different numbers:**

```sql
COUNT(*)                              -- procedures (each row = one procedure)
COUNT(DISTINCT "case_no")             -- candidate case count where case_no is populated
```

**Episode-level counting requires an explicit definition.** The live table has no physical `case_identifier` column. Use `COUNT(*)` for procedures; use a validated, period-specific case expression only after checking identifier completeness.

**`Case_No` is populated broadly across both eras here** (~92% of rows, including most NGEMR-era rows) — unlike admission/discharge, where `Case_No` is SAP-only. Don't use `Case_No`'s presence/absence to detect era for this table.

**`Adm_CSN` / `Surgery_CSN` are NGEMR-only and only ~31% populated overall** — they exist only when the procedure has a linked NGEMR encounter (day-surgery/walk-in procedures with no inpatient admission commonly have neither). When both are populated they agree only ~57% of the time: `Adm_CSN` is the admission encounter the procedure is tied to, `Surgery_CSN` is the specific procedure/OT encounter itself (a patient's admission can cover several procedure encounters, or a procedure encounter can stand alone with no admission link). Use `Surgery_CSN` to identify the procedure encounter itself; use `Adm_CSN` to join back to `admission`/`inflight`.

## Key columns

| Column | Type | Meaning |
|--------|------|---------|
| `Case_No` | TEXT | Broadly-populated case identifier (~92% of rows, both eras) — see caution above. |
| `Adm_CSN` | TEXT | NGEMR admission-encounter identifier — join key back to `admission`. Populated ~31% of rows. |
| `Surgery_CSN` | TEXT | NGEMR procedure/OT-encounter identifier — identifies this specific procedure encounter. Populated ~31% of rows; differs from `Adm_CSN` ~43% of the time when both present. |
| `C` (→ `record_type` in the DB) | TEXT | Same ETL mismatch as admission/inflight: really the SAP case-number suffix letter, not a category — concatenate with `Case_No` for the full SAP case number. |
| `operation_date` | TIMESTAMP | Date of procedure |
| `OT_Begin_Date` | TIMESTAMP | OT session start date |
| `OT_Begin_Time` | TIME | OT session start time |
| `OT_End_Date` | TIMESTAMP | OT session end date |
| `OT_End_Time` | TIME | OT session end time |
| `Adm_Type` | TEXT | Determines OP vs IP segmentation — see below. ~1% of rows have no value and fall in neither bucket. |
| `Treatment_OU` | TEXT | Operating theatre location |
| `Treatment_Rm` | TEXT | Specific room name |
| `OpTable` | TEXT | Raw OT table code, e.g. `1B`, `2C`, `M1`, `MSP` — see transform below |
| `Op_Code` | TEXT | Raw procedure/operation code |
| `Surg_Cd_Description` | TEXT | Description tied to `Op_Code` |
| `Surgical_Visit_Type` | TEXT | `Elective Oper`, `Emergency Oper`, etc. |
| `Sub-Specialty` | TEXT | Surgical sub-specialty (always double-quote — hyphen in name) |
| `Sub-Specialty_Final` | TEXT (derived) | Harmonized sub-specialty — use instead of raw `Sub-Specialty` |
| `Clinical_Dept` | TEXT | Department |
| `Surgeon` | TEXT | Primary surgeon name |
| `Surgeon_MCR_No` | TEXT | Primary surgeon MCR |
| `Anaesthetist` | TEXT | Anaesthetist name |
| `Anaesthetist_MCR_No` | TEXT | Anaesthetist MCR |
| `ASA_Score` | TEXT | ASA physical status, raw format `'ASA 1'`–`'ASA 3'` (space-separated, not bare digits). Mostly null (~78% of rows). |
| `DRG_Code` | TEXT | DRG code |
| `Cls` | TEXT | Raw patient class code — resolve through `pt_class_abc` (see `references/pt-class-lookup.md`) |
| `Pat_Class` | TEXT (derived) | `Cls` → `Class_abc` → collapsed to `'Private'`/`'Subsidized'` — see below |
| `Surgery_Patient_Class` | TEXT | Separate raw NGEMR-only field (`DS`, `Inpatient`, `SDA`, `Outpatient`, `DS 23/AS 23`) — not used by production reporting (which uses `Adm_Type` instead); null whenever `Adm_CSN`/`Surgery_CSN` are null. |
| `Age` | TEXT | Patient age |
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

Use the candidate joins in `references/data-ontology.yaml` and validate counts for the requested period. Prefer `Adm_CSN` over `Case_No` when joining to `admission` for NGEMR-era procedures.

## Open items

See `references/procedure-open-questions.md`.

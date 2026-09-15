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
COUNT(DISTINCT COALESCE(adm_csn,case_no))             -- episodes populated
```

**`case_no` is populated across both eras here** — unlike admission/discharge, where `case_no` is SAP-only. Don't use `case_no`'s presence/absence to detect era for this table.

**`adm_csn` / `surgery_csn` are NGEMR-only** — they exist only when the procedure has a linked NGEMR encounter (day-surgery/walk-in procedures with no inpatient admission commonly have neither). When both are populated they don't always agree: `adm_csn` is the admission encounter the procedure is tied to, `surgery_csn` is the specific procedure/OT encounter itself (a patient's admission can cover several procedure encounters, or a procedure encounter can stand alone with no admission link). Use `surgery_csn` to identify the procedure encounter itself; use `adm_csn` to join back to `admission`/`inflight`.

## Key columns

| Column | Type | Meaning |
|--------|------|---------|
| `case_no` | TEXT | Case identifier, populated across both eras — see caution above. |
| `adm_csn` | TEXT | NGEMR admission-encounter identifier — join key back to `admission`. |
| `surgery_csn` | TEXT | NGEMR procedure/OT-encounter identifier — identifies this specific procedure encounter. Can differ from `adm_csn` when both are present. |
| `c` (→ `record_type` in the DB) | TEXT | Same ETL mismatch as admission/inflight: really the SAP case-number suffix letter, not a category — concatenate with `case_no` for the full SAP case number. |
| `operation_date` | TIMESTAMP | Date of procedure |
| `ot_begin_date` | TIMESTAMP | OT session start date |
| `ot_begin_time` | TIME | OT session start time |
| `ot_end_date` | TIMESTAMP | OT session end date |
| `ot_end_time` | TIME | OT session end time |
| `adm_type` | TEXT | Determines OP vs IP segmentation — see below. Rows with no value, or a code outside both buckets, are "Unclassified." |
| `treatment_ou` | TEXT | Operating theatre location |
| `room` | TEXT | Specific room name |
| `optable` | TEXT | Raw OT table code, e.g. `1B`, `2C`, `M1`, `MSP` — see transform below |
| `op_code` | TEXT | Raw procedure/operation code |
| `surg_cd_description` | TEXT | Description tied to `op_code` |
| `surgical_visit_type` | TEXT | `Elective Oper`, `Emergency Oper`, etc. |
| `sub_specialty` | TEXT | Surgical sub-specialty. |
| `sub_specialty_final` | TEXT (derived) | Harmonized sub-specialty — use instead of raw `sub_specialty` |
| `clinical_dept` | TEXT | Department |
| `surgeon` | TEXT | Primary surgeon name |
| `surgeon_mcr_no` | TEXT | Primary surgeon MCR |
| `anaesthetist` | TEXT | Anaesthetist name |
| `anaesthetist_mcr_no` | TEXT | Anaesthetist MCR |
| `asa_score` | TEXT | ASA physical status, raw format `'ASA 1'`–`'ASA 3'` (space-separated, not bare digits). Frequently null. |
| `drg_code` | TEXT | DRG code |
| `cls` | TEXT | Raw patient class code — resolve through `pt_class_abc` (see `references/pt-class-lookup.md`) |
| `pat_class` | TEXT (derived) | `cls` → `class_abc` → collapsed to `'Private'`/`'Subsidized'` — see below |
| `surgery_patient_class` | TEXT | Separate raw NGEMR-only field (`DS`, `Inpatient`, `SDA`, `Outpatient`, `DS 23/AS 23`) — not used by production reporting (which uses `adm_type` instead); null whenever `adm_csn`/`surgery_csn` are null. |
| `age` | TEXT | Patient age |
| `cnt` | INTEGER | Always 1 |

## optable transform

Whenever `optable` is used as a grouping or display dimension, truncate to the **first character only**, then relabel any value starting with `M` as `'Minor Surgical Procedures'`. Do not use the raw multi-character value directly.

```sql
CASE WHEN LEFT(optable, 1) = 'M' THEN 'Minor Surgical Procedures'
     ELSE LEFT(optable, 1)
END AS op_table
```

## Patient class

Resolve `cls` through `pt_class_abc` (see `references/pt-class-lookup.md`) to get `class_abc`, then collapse to `pat_class`:

```sql
CASE
  WHEN class_abc IN ('A1','B1') THEN 'Private'
  WHEN class_abc IN ('B2','C')  THEN 'Subsidized'
  ELSE class_abc    -- 'Private'/'Subsidized' from PTE/SUB-family codes pass through unchanged
END AS pat_class
```

## sub_specialty_final harmonization

**Three stages, straight from the production code:**

| Raw `sub_specialty` (as stored) | Standardized (Alex-prefix normalized) | `sub_specialty_final` |
|---|---|---|
| `Chronic General Surgery` | `Alex Chronic General Surgery` | `Alex General Surgery` |
| `ALEX Chronic General Surgery` | `Alex Chronic General Surgery` | `Alex General Surgery` |
| `alex chronic general surgery` | `Alex Chronic General Surgery` | `Alex General Surgery` |
| `Fast General Surgery` | `Alex Fast General Surgery` | `Alex General Surgery` |
| `HA General Orthopaedic` | `Alex HA General Orthopaedic` | `Alex Orthopaedic` |
| `HA Adult Reconstruction` | `Alex HA Adult Reconstruction` | `Alex Orthopaedic` |
| `HA Urology` (any other value) | `Alex HA Urology` | `Alex HA Urology` (passes through unchanged) |

```sql
-- Stage 1: standardize casing/prefix
CASE WHEN sub_specialty ~* '^alex\s*'
       THEN regexp_replace(sub_specialty, '^alex\s*', 'Alex ', 'i')
     ELSE 'Alex ' || sub_specialty
END AS sub_specialty_standardized

-- Stage 2: collapse to sub_specialty_final
CASE
  WHEN sub_specialty_standardized IN ('Alex Fast General Surgery', 'Alex Chronic General Surgery')
    THEN 'Alex General Surgery'
  WHEN sub_specialty_standardized IN ('Alex HA General Orthopaedic', 'Alex HA Adult Reconstruction')
    THEN 'Alex Orthopaedic'
  ELSE sub_specialty_standardized
END AS sub_specialty_final
```

Note: the source also has a third collapse rule (`Alex Fast Medicine`/`Alex Chronic` -> `Alex Fast Med/Chronic`) commented out -- not currently active in production. Don't implement it unless it gets turned on.

## adm_type segmentation

```sql
-- Day surgery / outpatient procedures
WHERE adm_type IN ('DS','ES','DO')
-- DS=Day Surgery  ES=Endoscopy Surgery (day)  DO=Day Outpatient endoscopy

-- Inpatient procedures
WHERE adm_type IN ('DI','SD','EM','EL')

-- Main OT only (excludes endoscopy)
WHERE treatment_ou IN ('ALEX DAY SURGERY OT','ALEX MAIN OPERATING THEATRE')
  AND adm_type NOT IN ('DO','ES')
```

## Surgery duration (minutes)

```sql
EXTRACT(EPOCH FROM (
  (CAST(ot_end_date AS DATE) + ot_end_time::TIME) -
  (CAST(ot_begin_date AS DATE) + ot_begin_time::TIME)
)) / 60 + 15 AS duration_mins
```

## Example: monthly day surgery procedures

```sql
SELECT
  DATE_TRUNC('month', operation_date) AS month,
  clinical_dept, sub_specialty, treatment_ou,
  COUNT(*) AS procedures
FROM procedure
WHERE adm_type IN ('DS','ES','DO')
  AND operation_date >= DATE '2024-01-01'
GROUP BY 1, 2, 3, 4 ORDER BY 1;
```

## Example: monthly OT episodes vs. procedures by sub-specialty

```sql
SELECT
  DATE_TRUNC('month', operation_date) AS month,
  sub_specialty,
  COUNT(DISTINCT case_no) AS cases_with_case_no,
  COUNT(*) AS procedures
FROM procedure
WHERE UPPER(treatment_ou) IN ('ALEX DAY SURGERY OT','ALEX MAIN OPERATING THEATRE')
  AND adm_type NOT IN ('DO','ES')
  AND operation_date >= DATE '2024-01-01'
GROUP BY 1, 2 ORDER BY 1, cases_with_case_no DESC;
```

Add `AND prelim_flag = 'N'` only if the user explicitly asks to exclude provisional/preliminary records — don't filter on it by default.

## Joins

Use the candidate joins in `references/data-ontology.yaml` and validate counts for the requested period. Prefer `adm_csn` over `case_no` when joining to `admission` for NGEMR-era procedures.


## Additional columns (documented in the live schema, not previously listed here)

The live schema (83 columns total) includes several columns not covered above. Population % is from `Combined_proc_1000.csv` (1000-row sample).

### Sub-specialty variants — use `sub_specialty`

The raw file has **three** sub-specialty-shaped columns. **Use `sub_specialty` — confirmed by Tammy.** Documented here so the other two aren't mistaken for it later:

| Column | Population | Example | Note |
|---|---|---|---|
| `sub_specialty` | 100% | `Alex HA Opthalmology` | **Use this one.** Full name — what `sub_specialty_final` harmonization (above) is built on. |
| `sub_spec` | 100% | `LSHAOPT` | Not used. Looks like the department-code counterpart to `sub_specialty` (same code space as `admission.adm_dept_ou`). |
| `subspecialty` | 31.2% | `Chronic` | Not used. Populated only for the ~31% of rows that have `adm_csn`/`surgery_csn` (NGEMR admission-linked procedures) — an admission-program-level field, not a procedure-level one. Don't confuse with `sub_specialty` above. |

### Patient / demographic

| Column | Population | Example |
|---|---|---|
| `ext_pat_id` | 100% | `S0386561D` (PII — NRIC/FIN format) |
| `race` | 31.2% | `Chinese` — only populated for NGEMR admission-linked rows |
| `postal` | 31.2% | `670602` (PII) — only populated for NGEMR admission-linked rows |
| `resident` | 99.7% | `Resident` |

### Admission/discharge context captured on the procedure record

| Column | Population | Example |
|---|---|---|
| `admission_ward` | 5.3% | `ALEX UROLOGY CLINIC` |
| `treatment_ward` | 29.9% | `ALEX ENDOSCOPY CENTRE` |
| `adm_specialty` | 31.2% | `Chronic Program` |
| `disch_specialty` | 30.7% | `Chronic Program` |
| `level_of_care` | 30.9% | `Subsidised` |
| `admission_type` | 30.4% | `Endoscopy` — separate raw field from `adm_type`; not used by production reporting per the existing `surgery_patient_class` note above, same caveat likely applies |
| `admission_source` | 30.4% | `SOC` |
| `admit_date_time` | 30.7% | `2025-03-20 08:28:00` |
| `disch_date_time` | 30.7% | `2025-03-20 10:46:00` |
| `los` | 31.2% | `1` |
| `disch_disposition` | 30.4% | `Discharge to Home (with TCU)` |
| `discharged_to` | 0.4% | `Jurong Community Hospital {COMM ENT JCH} (ZZZ4708)` |
| `treatment_location` | 31.2% | `LCENDO` |
| `clinic` | 100% | `LDHEAG` |

### Clinicians

| Column | Population | Example |
|---|---|---|
| `first_performing_surgeon` | 9.1% | `TAN, WOON TECK CLEMENT` |
| `admitting_clinician` | 30.4% | `TANG, SI YING` |
| `attending_clinician` | 30.4% | `TANG, SI YING` |
| `disch_clinician` | 30.4% | `TANG, SI YING` |

### Procedure/case classification

| Column | Population | Example |
|---|---|---|
| `proc_description` | 31.2% | `HC INTESTINE/STOMACH, UPPER GI ENDOSCOPY WITH / WITHOUT BIOPSY` |
| `surgery_priority` | 0% in this sample | — |
| `case_classification` | 22.0% | `Elective` |
| `is_cancerous` | 9.2% | `Not Answered` |
| `unscheduled_return_to_ot` | 31.2% | `N` |
| `trtment` | 100% | `LODSOT` |
| `referral_type` | 68.8% | `Natl Uni Health` |
| `referral_hospital` | 99.2% | `National University Hospital` |
| `subvention_doc` | 30.9% | `SG Pink IC/BC` |
| `post_op_diagnosis` / `post_op_diagnosis_code` | 14.7% | `Hemorrhoids \| Intestinal metaplasia of stomach` / `I84.9 \| K31.88` — pipe-delimited when multiple |
| `pre_op_diagnosis` / `pre_op_diagnosis_code` | 20.5% | `Intestinal metaplasia of stomach` / `K31.88` |
| `period` | 31.2% | `Mar 2025` — pre-formatted month label, NGEMR-linked rows only |

### OT/PACU timing sequence (all sparsely populated — used for OT-flow duration analysis, not general reporting)

`in_ot_reception`, `in_procedure_room`, `surgical_prep_start`, `anaesthesia_start`, `anaesthesia_ready`, `anaesthesia_finish`, `out_procedure_room`, `clean_up_complete`, `in_pacu`, `pacu_care_complete`, `out_pacu`, `amb_unit_recovery_start`, `amb_unit_recovery_complete`, `out_of_amb_unit_recovery`, `procedural_care_complete` — population ranges 3%–22% in the sample; timestamps, only meaningful for cases where that OT/PACU stage was actually recorded.

---
name: ah-analytics-outpatient
description: Column reference and SQL guidance for the ah-analytics outpatient table (Combined_SOC — Specialist Outpatient Clinic visits). Use when writing SQL against the outpatient table, or when the user asks about SOC visits, clinic appointments, new vs repeat patients, first visit rates, or telehealth attendance at Alexandra Hospital.
---

# AH Analytics — outpatient table (SOC visits)

**One row per appointment. Primary date: `visit_date`.**

**Column names are lowercase and unquoted in this table** — unlike admission/discharge/inflight/procedure/urgentcarecenter, whose columns are mixed-case and must be double-quoted in SQL, `outpatient`'s live columns in `ah-analytics` are plain lowercase snake_case. Don't quote them, and don't assume the casing from the raw sample CSV (`Combined_SOC_1000.csv`) — that file is the pre-load export and uses different (PascalCase) header names than the actual queryable table.

## Query baseline

Use the `outpatient` filters and canonical date in `references/data-ontology.yaml`.

## Identifiers

`case_no` is populated broadly across both eras (~91% of rows, including most NGEMR-era rows) — unlike admission/discharge, don't use its presence/absence to detect era here. `pat_enc_csn_id` is the clean era switch: populated only for NGEMR-era rows (from 1 Jan 2023), always null for SAP-era rows — use it as the primary identifier/join key for NGEMR-era visits. `status` and `appt_status` are also NGEMR-only fields (both null for every SAP-era row) — a null `status` is expected for SAP-era data, not a data-quality gap, and is why the standard filter below keeps null alongside `'A'`.

## Key columns

| Column | Type | Meaning |
|--------|------|---------|
| `case_no` | TEXT | Broadly-populated case identifier (~91% of rows, both eras) — see identifiers note above. |
| `pat_enc_csn_id` | TEXT | NGEMR encounter identifier — clean era switch, null for all SAP-era rows. |
| `visit_date` | TIMESTAMP | Date visit occurred — primary date filter |
| `visit_time` | TIME | Actual visit time |
| `appt_time` | TIME | Scheduled appointment time |
| `visit_type` | TEXT | Visit classification — see mapping below |
| `appt_status` | TEXT | Lifecycle status (`Completed`, `Arrived`, `Cancelled`, `Booked`, `Did Not Attend`). NGEMR-only; not filtered by production reporting — `Did Not Attend` rows are included in workload counts as-is. |
| `status` | TEXT | `P` = Planned, `A` = Actual. NGEMR-only — see identifiers note above. |
| `trt_cat` | TEXT | Treatment category; `NC` = non-consult (exclude, except Dental — see above). `NC` is the single largest value in the raw data. |
| `class` | TEXT | Raw patient class code — resolve through `pt_class_abc` (see `references/pt-class-lookup.md`) |
| `clinical_dept` | TEXT | Department name |
| `sub_specialty` | TEXT | Sub-specialty name |
| `sub_specialty_id` | TEXT | Sub-specialty code — used for Dental exclusion and Psych/Cardiology re-tagging |
| `trt_ou` | TEXT | Clinic name |
| `trt_ou_id` | TEXT | Clinic code — used for MOH treatment-unit mapping |
| `attn_phy` | TEXT | Attending physician name |
| `attn_mcr` | TEXT | Attending physician MCR number |
| `age` | TEXT | Patient age — cast to INT for ranges |
| `sex` | TEXT | `M` / `F` |
| `referral_type` | TEXT | How patient was referred |
| `pri_diag_code` | TEXT | ICD-10 diagnosis code |
| `cnt` | INTEGER | Always 1 |

## Additional columns

Documented for completeness — not needed for the standard SOC workload queries above, but real and available if a question calls for them. Population rates are from the pre-load sample (`Combined_SOC_1000.csv`, 1,000 rows); they should carry over to the live table since the ETL only renames columns, not values, but treat them as approximate.

**Visit/appointment metadata**

| Column | Type | Meaning |
|---|---|---|
| `visit_no` | TEXT | Visit sequence number for the patient at this clinic (91% populated) |
| `visit_type_desc` | TEXT | Human-readable label for `visit_type` (62% populated) |
| `movement_creation_date` | DATE | Record creation date (99% populated) |
| `appt_creation_date` | DATE | Appointment creation date (62% populated) |
| `appt_creation_rationale` | TEXT | Why the appointment was created, e.g. `'Doctor Requested Appointment'` (30% populated) |
| `appt_request_dttm` | TIMESTAMP | Appointment request date/time (28% populated) |
| `appt_wt` | TEXT | Waiting time in days, stored as a string (`'0'`, `'14'`) — cast to INT for arithmetic (62% populated) |

**Clinic/department/specialty**

| Column | Type | Meaning |
|---|---|---|
| `trt_room` | TEXT | Treatment room code (20% populated) |
| `trt_room_name` | TEXT | Treatment room name (48% populated) |
| `clinical_dept_id` | TEXT | Department code, pairs with `clinical_dept` (99% populated) |
| `prc_desc` | TEXT | Procedure/service description, e.g. `'NUHS TECHNICAL VISIT'` (62% populated) |
| `prc_sub_specialty` | TEXT | Sub-specialty tied to the procedure/service rather than the visit itself (33% populated) |
| `adt_pat_class` | TEXT | ADT-side patient class label (`Outpatient`/`Inpatient`/`SDA`/`DS`) — distinct from `class`/`pt_class_abc` (62% populated) |

**Demographics**

| Column | Type | Meaning |
|---|---|---|
| `name` | TEXT | Patient name (100% populated) |
| `nationality` | TEXT | e.g. `'SINGAPORE'`, `'Singaporean'` — inconsistent casing/format in raw data (100% populated) |
| `ext_pat_id` | TEXT | External patient identifier (100% populated) |
| `race` | TEXT | Chinese/Malay/Indian/Others (100% populated) |
| `postal_code` | TEXT | Some pre-load values carry a trailing `.0` from a numeric source system — strip before use (99.8% populated) |
| `pat_pref_institution` | TEXT | Free-text scheduling note, not a clean categorical field (2.9% populated) |

**Referral** (three related but distinct fields — don't conflate)

| Column | Type | Meaning |
|---|---|---|
| `ref_mcr` | TEXT | Referring physician MCR number (21% populated) |
| `ref_phy` | TEXT | Referring physician name, or `'PROVIDER NOT IN SYSTEM'` (24% populated) |
| `referral_hospital` | TEXT | Referral source category/name, e.g. `'Intra-Dept referral SOC (Sub)'`, `'Self-referral/walk-in'`, or an external institution — always populated, distinct from `referral_type` (100% populated) |
| `ref_hosp_address` | TEXT | Free-text address, populated only for external referrals (37% populated) |

**Diagnosis**

| Column | Type | Meaning |
|---|---|---|
| `pri_diag_desc` | TEXT | Description pairing with `pri_diag_code` (33% populated) |
| `other_diag_code` | TEXT | Secondary diagnosis code(s) (5.7% populated) |
| `other_diag_desc` | TEXT | Description(s) pairing with `other_diag_code` (5.7% populated) |
| `diag_code_type_all` | TEXT | Coding system used, e.g. `'CURRENT_ICD10_LIST'` (23% populated) |
| `comments` | TEXT | Free-text clinical note (62% populated) |

**Data management**

| Column | Type | Meaning |
|---|---|---|
| `prelim_flag` | TEXT | `'Y'`/`'N'` — see `global_rules.preliminary_records`/`preliminary_disclosure` in `data-ontology.yaml` (98% `N` / 2% `Y` in the sample) |

## trt_ou relabeling

Production renames/regroups `trt_ou` before pivoting. Apply these relabels before grouping by `trt_ou` if replicating `Monthly_Att_TrtOU`:

- `'AH ORTHOPAEDIC CENTRE'` → `'ALEX ORTHOPAEDIC CENTRE'` (SAP legacy name → NGEMR name)
- Psych Medicine SOC sessions → `'Psych Medicine (I-Care)'` — identification logic changes on 1 Aug 2026, see below
- Visits under sub-specialty `LSCHCACA` → `'Cardiology (I-Care)'`

### ⚠️ Psych Medicine identification change — effective 1 Aug 2026

```sql
CASE
  WHEN visit_date < '2026-08-01'
       AND attn_mcr IN ('L11767F','L17139E','L05460G')
       AND sub_specialty_id = 'LSCHRO'
    THEN 'Psych Medicine (I-Care)'
  WHEN visit_date >= '2026-08-01'
       AND sub_specialty_id = 'LSHAPSYM'
    THEN 'Psych Medicine (I-Care)'
  ELSE trt_ou
END AS trt_ou
```

Pre-Aug-2026 rows are identified by the MCR + `LSCHRO` rule and are not retroactively retagged. The date-conditioned CASE above is correct as-is.

## visit_type codes

The SOC doctor-consult workload (new vs. repeat, in-person vs. telehealth) uses exactly these 8 codes:

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
WHERE visit_type IN ('FV','FW','DF','FD')   -- new visits only
WHERE visit_type IN ('RV','RW','DR','RD')   -- repeat visits only
WHERE visit_type IN ('DF','DR','FD','RD')   -- telehealth only
```

**Other `visit_type` values exist in the raw data and are real — don't treat them as noise.** They belong to separate report sections, not the SOC doctor-consult workload above:

| Code(s) | Meaning | Used for |
|---|---|---|
| `AF`, `AR` | Allied Health (first/repeat) | `OP_Attendance_Type = 'Allied Health'` in the Outpatient Attendance (MACG) report |
| `FS`, `RS` | Staff clinic (first/repeat) | Included in doctor-workload counts as a placeholder treatment category, not in SOC workload |
| `PA` | Anaesthesia pre-assessment | Included in doctor-workload counts, not in SOC workload |
| `TT`, `EN`, `RT`, `PR`, `TS`, `XP`, `FT`, `TR`, `NR`, `NF` | Other administrative/allied visit types observed in the raw data | Not referenced by the current reporting script — pass through unfiltered if you query `outpatient` without a `visit_type` filter |

## Patient class

Resolve `class` through `pt_class_abc` (see `references/pt-class-lookup.md`) to get `class_abc` and `class_abc_moh`, then collapse to `pat_class` for outpatient reporting:

```sql
-- Step 1: class_abc (financial) -- see pt-class-lookup.md for full CASE

-- Step 2: class_abc_moh (MOH-facing) -- see pt-class-lookup.md for full CASE

-- Step 3: collapse to pat_class (used in Monthly_Att_TrtOU etc.)
CASE
  WHEN class_abc IN ('A1','B1','Private') THEN 'Private'
  WHEN class_abc IN ('B2','C','Subsidized') THEN 'Subsidized'
  ELSE class_abc
END AS pat_class
```

## Example: monthly new vs repeat trend

```sql
SELECT
  DATE_TRUNC('month', visit_date) AS month,
  SUM(CASE WHEN visit_type IN ('FV','FW','DF','FD') THEN 1 ELSE 0 END) AS new_visits,
  SUM(CASE WHEN visit_type IN ('RV','RW','DR','RD') THEN 1 ELSE 0 END) AS repeat_visits
FROM outpatient
WHERE (status != 'P' OR status IS NULL)
  AND visit_type IN ('FV','RV','FW','RW','DF','DR','FD','RD')
  AND (trt_cat != 'NC' OR trt_cat IS NULL OR sub_specialty_id IN ('LSHAPROS','LSHADEN','LSHAGDEN','LSHAGDGD'))
  AND visit_date >= '2024-01-01'
GROUP BY 1 ORDER BY 1;
```

Add `AND prelim_flag = 'N'` only if the user explicitly asks to exclude provisional/preliminary records — don't filter on it by default.

## Join to procedure

Use the candidate join in `references/data-ontology.yaml` and validate counts for the requested period.

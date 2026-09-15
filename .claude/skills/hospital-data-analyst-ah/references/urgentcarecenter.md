---
name: ah-analytics-urgentcarecenter
description: Column reference and SQL guidance for the ah-analytics urgentcarecenter table (Combined_UCC — Urgent Care Centre / A&E attendances). Use when writing SQL against the urgentcarecenter table, or when the user asks about emergency attendances, triage acuity, ED waiting times, UCC case end disposition, or A&E volume at Alexandra Hospital.
---

# AH Analytics — urgentcarecenter table (UCC / A&E)

**One row per attendance. Primary date: `visit_date`.**

**Column names are lowercase and unquoted in this table** — like `outpatient`, and unlike admission/discharge/inflight/procedure, `urgentcarecenter`'s live columns in `ah-analytics` are plain lowercase snake_case. Don't quote them, and don't trust the casing from the raw sample CSV (`Combined_UCC_1000.csv`) — that file is the pre-load export and uses different (mostly UPPERCASE, with its own quirks like `NON-ED_DEPT_OU` and `BED_CLASS__SHORT`) header names than the actual queryable table.

## Query baseline

Use the `urgentcarecenter` filters and canonical date in `references/data-ontology.yaml`.

## Identifiers

`case_no` is populated broadly across both eras, including most NGEMR-era rows — don't use its presence/absence to detect era. `pat_enc_csn_id` is the clean era switch: populated only for NGEMR-era rows, always null for SAP-era rows — use it as the primary identifier for NGEMR-era attendances and for the admission join. `sap_ip_case_no`, despite the name, is **not** a legacy-era field — it's null for every SAP-era row and populated only for NGEMR-era rows that resulted in an inpatient admission. Per `data-ontology.yaml`'s `do_not_join`, it does not match `admission.case_no` on live data — don't join on it.

## Key columns

| Column | Type | Meaning |
|--------|------|---------|
| `case_no` | TEXT | Broadly-populated case identifier, both eras — see identifiers note above. |
| `visit_date` | TIMESTAMP | Date of attendance — primary date filter |
| `visit_time` | TIME | Arrival/registration time |
| `case_end_type` | TEXT | Discharge disposition — see full mapping below |
| `consult_acuity` | TEXT | Current production acuity field (see below) |
| `triage_acuity` | TEXT | Triage-stage acuity — still populated but no longer the primary reporting field, see below |
| `pacs` | TEXT | Most complete acuity field — the ultimate fallback for both of the above |
| `arrival_mode` | TEXT | `Walk In` (most common), `Police Vehicle`, `Private Ambulance`, `993 Ambulance`, `SCDF Ambulance`, `Ambulance (Others)` |
| `att_phy_name` | TEXT | Attending physician name |
| `att_phy_mcr_no` | TEXT | Attending physician MCR |
| `pri_diag_code` | TEXT | Primary diagnosis ICD code |
| `pat_enc_csn_id` | TEXT | NGEMR encounter identifier — see identifiers note above. |
| `sap_ip_case_no` | TEXT | See identifiers note above — do not join it to live `admission.case_no`. |
| `gender` | TEXT | `Male` / `Female` |
| `pat_age` | NUMERIC | Age at visit |
| `trauma` | TEXT | `Non-Trauma/Emergency` (majority), `Trauma/Emergency`, `Non-Trauma/Non-Emergency`, `Trauma/Non-Emergency` |
| `residency` | TEXT | `SG`, `FR`, `PR`, `FNR`, `Others` — NGEMR-only, null for SAP-era rows |
| `event_arrival_time` | TIMESTAMP | Actual arrival timestamp |
| `triage_start_time` | TIMESTAMP | Triage start |
| `triage_end_time` | TIMESTAMP | Triage end |
| `hospital_admission_dttm` | TIMESTAMP | Time admitted to inpatient (if admitted) |
| `ip_bed_request_time` | TIMESTAMP | When inpatient bed requested |
| `ip_admit_time` | TIMESTAMP | When inpatient bed assigned |
| `ed_departure_dttm` | TIMESTAMP | Time patient left ED |
| `ed_lodger_flag` | TEXT | `Y` / `N` / null — flags whether the patient is an ED lodger (boarding in ED, typically awaiting an inpatient bed) |
| `prelim_flag` | TEXT | `'Y'`/`'N'` — see `global_rules.preliminary_records`/`preliminary_disclosure` in `data-ontology.yaml` |
| `cnt` | INTEGER | Always 1 |

## Additional columns

Documented for completeness — not needed for the standard acuity/case-end-type workload above, but real and available if a question calls for them. Population rates are from the pre-load sample (`Combined_UCC_1000.csv`, 1,000 rows); they should carry over to the live table since the ETL only renames columns, not values, but treat them as approximate.

| Column | Type | Meaning |
|---|---|---|
| `visit_type` | TEXT | Attendance-type code, e.g. `EN` (97% of rows), `EA`, `RE` (100% populated) |
| `ext_pat_id` | TEXT | External patient identifier (100% populated) |
| `race` | TEXT | Chinese/Malay/Indian/Others — casing inconsistent in raw data (`CHINESE` vs `Chinese`) (100% populated) |
| `referral_type` | TEXT | Referral source category, e.g. `Self` (majority), `Pte Practitione`, `GP First` (100% populated) |
| `referral_hospital` | TEXT | Referral source name, e.g. `Self-referral/walk-in`, `Police`, an external clinic — distinct from `referral_type` (100% populated) |
| `pacs_start_date` / `pacs_start_time` / `pacs_end_date` / `pacs_end_time` | DATE/TIMESTAMP | PACS episode start/end (99.8% populated) |
| `pri_diag_desc` | TEXT | Description pairing with `pri_diag_code` (99.5% populated) |
| `subvention_doc_type` | TEXT | Subvention/subsidy document type, e.g. `SG PINK IC/BC`, `SG BLUE IC` (54% populated) |
| `ed_episode_id` | TEXT | ED episode identifier (already in `data-ontology.yaml`'s `identifiers` list for this table, but wasn't previously documented here) (54% populated) |
| `ed_disposition_dttm` | TIMESTAMP | Disposition decision time (54% populated) |
| `edtu_admit_time` | TIMESTAMP | EDTU (ED Treatment/Observation Unit) admit time (1.6% populated — rare) |
| `ed_discharge_time` | TIMESTAMP | ED discharge time (45% populated) |
| `ed_departure_time` | TIMESTAMP | ED departure time — distinct from `ed_departure_dttm` above; check which one a specific report actually uses before assuming they're interchangeable (54% populated) |
| `first_ip_adm_ou` | TEXT | First inpatient admitting department/OU, if admitted (9.9% populated) |
| `first_ip_adm_bed` | TEXT | First inpatient admitting bed, if admitted (9.9% populated) |
| `postal` | TEXT | Patient postal code, including sentinel values like `999999` (54% populated) |
| `event_lab_ordered` | TIMESTAMP | Time labs were ordered (32% populated) |
| `event_rad_ordered` | TIMESTAMP | Time radiology was ordered (27% populated) |
| `event_ct_ordered` | TIMESTAMP | Time CT was ordered (3.8% populated — rare) |
| `trauma_type` | TEXT | Short code pairing with `trauma`, e.g. `NE`, `TE` (54% populated) |
| `non_ed_dept_ou` / `non_ed_dept_ou_desc` | TEXT | Department code/name for attendances routed outside ED (9.9% populated) |
| `bed_class_short` | TEXT | Short accommodation class code, e.g. `B2`, `C`, `B1` (9.8% populated — rare) |
| `bed_class` | TEXT | Full accommodation/fee class label, e.g. `A&E Fees`, `C Class L3`, `B2 Class L3` (54% populated) |

## Acuity — resolve in priority order

**Default: when a user asks for "acuity" without specifying which, use Consult Acuity (derived) below — never a raw `consult_acuity`, `triage_acuity`, or `pacs` column alone.**

Production pivots now use **`consult_acuity`** as the primary field, not `triage_acuity` (changed from `triage_acuity`). The current production-consistent acuity — **Consult Acuity (derived)**, the default field described above — falls back to `triage_acuity`, then `pacs`, when `consult_acuity` is blank:

```sql
-- Consult Acuity (derived) -- default acuity field
COALESCE(NULLIF(consult_acuity, ''), NULLIF(triage_acuity, ''), pacs) AS acuity
```

**Both fallback fields need their own `NULLIF`, not just the first one** — a bare `COALESCE(NULLIF(consult_acuity, ''), triage_acuity, pacs)` treats a blank (empty-string) `triage_acuity` as a real, present value and stops there instead of falling through to `pacs`, silently under-using the most complete field. Every worked example in this file uses the fully-guarded form above.

`triage_acuity` is a separate field, still real and still filled with its own fallback chain (`triage_acuity` → `consult_acuity` → `pacs`) if you specifically need the triage-stage acuity rather than the consult-stage one used in current reports:

```sql
COALESCE(NULLIF(triage_acuity, ''), NULLIF(consult_acuity, ''), pacs) AS triage_acuity
```

Both `triage_acuity` and `consult_acuity` can be null before this fallback; `pacs` alone is the most consistently populated of the three.

## case_end_type — full mapping

Production only relabels these specific raw values; **everything else passes through unchanged** — the raw feed has many more distinct values than the relabeled set, and most of them are never touched:

| Raw value | Relabeled to |
|---|---|
| `Admitted` | `Admit` |
| `Admit to Other Hosp` | `Decant` |
| `Patient discharged` | `Discharged` |
| `Dis. against Advice` | `AMA/AOR` |
| `Follow-up at SOC` | `Direct to Subspecialty` |
| `Trans followup at ano A&E` | `Transfer to Other ED` |
| `Dis to Community Hosp` | `Discharge to Community Hosp` |
| `Death Non-Coroner` | `Death Non-Coroners` |
| `Death Coroner` | `Death Coroners` |

**Machine-readable copy:** `case-end-type-lookup.json` in this same folder mirrors this
table 1:1 (all 36 confirmed raw values -- the 9 relabeled, the 9 that already equal their
target, and the 18 pure passthroughs below) and is what validation scripts parse -- if you
edit the table above, update the json too. (This lookup maps data *values*, not column
names, so it's unaffected by the lowercase-column-name correction elsewhere in this file.)

```sql
CASE case_end_type
  WHEN 'Admitted'                   THEN 'Admit'
  WHEN 'Admit to Other Hosp'        THEN 'Decant'
  WHEN 'Patient discharged'         THEN 'Discharged'
  WHEN 'Dis. against Advice'        THEN 'AMA/AOR'
  WHEN 'Follow-up at SOC'           THEN 'Direct to Subspecialty'
  WHEN 'Trans followup at ano A&E'  THEN 'Transfer to Other ED'
  WHEN 'Dis to Community Hosp'      THEN 'Discharge to Community Hosp'
  WHEN 'Death Non-Coroner'          THEN 'Death Non-Coroners'
  WHEN 'Death Coroner'              THEN 'Death Coroners'
  ELSE case_end_type
END AS case_end_type
```

**Complete raw-value list (confirmed) — everything below passes through the `ELSE` unchanged:**

Passthrough values with no relabeling rule: `Trans. to NUHS hosp`, `Discharge to Police`, `Follow-up at PHC`, `Follow-up at GP`, `Trans. to NHG Hospital`, `Trans. to Singhealth Hosp`, `Others`, `Absconded`, `Trans. to private Hosp`, `Discharge to Nursing Home`, `Discharge to Prison`, `13 Months Auto Case End`, `Auto Case End (A&E)`, `Admission No-Show`, `Discharge to DRC`, `Death on Arrival NCoroner`, `Admit to EDTC/EDTU`, `Admit to DS`.

Raw values that already match a relabeled target (land on the same value the mapping table would produce anyway): `Decant`, `Discharged`, `Admit`, `Transfer to Other ED`, `Direct to Subspecialty`, `AMA/AOR`, `Discharge to Community Hosp`, `Death Non-Coroners`, `Death Coroners`.

`case_end_type` can also be null. Group the passthrough values deliberately if the user needs a clean disposition breakdown.

`Cancelled` rows are removed entirely before this mapping runs (see the standard filter in `data-ontology.yaml`), and never appear in the final `case_end_type`.

## Time interval calculations

```sql
-- Door-to-triage (minutes)
EXTRACT(EPOCH FROM (triage_start_time - event_arrival_time)) / 60 AS door_to_triage_mins

-- ED length of stay (hours)
EXTRACT(EPOCH FROM (ed_departure_dttm - event_arrival_time)) / 3600 AS ed_los_hours

-- Wait for inpatient bed (minutes)
EXTRACT(EPOCH FROM (ip_admit_time - ip_bed_request_time)) / 60 AS bed_wait_mins
```

## Example: monthly attendance by acuity and arrival mode

```sql
SELECT
  DATE_TRUNC('month', visit_date) AS month,
  COALESCE(NULLIF(consult_acuity, ''), NULLIF(triage_acuity, ''), pacs) AS acuity,
  arrival_mode,
  COUNT(*) AS attendances
FROM urgentcarecenter
WHERE case_end_type != 'Cancelled'
  AND att_phy_name != 'CANCELLATION'
  AND visit_date >= '2024-01-01'
GROUP BY 1, 2, 3 ORDER BY 1, 4 DESC;
```

Add `AND prelim_flag = 'N'` only if the user explicitly asks to exclude provisional/preliminary records — don't filter on it by default.

## Join to admission

Use the `pat_enc_csn_id` candidate join and validate its row count for the requested period. The complete join rules are in `references/data-ontology.yaml`.

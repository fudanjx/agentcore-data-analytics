---
name: ah-analytics-moh-f40
description: Column reference and SQL guidance for the ah-analytics moh_f40 table — MOH F40 SOC first-visit (FV) appointment waiting time, in days from appointment creation to scheduled appointment. Use for FV wait median, 95th percentile, max, or eligible FV volume at AH, by MOH specialty, subsidy or source-of-referral code. Not for FV attendance workload (outpatient) or in-clinic consultation wait (moh_f41).
---

# AH Analytics — moh_f40 (MOH F40 first-visit appointment wait)

**One row per SOC appointment, all visit types. Primary date: `appt_creation_date`. Unit: days.** Subsidised and private patients are both included.

Columns are lowercase snake_case. Two contain `+` and must be double-quoted: `"performing_ou_visit_type_+_desc"`, `"specialty_ou_visit_type_+_desc"`. Run `describe_table` once to confirm date types (sample export shows `YYYY-MM-DD 00:00:00.000000 UTC`).

## Standard filters

```sql
WHERE visit_type_code IN ('FV','DF','FD')
  AND appt_status_desc <> 'Cancelled'
  AND appt_creation_rationale IN ('Earliest Possible Appointment','Forcebook')
  AND NOT (performing_ou_code = 'LSWELL'
           AND regexp_like(lower(COALESCE(comments, '')), 'research|-emp|longev|heal'))
```

The LSWELL line drops research, pre-employment, and Longevity wellness bookings. It mirrors `F40_Code.py` exactly, including `heal` (which also matches "health"). Keep it as written.

Expect heavy exclusion: most FV rows are `Doctor/Patient Requested Appointment`. That is the MOH definition, not a data gap.

## Verified facts (sample, 1,000 rows, Feb 2025–Aug 2026)

- `waiting_time` = `date_diff('day', appt_creation_date, scheduled_appt_date)` for every row — use the column directly.
- `reporting_period` (e.g. `Oct 2025`) = month of `appt_creation_date`; `source_file` = `F40_YYYYMM_Mon.xlsx`.
- `epic_csn` is unique per row. `epic_csn`, `nric`, `patient_id` are encrypted on upload with the same encryption as other tables, so they work as join keys (e.g. `epic_csn` → `outpatient.pat_enc_csn_id`; see `data-ontology.yaml` candidate_joins).

## Key columns

| Column | Meaning |
|---|---|
| `epic_csn` | Appointment identifier (encrypted) |
| `appt_creation_date` | Booking date — primary date |
| `scheduled_appt_date` | Appointment date |
| `waiting_time` | Days, creation → scheduled appointment |
| `visit_type_code` / `visit_type_desc` | `FV` First Visit, `DF` Telehealth Doctor FV – Video, `FD` Telehealth Doctor FV – Phone |
| `appt_status_desc` | `Completed`, `Booked`, `Did Not Attend`, `Arrived`, `Cancelled` |
| `appt_creation_rationale` | Booking rationale (filter above) |
| `rescheduled_y_n` | `Y` / `N` |
| `residency` | `SG`, `PR`, `FR`, `FNR` (~5% null) |
| `patient_class_code` | Raw class → MOH Subsidy Code (map below) |
| `private_subsidized` | `SUBSIDISED` / `PRIVATE` (source grouping; use Subsidy Code for MOH output) |
| `"performing_ou_visit_type_+_desc"` | Perf. OU = text right of ` + ` → MOH Specialty Code / `moh_specialty` (map below) — **the default specialty** |
| `"specialty_ou_visit_type_+_desc"` | Sub-specialty + visit type, then ` + ` description (e.g. `NUHS SPINE SURGERY FV + Spine Surgery - First Visit`). Finer than MOH specialty — use for sub-specialty breakdowns such as Spine cases, not as the default specialty |
| `performing_ou_code` | Specialty code; reporting uses the Perf. OU text map |
| `referral_hosp_desc` | Referral institution → Source of Referral Code (map below) |
| `referral_type_desc` | Referral source grouping (source system) |
| `performing_physician_name` | Physician |
| `treatment_ou_code` | Do not report on; use `treatment_ou_desc` |

## MOH code mappings (source: `F40 Lookup Tables.xlsx`)

Unmatched values get `Unmapped` — never drop them (category_completeness).

- **Specialty Code**: `lower(trim(element_at(split("performing_ou_visit_type_+_desc", ' + '), -1)))` → `spec` CTE below. Not in the map (non-clinical, outside F40 scope): urgent care, pharmacy, podiatry, diagnostic imaging, nursing community, care & counselling, dietetics, clinical support svc.
- **Subsidy Code**: `patient_class_code` → `sub` CTE below (`1` subsidised, `2` private/non-resident). This is wider than the script's 6-class lookup (Tammy's decision, 2026-10-07).
- Source script `F40_Code.py` keeps rows with blank specialty or subsidy codes; it does not drop them.
- **Source of Referral Code**: 172 entries in `references/moh-f40-referral-codes.json` — too long for SQL. Only when a referral breakdown is requested, export the base rows and map in Code Interpreter:

```python
codes = json.load(open("references/moh-f40-referral-codes.json"))["codes"]
key = df["referral_hosp_desc"].str.replace(r"\s*\{.*$", "", regex=True).str.strip().str.lower()
df["source_of_referral_code"] = key.map(codes).fillna("Unmapped")
```

The source script asks staff to enter missing referral codes by hand and appends them to `F40 Lookup Tables.xlsx`, so this JSON can lag behind. Report any `Unmapped` referral names so the JSON can be refreshed from the lookup.

## Useful breakdowns

- **Forcebooked**: `appt_creation_rationale = 'Forcebook'` vs `Earliest Possible Appointment`.
- **Telehealth**: `visit_type_code IN ('DF','FD')` → `Y`, else `N`.
- **Spine** (sub-group of Orthopaedic Surgery, shown on its own ops sheet): `upper(split_part("specialty_ou_visit_type_+_desc", '+', 1)) LIKE '%SPINE%'`.
- **Well**: `performing_ou_code = 'LSWELL'` (own ops sheet).

## MOH submission columns (`F40AHM{mm}Y{yyyy}.xlsx`, one file per creation month)

| Column | Value |
|---|---|
| Hospital | `AH` |
| Report Period | `Mon-YYYY` of `appt_creation_date` |
| Specialty Code | spec map |
| Date of Request | `appt_creation_date` (`dd-mm-yyyy`) |
| Source of Referral Code | referral JSON |
| Appointment Date | `scheduled_appt_date` (`dd-mm-yyyy`) |
| Subsidy Status Code | sub map |
| Urgency Class Code | constant `2` |
| Forced Booked Appointment ? | `Y` if Forcebook, else blank |
| Did patient specify doctor ? | constant `2` |
| Waiting Time (days) | `waiting_time` |
| TeleHealth | `Y` for DF/FD, else `N` — only in the `… Telehealth.xlsx` copy, which has the same rows |

## SQL pattern (Athena)

```sql
WITH spec(perf_ou, specialty_code, specialty) AS (VALUES
  ('alex anaesthesia',1,'Anaesthesiology'),('alex chronic',13,'General Medicine'),
  ('alex chronic cardiology',3,'Cardiology'),('alex chronic general surgery',12,'General Surgery'),
  ('alex chronic plastic surgery',32,'Plastic Surgery'),('alex fast medicine',13,'General Medicine'),
  ('alex fast neurosurgery',22,'Neurosurgery'),('alex fast rehabilitation med',35,'Rehabilitation Medicine'),
  ('alex fast vascular surgery',4,'Vascular/Thoracic Surgery'),('alex ha general dentistry',6,'Dentistry'),
  ('alex ha general orthopaedic',28,'Orthopaedic Surgery'),('alex ha geriatric dentistry_pg',6,'Dentistry'),
  ('alex ha geriatric medicine',14,'Geriatric Medicine'),('alex ha hand&recon microsurg',16,'Hand Surgery'),
  ('alex ha opthalmology',10,'Ophthalmology'),('alex ha otolaryngology',9,'Otolaryngology'),
  ('alex ha prosthodontics',6,'Dentistry'),('alex ha psychological meds',33,'Psychiatric Medicine'),
  ('alex ha rehabilitation med',35,'Rehabilitation Medicine'),('alex ha urology',41,'Urology'),
  ('alex palliative care',29,'Palliative Medicine'),
  ('alex wellness',13,'General Medicine'),('alex wellness gynaecology',25,'Gynaecology')),
sub(patient_class_code, subsidy_code) AS (VALUES
  ('SUB',1),('SUBP',1),('C',1),('B2',1),('CP',1),('B2P',1),
  ('A',2),('B1',2),('B2RF',2),('CRF',2),('NR',2),('PTRF',2),('PTE',2),('PTEP',2)),
f40_report AS (
  SELECT
    f.epic_csn,
    f.nric,
    f.performing_ou_code,
    COALESCE(CAST(s.specialty_code AS varchar), 'Unmapped') AS specialty_code,
    COALESCE(s.specialty, 'Unmapped')                       AS moh_specialty,
    CAST(f.appt_creation_date AS date)                      AS appt_creation_date,
    trim(split_part(f.referral_hosp_desc, '{', 1))          AS referral_hosp_desc,
    CAST(f.scheduled_appt_date AS date)                     AS scheduled_appt_date,
    f.patient_class_code                                    AS appt_pat_class,
    COALESCE(CAST(b.subsidy_code AS varchar), 'Unmapped')   AS subsidy_code,
    f.waiting_time,
    f.visit_type_code,
    f.actual_planned_no_show_appt,
    f.appt_creation_rationale,
    CASE WHEN f.appt_creation_rationale = 'Forcebook' THEN 'Y' ELSE '' END AS forcebooked,
    CASE WHEN f.visit_type_code IN ('DF','FD') THEN 'Y' ELSE 'N' END      AS telehealth,
    f.comments,
    f.performing_physician_name,
    f.appt_entry_user_id,
    trim(split_part(f."specialty_ou_visit_type_+_desc", '+', 1)) AS subspecialty_visit_type
    -- add any other moh_f40 column the question needs (residency, referral_type_desc, treatment_ou_desc, rescheduled_y_n, ...)
  FROM moh_f40 f
  LEFT JOIN spec s ON s.perf_ou = lower(trim(element_at(split(f."performing_ou_visit_type_+_desc", ' + '), -1)))
  LEFT JOIN sub  b ON b.patient_class_code = f.patient_class_code
  WHERE f.visit_type_code IN ('FV','DF','FD')
    AND f.appt_status_desc <> 'Cancelled'
    AND f.appt_creation_rationale IN ('Earliest Possible Appointment','Forcebook')
    AND NOT (f.performing_ou_code = 'LSWELL'
             AND regexp_like(lower(COALESCE(f.comments, '')), 'research|-emp|longev|heal'))
    AND CAST(f.appt_creation_date AS date) BETWEEN DATE '2026-01-01' AND DATE '2026-03-31'
)
SELECT specialty_code, moh_specialty, COUNT(*) AS fv_volume,
       approx_percentile(waiting_time, 0.5)  AS median_days_approx,
       approx_percentile(waiting_time, 0.95) AS p95_days_approx,
       MAX(waiting_time) AS max_days
FROM f40_report GROUP BY ROLLUP ((specialty_code, moh_specialty));  -- NULL row = grand total
```

## Reporting fields

`f40_report` is a guiding base query, not a fixed output. Its fields mirror the `F40_Code.py` report (snake_case), so use them by default, under these names, for answers, extracts, drill-downs and dashboards. When the question needs other details, add any `moh_f40` column to the SELECT; keep the filters and lookups.
**Specialty rule:** the default specialty for every F40 answer, table, chart and dashboard is `moh_specialty` (with `specialty_code`), mapped from Perf. OU. `subspecialty_visit_type` (left side of `specialty_ou_visit_type_+_desc`, e.g. `NUHS SPINE SURGERY FV`) is the sub-specialty + visit type: use it when the user asks for a finer breakdown (e.g. Spine, Hip & Knee, Gastro/Hepato), nested under `moh_specialty`, and label it "Sub-specialty", not "Specialty".

Common slices: `moh_specialty`, `subsidy_code`, `source_of_referral_code`, `forcebooked`, `telehealth`, `actual_planned_no_show_appt`, Spine (`upper(subspecialty_visit_type) LIKE '%SPINE%'`).
`source_of_referral_code` is added in Code Interpreter after export (key = `lower(referral_hosp_desc)`, see the referral JSON above).

## Percentile rule (applies to moh_f40 and moh_f41)

MOH uses Excel `PERCENTILE.INC`. Athena has no `PERCENTILE_CONT` / `MEDIAN`, and `approx_percentile` is approximate. For any reported or MOH figure, export the base rows (`s3tables_execute_sql_export`) and compute in Code Interpreter with `series.quantile([0.5, 0.95])` (pandas default linear interpolation = `PERCENTILE.INC`). Label SQL-only results "approx."

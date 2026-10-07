---
name: ah-analytics-moh-f41
description: Column reference and SQL guidance for the ah-analytics moh_f41 table — MOH F41 QMS2.0 SOC consultation waiting time, in minutes from appointment/queue start to start of doctor consultation. Use for clinic/consultation wait median, P95, % within 30/60-min targets, by specialty, subsidy or investigation flag. Not for appointment lead time (moh_f40) or A&E waits (urgentcarecenter).
---

# AH Analytics — moh_f41 (MOH F41 consultation wait)

**One row per QMS2.0 consultation queue record. Primary date: `appointment_date`. Unit: minutes. Loaded per quarter (`report_quarter`, e.g. `Q2 2026`).**

**Targets:** median `wait_min` ≤ 30; P95 ≤ 60 — on retained rows after all filters. Percentiles: follow the percentile rule in `moh-f40.md` (Excel `PERCENTILE.INC`).

Columns are lowercase snake_case. No patient/encounter identifier — counts are row counts; no joins.

## Live column formats (all text)

| Column | Format | Example |
|---|---|---|
| `appointment_date` | `dd/mm/yy` | `03/06/26` = 3 Jun 2026 |
| `appointment_time`, `check_in_time`, `start_wait_time_for_consultation`, `start_serve_time_for_consultation`, `end_serve_time_for_consultation` | `HH:MM` 24h | `08:40` |
| `waiting_time` | `HH:MM` duration | `00:08` — **do not report**; use derived `wait_min` |

- Date: `date(date_parse(appointment_date, '%d/%m/%y'))`
- Minutes since midnight: `CAST(split_part(c, ':', 1) AS integer) * 60 + CAST(split_part(c, ':', 2) AS integer)`
- Clock times are compared within the same day (no midnight crossing).
- Format-drift check: `SELECT report_quarter, MIN(appt_dt), MAX(appt_dt) FROM t GROUP BY 1` must fall inside each quarter.

Other columns: `hospital`, `level_of_care`, `sap_visit_type`, `epic_specialty`, `group_ou`, `epic_department_name`, `mcr`, `provider_resource`, `session_am_pm`, `service_type_before_consultation`.

## Pipeline (order matters)

1. Keep `sap_visit_type` in the 6 doctor FV/RV labels below (the Repeat-Visit Video label uses an en dash `–`).
2. Require `epic_specialty` and `check_in_time` present.
3. Inner-join specialty map (`Class.xlsx › F41_Map_Dept`, inlined) → drop unmapped (only `LSCSOT` seen unmapped in the sample).
4. Require serve ≥ wait.
5. Derive `wait_min`; keep `wait_min <= 180`.

## SQL pattern (Athena)

```sql
WITH spec(epic_specialty, specialty_code, specialty) AS (VALUES
  ('LSANAE',1,'Anaesthesiology'),('LSCHCACA',3,'Cardiology'),('LSFAVAS',4,'Vascular/Thoracic Surgery'),
  ('LSFATHO',4,'Vascular/Thoracic Surgery'),('LSHAPERI',6,'Dentistry'),('LSHAGDGD',6,'Dentistry'),
  ('LSHAGDEN',6,'Dentistry'),('LSHAPROS',6,'Dentistry'),('LSHAENT',9,'Otolaryngology'),('LSHAOPT',10,'Ophthalmology'),
  ('LSCHROGS',12,'General Surgery'),('LSCHRO',13,'General Medicine'),('LSWELL',13,'General Medicine'),
  ('LSFAMED',13,'General Medicine'),('LSHAGERI',14,'Geriatric Medicine'),('LSHAHRM',16,'Hand Surgery'),
  ('LSFANS',22,'Neurosurgery'),('LSWEGYNA',25,'Gynaecology'),('LSHAORTH',28,'Orthopaedic Surgery'),
  ('LSHAAREC',28,'Orthopaedic Surgery'),('LSHASPIN',28,'Orthopaedic Surgery'),('LSPALL',29,'Palliative Medicine'),
  ('LSCHPLS',32,'Plastic Surgery'),('LSHAPSYM',33,'Psychiatric Medicine'),('LSFARHM',35,'Rehabilitation Medicine'),
  ('LSHARHM',35,'Rehabilitation Medicine'),('LSHAURO',41,'Urology')),
t AS (
  SELECT r.*, s.specialty_code, s.specialty,
    date(date_parse(r.appointment_date, '%d/%m/%y')) AS appt_dt,
    CAST(split_part(r.appointment_time, ':', 1) AS integer) * 60 + CAST(split_part(r.appointment_time, ':', 2) AS integer) AS appt_m,
    CAST(split_part(r.start_wait_time_for_consultation, ':', 1) AS integer) * 60 + CAST(split_part(r.start_wait_time_for_consultation, ':', 2) AS integer) AS wait_m,
    CAST(split_part(r.start_serve_time_for_consultation, ':', 1) AS integer) * 60 + CAST(split_part(r.start_serve_time_for_consultation, ':', 2) AS integer) AS serve_m
  FROM moh_f41 r
  JOIN spec s ON s.epic_specialty = r.epic_specialty
  WHERE r.sap_visit_type IN ('First Visit','Repeat Visit',
          'Telehealth Doctor (First Visit) - Video','Telehealth Doctor (Repeat Visit) – Video',
          'Telehealth Doctor (First Visit) - Phone','Telehealth Doctor (Repeat Visit) - Phone')
    AND COALESCE(r.check_in_time, '') <> ''
),
base AS (
  SELECT t.*,
    CASE WHEN appt_m >= serve_m THEN 0
         WHEN wait_m < appt_m   THEN serve_m - appt_m
         ELSE serve_m - wait_m END AS wait_min,
    CASE WHEN level_of_care IN ('Subsidised','Subsidised for PR') THEN 1 ELSE 2 END AS subsidy_code,
    CASE WHEN service_type_before_consultation IN ('Audiology','BMD','FLS','Investigation','Laser',
           'Mammogram','MRI','Refraction','Ultrasound','Visual Acuity','X-ray') THEN 1 ELSE 2 END AS investigations
  FROM t
  WHERE serve_m >= wait_m
    AND appt_dt BETWEEN DATE '2026-04-01' AND DATE '2026-06-30'
),
f41 AS (SELECT * FROM base WHERE wait_min <= 180)
SELECT specialty_code, specialty, COUNT(*) AS n,
       approx_percentile(wait_min, 0.5)  AS median_min_approx,
       approx_percentile(wait_min, 0.95) AS p95_min_approx,
       AVG(CASE WHEN wait_min <= 30 THEN 1.0 ELSE 0 END) AS pct_within_30
FROM f41 GROUP BY ROLLUP ((specialty_code, specialty));  -- NULL row = grand total
```

## Wait-time branches (`wait_min`)

| Condition | Wait |
|---|---|
| appointment ≥ service start | `0` |
| service after appointment, queue start < appointment | service start − appointment |
| service after appointment, queue start ≥ appointment | service start − queue start |

## F41 report columns

`hospital`, `report_quarter` (pass through — never substitute the output filename's quarter), `appointment_date`, `specialty_code`, `subsidy_code`, `appointment_time`, `check_in_time`, `investigations`, `start_wait_time_for_consultation`, `start_serve_time_for_consultation`, `wait_min`.

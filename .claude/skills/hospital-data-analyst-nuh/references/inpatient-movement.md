---
name: nuh-analytics-inpatient-movement
description: Column reference and SQL guidance for NUH inpatient_movement. Use when analyzing inpatient admissions, discharges, patient days, ALOS, PATIENT_TYPE, Elective, Emergency, New Born, Others, or paying and subsidised inpatient activity.
---

# NUH Analytics — inpatient_movement

## Critical identifier collision

`CURRENT_DATE` / `current_date` is a physical inpatient-table column whose name
collides with the SQL built-in current-date expression. An unquoted occurrence
is invalid for NUH inpatient analysis.

| Source | Required snapshot-column reference |
|---|---|
| RDS | `"CURRENT_DATE"` |
| S3 | `"current_date"` |

Before submitting any inpatient SQL, inspect the final SQL text. Every
occurrence of `CURRENT_DATE` or `current_date` must be enclosed in double quotes.
If any occurrence is unquoted, do not execute the query. This applies in
`SELECT`, `WHERE`, `CASE`, `DATE_TRUNC`, `GROUP BY`, `ORDER BY`, validation, and
coverage queries.

```sql
-- Correct S3
DATE_TRUNC('month', "current_date") AS month_date

-- Correct RDS
DATE_TRUNC('month', "CURRENT_DATE") AS month_date

-- Invalid: resolves to the SQL runtime date
DATE_TRUNC('month', current_date)
```

For any historical inpatient request, first confirm source coverage with the
source-specific quoted physical column. For S3 use:

```sql
SELECT
  MIN("current_date") AS min_snapshot_date,
  MAX("current_date") AS max_snapshot_date,
  COUNT(DISTINCT DATE_TRUNC('month', "current_date")) AS snapshot_months
FROM nuh.inpatient
```

Use `"CURRENT_DATE"` for the equivalent RDS query. A zero-row result, a
one-date result, or a result showing today's runtime date is not evidence that
historical data are absent unless this exact quoted coverage check confirms it.
Discard any result generated with unquoted `current_date` and rerun it with the
correct quoted identifier. Report missing history only after the quoted check.

Examples below use quoted RDS column casing; for S3, substitute the exact quoted
lowercase column name without changing the logic. For validated 2025 metrics,
use the hybrid rules below. `MOVEMENT_CAT` is text: compare with `'1'`, `'2'`,
and `'20'`, never integers.

For admissions, discharges, and patient days, always use
`DATE_TRUNC('month', "CURRENT_DATE")` as the date-range filter, grouping key,
ordering key, and displayed month. Never use `ADATE` or `DDATE` as the monthly
bucket.

| Era | Snapshot range | Episode key | Discharge category | Admission category |
|---|---|---|---|---|
| SAP | before `DATE '2025-05-01'` | `CASE_NO` | `'2'` | `'1'` |
| Epic | from `DATE '2025-05-01'` | `EPIC_CSN` | `IN ('2','20')` | `IN ('1','20')` |

## Global and metric filters

Exclude Healthy Baby records from every inpatient count:

```sql
"TREATMENT_CAT" <> 'BBW'
```

For admissions, additionally require
`DATE_TRUNC('month', "ADATE") = DATE_TRUNC('month', "CURRENT_DATE")`. This is an
eligibility filter only; it does not change the monthly bucket from
`CURRENT_DATE`. For patient days, calculate `SUM("LSTAY")`, group by
`CURRENT_DATE`, and additionally exclude `"TREATMENT_OU" NOT IN
('NW22','NWDSW','NWEDS','NWASW')`. Apply those OU exclusions only to patient
days.

Use snapshot month (`CURRENT_DATE`) for the validated discharge grouping, not
`DDATE`. Cast `"DDATE"::date` only for discharge-based ALOS arithmetic.

## Paying / subsidised discharges

Use this locked hybrid classification for a cross-era discharge breakdown:

```sql
CASE
  WHEN "CURRENT_DATE" < DATE '2025-05-01'
   AND "PATIENT_CLASS" IN ('A','AP','ARF','B1','B1P','B1RF','B2RF',
                           'CRF','NR','NRB1','PTE','PTEP','PTRF') THEN 'Paying'
  WHEN "CURRENT_DATE" < DATE '2025-05-01' THEN 'Subsidised'
  WHEN "ADM_PATIENT_CLASS_GROUP" = 'PTE' THEN 'Paying'
  WHEN "ADM_PATIENT_CLASS_GROUP" = 'SUB' THEN 'Subsidised'
  ELSE 'Unclassified'
END AS patient_class_group
```

Derive `patient_class_group` once for each eligible discharge episode, then
aggregate Paying, Subsidised, and Unclassified only by equality against that
derived value. Do not rebuild the three counts with independent Boolean
predicates, and never define Unclassified as `NOT` of a Paying or Subsidised
condition. The ordered `CASE` is exhaustive: an unlisted or null SAP
`PATIENT_CLASS` is Subsidised, while a null or newly encountered Epic
`ADM_PATIENT_CLASS_GROUP` is Unclassified.

```sql
COUNT(DISTINCT CASE WHEN patient_class_group = 'Paying' THEN episode_key END)
COUNT(DISTINCT CASE WHEN patient_class_group = 'Subsidised' THEN episode_key END)
COUNT(DISTINCT CASE WHEN patient_class_group = 'Unclassified' THEN episode_key END)
```

Use `ADM_PATIENT_CLASS_GROUP`, not `DISCH_PATIENT_CLASS_GROUP`, for Epic. Include
unclassified records in the overall total and state them separately. One Epic
record was unclassified in August 2025; `ADM_PATIENT_CLASS_GROUP` is null for the
entire SAP era by design. At every month-and-`source_ou` grain and again at the
monthly roll-up, require Paying + Subsidised + Unclassified = total discharges.
SAP-period Unclassified must be zero.

## ALOS

Use snapshot-based ALOS by default:

```text
monthly patient days / monthly discharge count
```

Use discharge-based ALOS only when explicitly requested:

```sql
SUM(CASE WHEN "DDATE"::date - "ADATE"::date = 0 THEN 1
         ELSE "DDATE"::date - "ADATE"::date END) / COUNT(*)
```

It includes episodes admitted in earlier months; inspect the row grain first.

## OU grouping and reconciliation

For department, cluster, MOH-specialty, or subspecialty reporting, read
`subspec-mapping.md`. Use only `Dept_OU` as the source mapping field for every
SAP and Epic period; for S3 Tables use `dept_ou`. Resolve exact RDS
capitalization from schema metadata without selecting an alternative OU field.
Alias it as `source_ou` and join it to the mapping's `organizational_unit`.
Never use `DEPT_OU_DESC`, `dept_ou_desc`, or another department-description
field as a substitute for this mapping. For a requested multi-month department
output, retain every requested month and mapped department; do not replace it
with a latest-month or Top-N extract.
Do not invent or manually reconstruct mapped result rows. Use fresh SQL output
for reconciliation.

## Elective, Emergency, New Born, and Others

For an inpatient admissions or discharges breakdown by admission type, use
`PATIENT_TYPE` in RDS and `patient_type` in S3. These are the physical source
fields. Derive exactly four output categories:

```sql
CASE
  WHEN "PATIENT_TYPE" IN ('EL','SD') THEN 'Elective'
  WHEN "PATIENT_TYPE" IN ('DI','EM','SOC') THEN 'Emergency'
  WHEN "PATIENT_TYPE" = 'NB' THEN 'New Born'
  ELSE 'Others'
END AS admission_type
```

Known `Others` codes are `RA`, `SA`, and `TA`. Null and any newly encountered
code also map to `Others`. Before reporting, profile every distinct source
value. Keep four displayed categories, but state the codes and counts of null
or newly encountered values separately in the QC notes.

## CY2025 locked benchmarks

| Metric | Value |
|---|---:|
| Admissions | 74,461 |
| Discharges | 75,037 |
| Patient days | 389,331 |
| Snapshot ALOS | 5.19 |
| Paying discharges | 18,197 |
| Subsidised discharges | 56,839 |

Validate monthly roll-up, the April-to-May transition (investigate over 5%),
correct era-specific categories and keys, and Paying plus Subsidised plus stated
Unclassified equals total discharges. After any mapped load, check the SQL row
count, total, and unique OU count before reporting.

## Fail-closed dashboard workflow

For a monthly department dashboard, derive `month_date` only from
`DATE_TRUNC('month', "CURRENT_DATE")` and export one complete row per snapshot
month and `source_ou` with these columns:

```text
month_date,source_ou,admissions,discharges,patient_days,
paying_discharges,subsidised_discharges,unclassified_discharges
```

Generate every measure in SQL with the rules above. Do not calculate distinct
episodes from an already aggregated export. Ensure SAP rows use `CASE_NO`, Epic
rows use `EPIC_CSN`, and patient-day OU exclusions affect only `patient_days`.

Run `scripts/validate_inpatient_dashboard.py` with the complete export and the
bundled mapping JSON. It must confirm exact month coverage, all 277 mapping
records, non-negative integral counts, discharge-class reconciliation, mapping
and exclusion reconciliation, and any applicable locked benchmark. For a full
CY2025 range, its benchmark checks are mandatory. A missing month, unexplained
OU, total mismatch, or benchmark mismatch is a QC failure; never fill the gap.

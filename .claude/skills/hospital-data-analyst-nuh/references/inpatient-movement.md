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
lowercase column name without changing the logic. Use the era-specific episode
keys and movement categories below. `MOVEMENT_CAT` is text: compare with `'1'`,
`'2'`, and `'20'`, never integers.

For admissions, discharges, and patient days, always use
`DATE_TRUNC('month', "CURRENT_DATE")` as the date-range filter, grouping key,
ordering key, and displayed month. Never use `ADATE` or `DDATE` as the monthly
bucket.

| Era | Snapshot range | Episode key | Discharge category | Admission category |
|---|---|---|---|---|
| SAP | before `DATE '2025-05-01'` | `CASE_NO` | `'2'` | `'1'` |
| Epic | from `DATE '2025-05-01'` | `EPIC_CSN` | `IN ('2','20')` | `IN ('1','20')` |

## Canonical admission and discharge predicates

Use these predicates exactly for a range that crosses the May 2025 transition.
For a SAP-only or Epic-only range, retain only the applicable branch.

Admissions:

```sql
AND (
  ("CURRENT_DATE" < DATE '2025-05-01' AND "MOVEMENT_CAT" = '1')
  OR
  ("CURRENT_DATE" >= DATE '2025-05-01' AND "MOVEMENT_CAT" IN ('1','20'))
)
AND DATE_TRUNC('month', "ADATE") = DATE_TRUNC('month', "CURRENT_DATE")
```

Discharges:

```sql
AND (
  ("CURRENT_DATE" < DATE '2025-05-01' AND "MOVEMENT_CAT" = '2')
  OR
  ("CURRENT_DATE" >= DATE '2025-05-01' AND "MOVEMENT_CAT" IN ('2','20'))
)
```

Before executing SQL, inspect the final predicate. Never apply only
`"MOVEMENT_CAT" = '1'` or `"MOVEMENT_CAT" = '2'` to any range containing Epic
snapshots. A cross-era query must contain both date-qualified branches. Count
distinct admissions and discharges at snapshot-month plus era-appropriate
episode-key grain.

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

When one SQL query produces multiple inpatient metrics, keep the shared base
limited to the requested `CURRENT_DATE` range and the Healthy Baby exclusion.
Apply the `TREATMENT_OU` exclusions only inside the patient-days aggregation;
admissions and discharges must not inherit them. Count discharges using the
era-appropriate distinct episode key, never by summing a discharge-row flag.

Use snapshot month (`CURRENT_DATE`) for the validated discharge grouping, not
`DDATE`. Cast `"DDATE"::date` only for discharge-based ALOS arithmetic.

## Universal paying / subsidised classification

For admissions, discharges, patient days, ALOS, and any other analysis using the
inpatient table, use `PATIENT_CLASS` for every period. Do not use
`ADM_PATIENT_CLASS_GROUP`, `DISCH_PATIENT_CLASS_GROUP`, or another class-group
field as a substitute.

Use this classification before aggregating the requested measure:

```sql
CASE
  WHEN "PATIENT_CLASS" IN (
    'A','AP','ARF','B1','B1P','B1RF','B2RF',
    'CRF','NR','NRB1','PTE','PTEP','PTRF'
  ) THEN 'Paying'
  ELSE 'Subsidised'
END AS patient_class_group
```

For S3, use the exact physical field `"patient_class"`. Null, unlisted, and newly
encountered codes map to `Subsidised` through the `ELSE` branch.

Derive `patient_class_group` once on eligible rows before aggregation. For an
episode count, retain one derived class per snapshot month and era-appropriate
episode key. If an eligible episode-month has more than one derived class, fail
QC rather than choosing one. Aggregate classes only by equality against the
derived value; do not rebuild the counts with independent predicates.

```sql
COUNT(DISTINCT CASE WHEN patient_class_group = 'Paying' THEN episode_key END)
COUNT(DISTINCT CASE WHEN patient_class_group = 'Subsidised' THEN episode_key END)
```

At every reported grain and again at each roll-up, require Paying + Subsidised
to equal the corresponding overall measure. `Unclassified` is not a valid
output under this exhaustive rule. If the dashboard export retains the legacy
`unclassified_discharges` compatibility column, its value must be zero.

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
| Paying discharges | 18,548 |
| Subsidised discharges | 56,489 |

Validate monthly roll-up, the April-to-May transition (investigate over 5%),
correct era-specific categories and keys, and Paying plus Subsidised equals
total discharges. After any mapped load, check the SQL row count, total, and
unique OU count before reporting.

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
Set the retained compatibility field `unclassified_discharges` to zero.

Run `scripts/validate_inpatient_dashboard.py` with the complete export and the
bundled mapping JSON. It must confirm exact month coverage, all 277 mapping
records, non-negative integral counts, discharge-class reconciliation, mapping
and exclusion reconciliation, and any applicable locked benchmark. For a full
CY2025 range, its benchmark checks are mandatory. A missing month, unexplained
OU, total mismatch, or benchmark mismatch is a QC failure; never fill the gap.

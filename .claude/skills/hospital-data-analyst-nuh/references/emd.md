---
name: nuh-analytics-emd
description: Column reference and SQL guidance for NUH emd. Use when analyzing emergency department attendance, PACS acuity, ED admissions, Adult or Children segments, or arrival mode.
---

# NUH Analytics — emd

**Count ED attendance from source rows. Primary date: `EMD_VISIT_DATE`. Coverage: January 2023–June 2026.**

## Row counting and date filter

Use `COUNT(*)` for attendance and count qualifying source rows for ED admissions.
Do not use the `DUPLICATE` field to identify or exclude duplicate records,
regardless of its value (including `Y`, `Duplicate Record`, `First Record`, or
NULL). Do not substitute a distinct ID count or apply ID-based deduplication.

Examples use quoted RDS column names. For S3, use the exact quoted lowercase
physical names, including `"emd_visit_date"`, with the same counting logic.

```sql
SELECT
  COUNT(*) AS attendance,
  COUNT(CASE WHEN "CASE_END_TYPE_DESC" LIKE 'Admit%' THEN 1 END) AS admissions
FROM emd
WHERE "EMD_VISIT_DATE" IS NOT NULL
  AND "EMD_VISIT_DATE" >= DATE '2025-01-01'
  AND "EMD_VISIT_DATE" < DATE '2026-01-01';
```

Use `EMD_VISIT_DATE` for date filtering and grouping. Apply this row-counting
basis consistently to all attendance breakdowns and ED admissions.

## Required segment mapping

Use `TREATMENT_OU_CLINIC`, not a text inference from OU descriptions:

```sql
CASE
  WHEN "TREATMENT_OU_CLINIC" = 'NCA&E' THEN 'Adult (NCA&E)'
  WHEN "TREATMENT_OU_CLINIC" = 'NCCE' THEN 'Children CE (NCCE)'
  WHEN "TREATMENT_OU_CLINIC" = 'NCPUCC' THEN 'Children UCC (NCPUCC)'
  ELSE 'Unknown'
END AS segment
```

In S3, `NCPUCC` is available from January 2023, including CY2023 and CY2024
data backfilled on 2026-09-15. Query and include those rows in S3 ED-attendance
tables and totals. Never apply the former rule that treated pre-2025 `NCPUCC`
as unavailable. This backfill does not establish matching RDS coverage; for an
RDS request, query the requested period and report the observed coverage rather
than assuming S3/RDS parity.

## PACS rules

Default to `PACS_STATUS_CONSULT` for PACS reporting. Use `PACS_STATUS` only if
the user explicitly asks for initial-triage PACS, and `TRIAGE_ACUITY` only if
explicitly requested. PACS levels are `P1`–`P4`.

Exclude null PACS only from a PACS breakdown, not from total or segment attendance:

```sql
AND "PACS_STATUS_CONSULT" IS NOT NULL
```

In the NUH RDS and S3 row-based checks on 2026-08-31, the default and initial-triage PACS
fields differ for 129 of 85,972 H1-2026 records (0.15%), mainly at the P2/P3
boundary. This uses a NULL-safe comparison (`IS DISTINCT FROM`); neither field
was NULL in that check. Segment totals must remain equal.

## ED admissions and arrival mode

Identify ED admissions with `"CASE_END_TYPE_DESC" LIKE 'Admit%'`; do not list
individual admission subtypes.

For arrival-mode reports, use the current documented `ARRIVAL_MODE_DESC` field.
Inspect distinct values and nulls, and verify the resulting groups sum to the
base-filtered ED attendance. Do not carry forward an undocumented raw
`ARRIVAL_MODE` code normalisation from older instructions.

## Patient gender and unique patients

Apply these rules to every EMD output format. Use `SEX` in RDS and `sex` in S3:

```sql
CASE
  WHEN UPPER(TRIM(CAST("SEX" AS VARCHAR))) = 'M' THEN 'Male'
  WHEN UPPER(TRIM(CAST("SEX" AS VARCHAR))) = 'F' THEN 'Female'
  ELSE 'Others'
END AS gender_group
```

Map `U`, null, blank, and every unexpected value to `Others`. Require
`Male + Female + Others = source attendance` at every reported grain.

Use `HRN` in RDS and `hrn` in S3 for a distinct EMD patient count:

```sql
COUNT(DISTINCT NULLIF(TRIM(CAST("HRN" AS VARCHAR)), ''))
```

Use the exact quoted lowercase fields `"sex"` and `"hrn"` for S3. Keep the
distinct-patient measure separate from row-counted attendance and admissions.
Do not display individual HRNs.

## Example: monthly PACS attendance

```sql
SELECT
  DATE_TRUNC('month', "EMD_VISIT_DATE") AS month,
  CASE
    WHEN "TREATMENT_OU_CLINIC" = 'NCA&E' THEN 'Adult (NCA&E)'
    WHEN "TREATMENT_OU_CLINIC" = 'NCCE' THEN 'Children CE (NCCE)'
    WHEN "TREATMENT_OU_CLINIC" = 'NCPUCC' THEN 'Children UCC (NCPUCC)'
    ELSE 'Unknown'
  END AS segment,
  "PACS_STATUS_CONSULT" AS pacs,
  COUNT(*) AS attendance
FROM emd
WHERE "EMD_VISIT_DATE" >= DATE '2025-01-01'
  AND "EMD_VISIT_DATE" < DATE '2026-01-01'
  AND "PACS_STATUS_CONSULT" IS NOT NULL
GROUP BY 1, 2, 3
ORDER BY 1, 2, 3;
```

## Locked benchmarks — unaffected periods

Verified independently against NUH RDS and S3 `emd` on 2026-08-31 using the row-counting rule above,
without a duplicate-status filter or ID-based deduplication. Monthly roll-ups
and segment sums reconcile to independent period totals (12 months for CY2025
and 6 months for H1 2026).

NUH RDS and S3 results match for the periods and segments below, the CY2025
admission figures and rates, and the H1-2026 PACS comparison above.

| Period | Adult | Children CE | Children UCC | Total |
|---|---:|---:|---:|---:|
| CY2025 | 111,113 | 38,472 | 19,899 | 169,484 |
| H1 2026 | 55,814 | 19,790 | 10,368 | 85,972 |

The former CY2023 and CY2024 benchmark rows are retired because their totals
predated the S3 `NCPUCC` backfill and excluded Children UCC. For S3 CY2023 or
CY2024 reporting, obtain all three segment counts from a fresh query, calculate
the total from that same result, and reconcile monthly, segment, and grand
totals. Do not reuse the former totals or label Children UCC unavailable. Do not
compare those S3 totals with RDS unless RDS coverage has been independently
verified for the requested period.

CY2025 total ED admissions are 45,520 in both sources. Admission rates use the attendance
denominator for the same requested segment: Adult 39,337 / 111,113 = 35.40%;
Children CE alone 6,183 / 38,472 = 16.07%; combined Children CE + UCC
6,183 / 58,371 = 10.59%. Do not use the CE-only rate for combined Children.

## QC

For a matching period, confirm: monthly roll-up equals annual total; segment total
equals the grand total; PACS P1–P4 plus separately stated null PACS equals segment
total; and arrival-mode groups equal total attendance. Pipe SQL output directly
into analysis; do not manually reconstruct results, and re-query SQL when reconciling.

---
name: nuh-analytics-emd
description: Column reference and SQL guidance for the NUH emd table. Use when analyzing NUH emergency department attendance, ED admissions, ED disposition, or adult versus children ED activity.
---

# NUH Analytics — emd table

**One row is an ED visit. Primary date: `EMD_VISIT_DATE`.**

The supplied logic is QC-verified for January–December 2025 and states that this
table has one source system for that period.

## Mandatory base filter

```sql
FROM emd
WHERE "EMD_VISIT_DATE" IS NOT NULL
  AND "DUPLICATE" <> 'Y'
```

`"DUPLICATE" <> 'Y'` is mandatory. Omitting it overcounts attendance.

## Core fields

| Column | Use |
|---|---|
| `EMD_VISIT_DATE` | Attendance date and monthly time series |
| `DUPLICATE` | Exclude `Y` records |
| `CASE_END_TYPE_DESC` | ED disposition; identify admissions with `LIKE 'Admit%'` |
| `TREATMENT_OU_DESC` | Adult/children segmentation input |

## Metrics

### ED attendance

Count all rows remaining after the mandatory base filter.

```sql
SELECT
  DATE_TRUNC('month', "EMD_VISIT_DATE") AS month,
  COUNT(*) AS attendance
FROM emd
WHERE "EMD_VISIT_DATE" IS NOT NULL
  AND "DUPLICATE" <> 'Y'
  AND "EMD_VISIT_DATE" >= DATE '2025-01-01'
  AND "EMD_VISIT_DATE" < DATE '2026-01-01'
GROUP BY 1
ORDER BY 1;
```

### ED admissions

Apply the base filter and identify all admission dispositions with:

```sql
AND "CASE_END_TYPE_DESC" LIKE 'Admit%'
```

Do not hard-code individual `Admit` subtypes. The documented admission values
include `Admit - Ward`, `Admit - ICU`, and `Admit - HDU`.

### Adult versus children

Use `TREATMENT_OU_DESC`. The source material says child-specific departments are
identified by their OU descriptions, but does not provide the exact allowed value
list. First inspect the distinct non-null values and obtain or state a value-level
mapping before labelling a series as Adult or Children. Do not use a speculative
text match as a validated mapping.

The supplied document also gives adult and children admission-rate reference
figures while its QC note says to use total attendance as the rate denominator.
This is ambiguous for a segment-specific rate. Confirm the intended denominator
before reporting an Adult or Children admission rate.

## 2025 locked benchmarks

| Metric | Value |
|---|---:|
| Total ED attendance | 149,576 |
| Total ED admissions | 45,512 |
| Adult admission-rate reference | about 35.7% |
| Children admission-rate reference | about 15.7% |

## QC checks

For an exactly matching 2025 query:

1. Calculate totals in SQL, not by manual arithmetic.
2. Confirm monthly attendance sums to the full-year attendance total.
3. After a verified adult/children mapping, confirm Adult plus Children equals total attendance.
4. Investigate mismatches before calling the result QC-passed.

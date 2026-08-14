---
name: nuh-analytics-emd
description: Column reference and SQL guidance for NUH emd. Use when analyzing emergency department attendance, PACS acuity, ED admissions, Adult or Children segments, or arrival mode.
---

# NUH Analytics — emd

**One row is one ED visit. Primary date: `EMD_VISIT_DATE`. Coverage: January 2023–June 2026.**

## Mandatory base filter

```sql
FROM emd
WHERE "EMD_VISIT_DATE" IS NOT NULL
  AND "DUPLICATE" <> 'Y'
```

Never omit the duplicate filter. Use `EMD_VISIT_DATE` for date filtering and grouping.

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

`NCPUCC` is available only from January 2025; report it as unavailable, not zero, for 2023–2024.

## PACS rules

Default to `PACS_STATUS_CONSULT` for PACS reporting. Use `PACS_STATUS` only if
the user explicitly asks for initial-triage PACS, and `TRIAGE_ACUITY` only if
explicitly requested. PACS levels are `P1`–`P4`.

Exclude null PACS only from a PACS breakdown, not from total or segment attendance:

```sql
AND "PACS_STATUS_CONSULT" IS NOT NULL
```

The default and initial-triage PACS fields differ for 129 of 85,970 H1-2026
records (0.15%), mainly at the P2/P3 boundary. Segment totals must remain equal.

## ED admissions and arrival mode

Identify ED admissions with `"CASE_END_TYPE_DESC" LIKE 'Admit%'`; do not list
individual admission subtypes.

For arrival-mode reports, use the current documented `ARRIVAL_MODE_DESC` field.
Inspect distinct values and nulls, and verify the resulting groups sum to the
base-filtered ED attendance. Do not carry forward an undocumented raw
`ARRIVAL_MODE` code normalisation from older instructions.

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
  AND "DUPLICATE" <> 'Y'
  AND "PACS_STATUS_CONSULT" IS NOT NULL
GROUP BY 1, 2, 3
ORDER BY 1, 2, 3;
```

## Locked benchmarks

| Period | Adult | Children CE | Children UCC | Total |
|---|---:|---:|---:|---:|
| CY2023 | 107,235 | 40,613 | unavailable | 147,848 |
| CY2024 | 100,600 | 36,149 | unavailable | 136,749 |
| CY2025 | 111,108 | 38,468 | 19,899 | 169,475 |
| H1 2026 | 55,813 | 19,789 | 10,368 | 85,970 |

CY2025 total ED admissions are 45,512. Segment admission rates use their own
segment attendance denominator; the cited adult and children references are about
35.7% and 15.7% respectively.

## QC

For a matching period, confirm: monthly roll-up equals annual total; segment total
equals the grand total; PACS P1–P4 plus separately stated null PACS equals segment
total; and arrival-mode groups equal total attendance. Pipe SQL output directly
into analysis; do not manually reconstruct results, and re-query SQL when reconciling.

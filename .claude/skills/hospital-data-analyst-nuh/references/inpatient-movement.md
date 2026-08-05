---
name: nuh-analytics-inpatient-movement
description: Column reference and SQL guidance for the NUH inpatient_movement table. Use when analyzing NUH inpatient admissions, discharges, patient days, SAP-to-Epic transition rules, or average length of stay.
---

# NUH Analytics — inpatient_movement table

**Monthly snapshot date: `CURRENT_DATE`. Validated scope: January–December 2025.**

Always quote `"CURRENT_DATE"`. This 2025 source has two eras:

| Era | Snapshot period | Distinct episode key |
|---|---|---|
| SAP | `"CURRENT_DATE" < DATE '2025-05-01'` | `CASE_NO` |
| Epic | `"CURRENT_DATE" >= DATE '2025-05-01'` | `EPIC_CSN` |

## Global rule

Exclude Healthy Baby records from every inpatient count unless the user explicitly
asks to include them:

```sql
"TREATMENT_CAT" <> 'BBW'
```

## Metric 1 — discharge count

Use the snapshot month and era-specific distinct key:

```sql
SELECT
  DATE_TRUNC('month', "CURRENT_DATE") AS month,
  COUNT(DISTINCT "CASE_NO") AS discharges
FROM inpatient_movement
WHERE "MOVEMENT_CAT" = 2
  AND "TREATMENT_CAT" <> 'BBW'
  AND "CURRENT_DATE" >= DATE '2025-01-01'
  AND "CURRENT_DATE" < DATE '2025-05-01'
GROUP BY 1

UNION ALL

SELECT
  DATE_TRUNC('month', "CURRENT_DATE") AS month,
  COUNT(DISTINCT "EPIC_CSN") AS discharges
FROM inpatient_movement
WHERE "MOVEMENT_CAT" IN (2, 20)
  AND "TREATMENT_CAT" <> 'BBW'
  AND "CURRENT_DATE" >= DATE '2025-05-01'
  AND "CURRENT_DATE" < DATE '2026-01-01'
GROUP BY 1
ORDER BY 1;
```

The source summary labels this measure as grouped by `DDATE` month, but the locked
SQL and benchmark query group by `CURRENT_DATE`. For the validated snapshot report,
use `CURRENT_DATE`. If the user asks for actual discharge-event month by `DDATE`,
clarify that it is a different, unvalidated grouping.

## Metric 2 — admission count

Require `ADATE` to fall in the same calendar month as the snapshot. Use
`MOVEMENT_CAT = 1` in SAP and `IN (1, 20)` in Epic, with the era-specific distinct
key.

```sql
-- Add this condition to both era queries.
AND DATE_TRUNC('month', "ADATE") = DATE_TRUNC('month', "CURRENT_DATE")
```

Group by the snapshot month. Because this equality is required, grouping by
`ADATE` month produces the same monthly bucket for eligible rows.

## Metric 3 — patient days

Patient days are the sum of `LSTAY`, grouped by snapshot month. In addition to the
global BBW exclusion, exclude these treatment OUs:

```sql
SELECT
  DATE_TRUNC('month', "CURRENT_DATE") AS month,
  SUM("LSTAY") AS patient_days
FROM inpatient_movement
WHERE "TREATMENT_CAT" <> 'BBW'
  AND "TREATMENT_OU" NOT IN ('NW22', 'NWDSW', 'NWEDS', 'NWASW')
  AND "CURRENT_DATE" >= DATE '2025-01-01'
  AND "CURRENT_DATE" < DATE '2026-01-01'
GROUP BY 1
ORDER BY 1;
```

Do not apply the patient-day treatment-OU exclusions to admissions or discharges:
the source logic specifies them only for patient days.

## Metric 4 — ALOS

### Default: snapshot-based ALOS (Method 1)

Use this for general ALOS requests or requests phrased as ALOS using a month's
inpatient data:

```text
ALOS = monthly patient days / monthly discharge count
```

Use the patient-day and discharge rules above and group by `CURRENT_DATE` month.

### On request only: discharge-based ALOS (Method 2)

Use only when the user explicitly asks for ALOS based on patients discharged in a
month. The supplied formula is:

```sql
SUM(
  CASE
    WHEN "DDATE"::date - "ADATE"::date = 0 THEN 1
    ELSE "DDATE"::date - "ADATE"::date
  END
) / COUNT(*) AS alos_method2
```

This method includes episodes admitted in earlier months. Before using it, inspect
the live row grain and eligibility fields: the supplied logic does not provide a
complete deduplication rule for this alternate method.

If an ALOS request is ambiguous, ask whether the user wants snapshot-based
(default) or discharge-based ALOS.

## 2025 locked benchmarks

| Metric | Full-year value |
|---|---:|
| Admissions | 74,461 |
| Discharges | 75,037 |
| Patient days | 389,331 |
| Snapshot-based ALOS | 5.19 |

| Month | Admissions | Discharges | Patient days | ALOS (M1) |
|---|---:|---:|---:|---:|
| Jan | 5,940 | 6,026 | 30,529 | 5.07 |
| Feb | 5,970 | 5,843 | 29,479 | 5.05 |
| Mar | 5,961 | 6,107 | 30,884 | 5.06 |
| Apr | 6,269 | 6,215 | 31,072 | 5.00 |
| May | 6,079 | 6,200 | 32,294 | 5.21 |
| Jun | 6,018 | 6,001 | 32,475 | 5.41 |
| Jul | 6,448 | 6,539 | 33,711 | 5.16 |
| Aug | 6,349 | 6,494 | 33,196 | 5.11 |
| Sep | 6,479 | 6,394 | 33,086 | 5.17 |
| Oct | 6,451 | 6,529 | 34,501 | 5.28 |
| Nov | 6,168 | 6,297 | 33,247 | 5.28 |
| Dec | 6,329 | 6,392 | 34,857 | 5.45 |

## Mandatory 2025 QC

1. Confirm monthly discharges total 75,037, admissions total 74,461, and patient days total 389,331 when scope exactly matches.
2. Check the April-to-May transition. Flag and investigate a change above 5%.
3. Flag a full-year admissions/discharges difference of 2% or more for clinical investigation.
4. Do not report an exact benchmark comparison until all applicable filters and era rules have been applied.

---
name: nuh-analytics-surgery
description: Column reference and SQL guidance for the NUH surgery table. Use when analyzing NUH procedure volume, day surgery, normal delivery, inpatient surgery, or surgical-category time series.
---

# NUH Analytics — surgery table

**Count procedures, not surgical cases. Primary date: `SVISITDATE`.**

The supplied logic is QC-verified for January–December 2025 and uses one source
system for that period.

## Mandatory 2025 base filter

```sql
FROM surgery
WHERE "SVISITDATE" >= DATE '2025-01-01'
  AND "SVISITDATE" < DATE '2026-01-01'
```

This is the PostgreSQL-safe equivalent of the supplied year-2025 condition.

## Required surgical-category logic

Use `PATIENT_TYPE` only. Do not use `Surgery_Patient_Class`: the supplied source
states that it misclassifies about 8,390 cases.

| Category | Logic |
|---|---|
| Day Surgery | `"PATIENT_TYPE" = 'D'` |
| Normal Delivery | `"PATIENT_TYPE" = 'I' AND "S_CODE" IN ('SP836U', 'SI836U')` |
| Inpatient Surgery | `"PATIENT_TYPE" = 'I' AND "S_CODE" NOT IN ('SP836U', 'SI836U')` |

`SP836U` is Normal Delivery (Private); `SI836U` is Normal Delivery (Subsidised).

## Example: monthly procedures by category

```sql
SELECT
  DATE_TRUNC('month', "SVISITDATE") AS month,
  CASE
    WHEN "PATIENT_TYPE" = 'D' THEN 'Day Surgery'
    WHEN "PATIENT_TYPE" = 'I'
      AND "S_CODE" IN ('SP836U', 'SI836U') THEN 'Normal Delivery'
    WHEN "PATIENT_TYPE" = 'I'
      AND "S_CODE" NOT IN ('SP836U', 'SI836U') THEN 'Inpatient Surgery'
    ELSE 'Unclassified'
  END AS surgical_category,
  COUNT(*) AS procedures
FROM surgery
WHERE "SVISITDATE" >= DATE '2025-01-01'
  AND "SVISITDATE" < DATE '2026-01-01'
GROUP BY 1, 2
ORDER BY 1, 2;
```

Investigate any `Unclassified` records before presenting a fully classified total.

## 2025 locked benchmarks

| Category | Procedures | Share |
|---|---:|---:|
| Day Surgery | 83,467 | 66.3% |
| Normal Delivery | 2,537 | 2.0% |
| Inpatient Surgery | 39,945 | 31.7% |
| Total procedures | 125,949 | 100% |

| Month | Day Surgery | Normal Delivery | Inpatient Surgery | Total |
|---|---:|---:|---:|---:|
| Jan | 6,368 | 205 | 3,015 | 9,588 |
| Feb | 5,858 | 175 | 2,828 | 8,861 |
| Mar | 6,894 | 196 | 3,278 | 10,368 |
| Apr | 6,511 | 194 | 3,126 | 9,831 |
| May | 6,881 | 207 | 3,250 | 10,338 |
| Jun | 6,631 | 208 | 3,130 | 9,969 |
| Jul | 7,107 | 218 | 3,434 | 10,759 |
| Aug | 6,993 | 225 | 3,380 | 10,598 |
| Sep | 6,918 | 222 | 3,315 | 10,455 |
| Oct | 7,578 | 233 | 3,639 | 11,450 |
| Nov | 7,134 | 224 | 3,312 | 10,670 |
| Dec | 6,594 | 230 | 3,238 | 10,062 |

## Mandatory 2025 QC

1. Confirm Day Surgery plus Normal Delivery plus Inpatient Surgery equals 125,949.
2. Confirm monthly totals sum to 125,949.
3. Confirm no record remains unclassified under the required `PATIENT_TYPE`/`S_CODE` logic.
4. Calculate checks programmatically and investigate any mismatch before reporting.

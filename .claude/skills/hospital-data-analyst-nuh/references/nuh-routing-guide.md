---
name: nuh-analytics-data-dictionary
description: Route questions to the correct NUH table in nuh-analytics and apply the approved 2025 metric logic. Use when analyzing NUH emergency department, inpatient movement, specialist outpatient clinic, or surgery data; writing NUH SQL; or selecting date fields, filters, and validation benchmarks.
---

# NUH Analytics — routing guide

## Select the source table

| Question is about | Table | Primary date field | Row/count unit |
|---|---|---|---|
| ED attendance, ED disposition, or ED admissions | `emd` | `EMD_VISIT_DATE` | Valid ED visit |
| Inpatient admissions, discharges, patient days, or ALOS | `inpatient_movement` | `CURRENT_DATE` monthly snapshot | Metric-specific; use the documented distinct key |
| Specialist outpatient clinic activity | `soc` | `SOC_VISIT_DATE` | Actualized SOC visit |
| Surgical workload | `surgery` | `SVISITDATE` | Procedure, not case |

## Required query process

1. Read the table-level reference before querying.
2. Use the approved base filter and metric-specific filters in that reference.
3. Quote every mixed-case or reserved column name. In particular, always write `"CURRENT_DATE"`.
4. For a calendar-year query in PostgreSQL, use half-open date ranges rather than `YEAR(...)`, for example:

```sql
WHERE "SVISITDATE" >= DATE '2025-01-01'
  AND "SVISITDATE" < DATE '2026-01-01'
```

5. Use the 2025 benchmarks only when the query exactly matches the documented 2025 scope. They are not a target or forecast for other periods.

## Scope and relationship limits

- The supplied logic validates calendar year 2025. Do not assume an identical source-system era, lookup, or benchmark beyond that period.
- `inpatient_movement` changes from SAP logic through April 2025 to Epic logic from May 2025. Use the correct distinct key and `MOVEMENT_CAT` rule for each era.
- The supplied materials do not define cross-table join keys or cardinalities. Before joining NUH tables, inspect the live columns, establish row grain, and state the join assumption. Do not copy AH join logic into NUH analysis.

## Shared reporting checks

- Generate monthly and full-period totals from SQL; do not sum values manually.
- Check monthly total equals full-period total for a complete 2025 series.
- Preserve nulls and report excluded rows only when the documented filter calls for their exclusion.
- State the table, date field, filters, and aggregation unit with every result.

## Table references

- `emd.md` — ED attendance and disposition rules
- `inpatient-movement.md` — SAP/Epic inpatient measures and ALOS methods
- `soc.md` — SOC visit, class, cluster, and specialty rules
- `surgery.md` — procedure count and surgical-category rules

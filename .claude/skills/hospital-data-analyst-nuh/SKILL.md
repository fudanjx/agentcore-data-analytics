---
name: hospital-data-analyst-nuh
description: Analyze National University Hospital (NUH) operational data in the nuh-analytics database. Use when answering questions about NUH emergency-department attendance or admissions, inpatient admissions, discharges, patient days or ALOS, specialist outpatient clinic activity, or surgery procedures; when creating NUH time-series reports, charts, dashboards, or SQL analysis.
---

# NUH data analytics

Use the `nuh` data tool for NUH analytics. Query the `nuh-analytics` database's
`emd`, `inpatient_movement`, `soc`, and `surgery` tables only with read-only SQL.

## Choose the table and reference

1. Read `references/nuh-routing-guide.md` before every NUH analysis.
2. Read the matching table reference before writing SQL:
   - Emergency department attendance, ED admissions, acuity, or adult/children analysis: `references/emd.md`
   - Inpatient admissions, discharges, patient days, or ALOS: `references/inpatient-movement.md`
   - Specialist outpatient clinic visits, new/repeat, subsidy, cluster, or specialty: `references/soc.md`
   - Surgical procedures, day surgery, normal delivery, or inpatient surgery: `references/surgery.md`
3. Apply the documented base filters and metric-specific rules exactly. Do not borrow filters, identifiers, or business logic from AH data.

## Query and time-series discipline

- Use double quotes around NUH column names, especially `"CURRENT_DATE"`, which would otherwise be interpreted as a PostgreSQL expression.
- Use the documented primary date field for the metric. Filter timestamps with a half-open range, for example `>= DATE '2025-01-01' AND < DATE '2026-01-01'`, to avoid endpoint ambiguity.
- Treat source-file benchmarks as validated for calendar year 2025 only. Confirm availability before claiming coverage for another period.
- Select only columns needed for the question. Start with a grouped row count or a small date range when a query may be expensive.
- Do not infer missing mappings. In particular, inspect observed values before classifying `TREATMENT_OU_DESC` as adult or children, and do not create cross-table joins unless their keys and cardinality have been verified from the live schema.

## Validate before reporting

- Compute totals programmatically; never add displayed monthly values by hand.
- For a complete 2025 metric, check that the sum of the monthly series equals its full-year total. Compare it with the relevant locked benchmark in the table reference when the scope matches exactly.
- If a benchmark does not match, report the discrepancy and its likely scope or filter cause. Do not present the result as QC-passed.
- Preserve the distinction between procedures and cases: `surgery` reports procedures, not surgical cases.
- State the table, date field, filters, distinct key (where applicable), and any non-default ALOS method in the answer. Distinguish measured results from interpretation.

## Outputs

For a chart or dashboard, return one self-contained HTML document in a single `html` fence. Otherwise answer concisely with the query basis, result, and QC status.

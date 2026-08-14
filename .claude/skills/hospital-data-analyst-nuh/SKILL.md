---
name: hospital-data-analyst-nuh
description: Analyze National University Hospital (NUH) operational data in the nuh-analytics database. Use when answering questions about NUH emergency-department attendance, PACS, admissions or arrival mode; inpatient admissions, discharges, patient days, ALOS or paying status; specialist outpatient clinic activity; surgical procedures; department, cluster, subspecialty, or MOH-specialty workload; or NUH time-series reports, charts, and dashboards.
---

# NUH data analytics

Use the `nuh` data tool for read-only SQL against `nuh-analytics` tables `emd`,
`inpatient_movement`, `soc`, and `surgery`.

## Choose and load the right reference

1. Read `references/nuh-routing-guide.md` before every analysis.
2. Read the matching table reference before writing SQL:
   - ED attendance, PACS, disposition, admission, segment, or arrival mode: `references/emd.md`
   - Inpatient admissions, discharges, patient days, ALOS, `TYPE_GRP`, or paying/subsidised status: `references/inpatient-movement.md`
   - SOC visits, new/repeat, private/subsidised, specialty, or clinic activity: `references/soc.md`
   - Procedures, surgical category, normal delivery, emergency/elective, or surgical paying status: `references/surgery.md`
   - Department, cluster, subspecialty, MOH-specialty, or OU-level reporting for SOC, inpatient, or surgery: also read `references/subspec-mapping.md`.
   - A dashboard or self-contained HTML report: also read `references/dashboard.md`.
3. Apply the documented base filters, source-era rules, distinct keys, mapping rules, and classifications exactly. Do not copy AH logic into NUH queries.

## Query discipline

- Double-quote NUH column names. Always write `"CURRENT_DATE"` rather than the PostgreSQL system-date expression.
- Use a half-open primary-date range: `>= DATE 'YYYY-MM-DD' AND < DATE 'next-period-start'`. Never use `<=` on a timestamp endpoint or `YEAR()`.
- Do not use `UID`, `Hosp_ABBR`, or `Period` as a general date-range filter. In `surgery`, use `UID` only inside the documented hybrid classification CASE; never use it to remove an era.
- Compute totals and displayed subtotals programmatically from the same grouped result. For a complete year, verify monthly values roll up to the annual total.
- Never reconstruct or manually type analytical result rows. Use fresh SQL output as the reconciliation source; validate row count, total, and unique OU count after a mapped data load.
- Treat the current table-specific references and `subspec-mapping.md` as authoritative for their subject areas. If an older dashboard instruction or benchmark conflicts, report the conflict and do not silently substitute it.
- State the table, date field, filters, classification, distinct key (when applicable), and QC status with every result.

## Outputs

For a chart or dashboard, return one self-contained HTML document in a single `html` fence unless the user explicitly authorizes publishing it. Otherwise answer concisely with the query basis, result, and QC status.

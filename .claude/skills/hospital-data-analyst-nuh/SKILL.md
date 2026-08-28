---
name: hospital-data-analyst-nuh
description: Analyze National University Hospital (NUH) operational data in the nuh-analytics database. Use ONLY when the user explicitly mentions "NUH" or "National University Hospital". Covers ED/EMD attendance, PACS, inpatient admissions/discharges/patient days/ALOS, SOC visits, surgery, and departmental/subspecialty workload. Always load the relevant table reference before writing SQL.
---

# NUH Analytics

Use the `nuh` data tool for read-only SQL against `nuh-analytics` tables `emd`,
`inpatient_movement`, `soc`, and `surgery`.

## Scope gate

Apply this skill only when the user explicitly mentions `NUH` or `National University Hospital`. Do not apply it to a request that omits both, even if it mentions PACS, SOC, NCA&E, an NUH table, field, clinic, or OU. Do not apply it to non-analytical questions (visiting hours, directions, contact details).

## Pre-query workflow

Before using the `nuh` data tool, inspecting candidate columns, or writing SQL:

1. Select the correct table and primary date from the routing table below.
2. Read the corresponding reference file for filters, classifications, and SQL patterns.
3. If the request involves department, OU, cluster, subspecialty, or MOH specialty: also read `references/subspec-mapping.md`.
4. If the request involves a chart, visualization, or dashboard: also read `references/dashboard.md`.
5. For combined requests, load all applicable references before writing any SQL.

Apply reference-defined logic exactly. Never substitute a convenient source column for a documented classification, mapping, date rule, or distinct key.
For mapped reporting, use the table-specific OU field contract in
`references/subspec-mapping.md`; schema inspection may resolve physical casing
or source-specific names only and must never select a different semantic field.

Treat a user-specified data source as authoritative. Do not switch between S3
and RDS without the user's approval; first troubleshoot and report the failed
query using the applicable reference rules.

## Table routing

| Question | Table | Primary date | Reference |
|---|---|---|---|
| ED attendance, PACS, arrival mode, disposition, or ED admission | `emd` | `EMD_VISIT_DATE` | references/emd.md |
| Inpatient admissions, discharges, patient days, ALOS, Elective/Emergency/Transfer-In/New Born, or patient class | `inpatient_movement` | RDS `"CURRENT_DATE"` / S3 `"current_date"` snapshot; never unquoted | references/inpatient-movement.md |
| SOC visits or attendance, First/New vs Repeat, private/subsidised, clinic, or specialty | `soc` | `SOC_VISIT_DATE` | references/soc.md |
| Surgery, day surgery, normal delivery, inpatient surgery, or emergency/elective procedures | `surgery` | `SVISITDATE` | references/surgery.md |
| Department, cluster, MOH specialty, or subspecialty report | Relevant table + subspec mapping | — | references/subspec-mapping.md |
| Chart, visualization, or dashboard | Relevant table references | — | references/dashboard.md |

## Composite reference requirements

| Request | Required references |
|---|---|
| Patient days by clinical department | `inpatient-movement.md` + `subspec-mapping.md` |
| Day Surgery by clinical department | `surgery.md` + `subspec-mapping.md` |
| SOC First/New vs Repeat visits | `soc.md` |
| Inpatient discharges by Elective/Emergency | `inpatient-movement.md` |
| Any chart, visualization, or dashboard | Responsible table references + `dashboard.md` |

## Coverage and source-era limits

| Table | Coverage | Key era note |
|---|---|---|
| `emd` | Jan 2023–Jun 2026 | `NCPUCC` starts Jan 2025 — report unavailable, not zero, for 2023–2024 |
| `soc` | Jan 2023–Jun 2026 | Native new/repeat and private/subsidised groups start 2024; use hybrid CASE for 2023 |
| `surgery` | Jan 2023–Jun 2026 | SAP = `UID IS NULL` through Jan 2024; Epic = `UID IS NOT NULL` from Feb 2024 |
| `inpatient_movement` | Snapshot-based | Use `CASE_NO` before May 2025; use `EPIC_CSN` from May 2025 |

## Query discipline

- Double-quote NUH column names. For the inpatient snapshot field, use the exact
  source-specific physical column: RDS `"CURRENT_DATE"`; S3 `"current_date"`.
  Never use either identifier unquoted because it resolves to the SQL runtime
  date. Read `references/inpatient-movement.md` before generating inpatient SQL.
- Use half-open primary-date ranges: `>= DATE 'YYYY-MM-DD' AND < DATE 'next-period-start'`. Never use `<=` on a timestamp endpoint or `YEAR()`.
- Do not group or filter by `Period`, `Hosp_ABBR`, or `UID` as a date-range filter. In `surgery`, use `UID` only inside the documented hybrid category CASE.
- Generate annual totals and displayed subtotals programmatically from the same grouped result. A yearly total must equal the sum of its monthly values.
- Never manually reconstruct SQL result rows. Use fresh SQL output for reconciliation; re-query when discrepancies arise.
- State the table, date field, filters, classification, distinct key (when applicable), and QC status with every result.

## Fail-closed reporting gate

Treat historical operational reporting as a low-freedom workflow. Never replace
missing, truncated, failed, or inconvenient SQL output with sample rows,
interpolation, assumed growth, seasonal formulas, forecasts, hand-entered values,
or model-generated values. Stop and report the retrieval problem instead.

Before presenting a chart, dashboard, or mapped result:

1. Confirm the SQL call succeeded and pipe its complete result directly into analysis.
2. Record the SQL result row count, requested and observed date coverage, total
   workload, and unique source-OU count.
3. For mapped output, load the complete bundled mapping file programmatically;
   never paste or recreate a partial lookup in code.
4. Reconcile plotted monthly values, displayed subtotals, KPI totals, and locked
   benchmarks from the same processed dataset.
5. Stop with a failed QC status when months are missing, result coverage is
   incomplete, totals disagree, or a required assertion cannot be evaluated.

For a department dashboard, run the matching validator on the complete
month-by-`source_ou` SQL export before building HTML:

- SOC: `scripts/validate_soc_dashboard.py`
- Inpatient: `scripts/validate_inpatient_dashboard.py`
- Surgery: `scripts/validate_surgery_dashboard.py`

Preserve the validator's mapped CSV and audit JSON with the deliverable. Do not
build or present the dashboard when the validator returns a failed QC status.

When a direct SQL result is too large, use the supported SQL-export operation
to retrieve the complete result as a file, then run the matching validator on
that export. Never replace it with a preview, partial mapping, or manually
reconstructed rows.

## Outputs

For a chart or dashboard, return one self-contained HTML document in a single `html` fence unless the user explicitly authorizes publishing. Otherwise answer concisely with the query basis, result, and QC status.

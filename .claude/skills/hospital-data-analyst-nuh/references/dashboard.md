---
name: nuh-analytics-dashboard
description: Build and quality-check NUH operational dashboards from approved NUH table logic. Use when creating a self-contained HTML dashboard, mobile dashboard, chart report, or validating dashboard SQL, classifications, totals, and rendering.
---

# NUH dashboard build and QC

Read the relevant table references first. They control table logic and benchmarks;
this guide controls dashboard construction, rendering, and cross-table QC.

Read this guide for every chart, visualization, chart report, or dashboard. Apply
the data and SQL guardrails and Final QC to every visual output. Apply the HTML
build rules only to HTML output, and apply the mobile dashboard rules only when
the user requests a mobile dashboard.

## Data and SQL guardrails

- Use half-open primary-date ranges and double-quoted NUH columns.
- Do not use `UID`, `Hosp_ABBR`, or `Period` as a date-range filter. Keep surgery's
  `UID` only inside its approved hybrid category CASE.
- Use the current SOC and surgery hybrid classifications for any range that includes CY2023.
- Group ED arrival mode with `ARRIVAL_MODE_DESC`; its groups must total the base-filtered EMD count. Do not use superseded raw-code normalisation rules.
- For SOC, inpatient, or surgery department/cluster/MOH/subspecialty visuals, read `subspec-mapping.md`, use fresh SQL output, and show unmatched OU records separately before applying the clinical-reporting exclusion.
- Compute annual totals and every displayed subtotal from the exact monthly values shown. Never manually add them.
- Before reporting that an inpatient source lacks historical coverage, run the
  approved coverage query from `inpatient-movement.md` using the exact quoted
  physical snapshot column. A zero-row result produced with unquoted
  `current_date` is a query failure, not a data-availability finding.
- Treat a user-specified source as authoritative. Do not switch between S3 and
  RDS without the user's approval; troubleshoot and report a failed query first.

## Historical-data integrity

- Never create missing historical observations with interpolation, extrapolation,
  assumed growth, seasonal formulas, random values, or plausible-looking sample
  data unless the user explicitly requests a forecast or simulation. Label any
  authorized forecast separately from actuals.
- Treat tool previews and truncated results as incomplete. Stop dashboard
  creation until the complete requested data has been retrieved.
- Build the chart series, summary tables, KPIs, peak/low calculations, and
  narrative findings from one canonical processed dataset. Do not paste locked
  benchmarks into KPI cards independently of the plotted series.
- Keep the requested source, date range, and grouping coverage consistent across
  every visual. Do not substitute a latest-month or Top-N extract for a requested
  full-range department view.
- Preserve an audit manifest containing source table, date field, date range,
  SQL result row count, observed month count, unique source OUs, mapping count,
  mapped/unmapped workload, exclusions, plotted total, and QC status.
- For inpatient output, the audit manifest must additionally record the data
  source, exact quoted snapshot identifier, monthly expression, minimum and
  maximum snapshot dates, expected months, and observed months. Do not proceed
  from SQL to dashboard construction until these values confirm the requested
  coverage.
- Treat the validator process exit code and audit JSON as authoritative. Never
  hardcode `QC PASSED`, and never describe an output as ready, verified, or
  complete when a required assertion fails.
- For an SOC, inpatient, or surgery department dashboard, run the corresponding
  bundled `validate_*_dashboard.py` script on the complete month-by-OU export.
  Build every visual and KPI from its mapped CSV and retain its audit JSON.
- If an inpatient admissions or discharges benchmark fails, rerun the canonical
  monthly control query from `inpatient-movement.md` before constructing the
  dashboard. If that control query passes, correct the dashboard SQL or export,
  rerun the validator, and use only the corrected result. Use the diagnostic
  dashboard state only if the canonical control query also fails.
- If a direct SQL result is too large, use the supported SQL-export operation
  and run the corresponding validator on the complete exported file.

## Fail-visible validation output

Use the validator outcome to select one of these output states:

- If the validator exits successfully, `qc_status` is `PASSED`, and `errors` is
  empty, generate the normal dashboard.
- If complete source data are available but validation fails, generate a
  diagnostic dashboard. Show a prominent page-level `QC FAILED` banner, plus a
  warning panel immediately above each affected chart and a reconciliation
  panel or table immediately below it. Keep the chart itself visually unchanged:
  place no failure badge, warning, reference line, special bar styling,
  watermark, or diagnostic tooltip inside the chart.
- The reconciliation panel must identify the affected period or grouping and
  show the source total, displayed component values, component sum, difference,
  and failed assertion. Preserve unaffected charts and tables normally.
- Describe a diagnostic dashboard as an investigation aid and not for official
  reporting. Do not call it ready, verified, completed, or QC-passed.
- If source data are incomplete or unavailable, generate only a failure report
  explaining what is missing. Do not create charts from partial, inferred, or
  fabricated data.

## HTML build rules

1. Re-initialise all data arrays in the active session before building. Do not rely on a prior session.
2. Build HTML in one active session with list assembly and `json.dumps()` data injection. Do not use Python f-strings to embed JavaScript or `str.replace()` template placeholders.
3. In JavaScript HTML builders, use single-quoted JavaScript strings around HTML containing double-quoted attributes.
4. Before delivery, verify the generated script contains data declarations, `Plotly.newPlot`, KPI construction, and table construction; verify it has no empty script block or escaped-quote pattern that breaks HTML attributes.
5. Do not upload or publish a dashboard unless the user explicitly authorizes that external action.

## Mobile dashboard rules

- Use a 430px centred single-column layout, 2×2 KPI grid, horizontal-scroll tab bar, and the documented EMD tabs: Overall, PACS, Arrival.
- Use the last 24 months for charts only; retain the full time series in tables.
- Use the mobile inpatient KPI id `kpi-ip` (desktop uses `kpi-ipdis`).
- Verify the fixed viewport, tab IDs, active default tab, and all four sections on a mobile-sized rendering before handoff.

## Final QC

For each included metric: verify table-specific classifications, no unexpected
unclassified categories, monthly-to-annual roll-up, and the applicable locked
benchmark. If a dashboard source total conflicts with the responsible table
reference, show the discrepancy and ask for a data-owner decision rather than
choosing a value. For mapped outputs, also validate source row count, total, and
unique OU count against the SQL result; never manually rebuild a data array for reconciliation.
For a requested monthly range, assert exact month coverage. Recalculate the
total from the plotted series and require it to equal the KPI and summary-table
totals. For clinical-department output, require plotted workload plus declared
non-clinical exclusions to equal source workload, and preserve unmatched OUs as
`Unmapped` until corrected.

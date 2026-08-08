---
name: nuh-analytics-dashboard
description: Build and quality-check NUH operational dashboards from approved NUH table logic. Use when creating a self-contained HTML dashboard, mobile dashboard, chart report, or validating dashboard SQL, classifications, totals, and rendering.
---

# NUH dashboard build and QC

Read the relevant table references first. They control table logic and benchmarks;
this guide controls dashboard construction, rendering, and cross-table QC.

## Data and SQL guardrails

- Use half-open primary-date ranges and double-quoted NUH columns.
- Do not use `UID`, `Hosp_ABBR`, or `Period` as a date-range filter. Keep surgery's
  `UID` only inside its approved hybrid category CASE.
- Use the hybrid SOC and surgery classifications for any range that includes CY2023.
- Normalise EMD `ARRIVAL_MODE` before grouping; its groups must total the base-filtered EMD count.
- Compute annual totals and every displayed subtotal from the exact monthly values shown. Never manually add them.

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
choosing a value.

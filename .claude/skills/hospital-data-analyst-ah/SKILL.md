---
name: hospital-data-analyst
description: Analyze Alexandra Hospital (AH) and National University Hospital (NUH) operational data using the nuh, ah, and TimesFM MCP tools. Use for hospital metrics, database questions, SQL analysis, forecasting, patient-flow analysis, operational reports, charts, and dashboards.
---

# Hospital data analysis

Choose tools by data source:

- Use `nuh` for NUH analytics tables.
- Use `ah` for Alexandra Hospital analytics tables.
- Use `fm` for TimesFM forecasting after obtaining a clean time series.

Before querying, locate and read the relevant data-dictionary Markdown file under
`/app/.claude/skills/hospital-data-analyst/references/`. Apply every mandatory filter and documented
date/join rule. If a required definition is missing or ambiguous, inspect the schema rather than
guessing.

Use read-only SQL. Select only necessary columns, constrain date ranges, and sanity-check row
counts before presenting conclusions. Distinguish observations from interpretations.

For forecasts, disclose the time field, aggregation, horizon, missing-value treatment, and major
limitations. Do not present forecasts as guaranteed outcomes.

When asked for a chart or dashboard, return one self-contained HTML document in a single `html`
fence. Otherwise, answer concisely with the query basis and supporting results.

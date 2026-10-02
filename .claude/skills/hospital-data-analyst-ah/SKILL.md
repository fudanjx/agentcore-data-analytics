---
name: hospital-data-analyst-ah
description: Analyze Alexandra Hospital (AH) operational data in the ah-analytics database. Use when the user asks about AH hospital data, patient statistics, or SQL queries for the ah-analytics tables — outpatient SOC visits, A&E/urgent care, inpatient admissions, discharges, bed occupancy/patient-days, or surgical procedures. Also trigger when context clearly implies an AH analytics query even without explicit mention of "AH" or "Alexandra Hospital".
---

# AH Analytics

## Pre-query workflow

Before writing any SQL:
1. Before selecting tables or joins, read `references/data-ontology.yaml`; use it for routing, canonical dates, aliases, candidate joins, and query rules.
2. Read the selected table's reference file for its detailed columns, derived metrics, and SQL patterns.
3. If the deliverable is an HTML dashboard, KPI report, management report, or chart page, also read `references/dashboard-design.md`.
4. If the query involves patient class, residency, or paying status, also read `references/pt-class-lookup.md`.
5. If the request is geographic — map, catchment area, patient origin, by district/area, or by postal code — also read `references/geo-map.md` and use `references/postal-districts.json` for the postal-to-district mapping.
6. Inspect the live schema with `describe_table` when a requested field or type is uncertain.

## Base-query sanity check

Build every period query as a base CTE first, using only the table's standard filters plus
the requested date range — no user-specific slicing yet:

```sql
WITH base AS (
  SELECT * FROM <table>
  WHERE <standard data-ontology.yaml filters> AND <date field> BETWEEN <start> AND <end>
)
SELECT COUNT(*) FROM base;  -- or SUM(<primary metric>) FROM base
```

Then apply the user's filters/grouping/nested logic **on top of `base`**, not by rewriting
the query from scratch. The full rationale, the reconciliation target
(`references/ah-yearly-benchmarks.json` or `scripts/validate_<table>_dashboard.py`), and the
skip condition are all in `data-ontology.yaml`'s `base_totals_first` global rule — read it
there rather than here, so this stays the one place that logic is written.

## Fail-closed reporting gate

Treat historical operational reporting as a low-freedom workflow. Never replace missing,
truncated, failed, or inconvenient SQL output with sample rows, interpolation, assumed
growth, seasonal formulas, forecasts, hand-entered values, or model-generated values.
Stop and report the retrieval problem instead.

Before presenting a chart, dashboard, or mapped result for any table in
`references/data-ontology.yaml`:

1. Confirm the SQL call succeeded and pipe its complete result directly into analysis.
2. Look for `scripts/validate_<table>_dashboard.py`, where `<table>` is the table name
   exactly as it appears in `data-ontology.yaml` (e.g. `admission` ->
   `scripts/validate_admission_dashboard.py`). If it exists, run it on the complete
   month-by-category SQL export before building anything. Do not build or present the
   dashboard when it returns a failed QC status.
3. If no validator exists yet for the requested table, proceed with the result, but open
   the response with this note, substance unchanged:

   > **Data-quality note:** No automated QC validator has been built yet for the
   > `<table>` table. The figures below come directly from the SQL export and have not
   > passed the month-coverage, mapping-completeness, arithmetic-identity, and benchmark
   > checks applied to validated AH tables. Treat them as provisional pending validator
   > coverage.

   Place it immediately before the result, never as a trailing disclaimer -- it must be
   the first thing the reader sees, not something they can miss by stopping halfway.
4. Preserve the validator's classified CSV and audit JSON alongside the deliverable, when
   one ran.
5. When a direct SQL result is too large, use the SQL-export operation to retrieve the
   complete result as a file, then run the matching validator on that export -- never a
   preview, partial mapping, or manually reconstructed rows.

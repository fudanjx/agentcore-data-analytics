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
5. Inspect the live schema with `describe_table` when a requested field or type is uncertain.

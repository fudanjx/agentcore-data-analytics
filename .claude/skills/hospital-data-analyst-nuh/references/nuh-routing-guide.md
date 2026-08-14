---
name: nuh-analytics-data-dictionary
description: Route questions to the correct NUH table in nuh-analytics and apply the approved 2023 to June 2026 metric logic. Use when analyzing NUH emergency department, inpatient movement, specialist outpatient clinic, surgery, dashboards, or when selecting date fields, filters, source-era rules, and validation benchmarks.
---

# NUH Analytics — routing guide

## Select the table

| Question | Table | Primary date | Grain |
|---|---|---|---|
| ED attendance, PACS, arrival mode, disposition, or ED admission | `emd` | `EMD_VISIT_DATE` | ED visit |
| Inpatient admissions, discharges, patient days, ALOS, or patient class | `inpatient_movement` | `CURRENT_DATE` snapshot | Metric-specific |
| SOC visits, new/repeat, private/subsidised, clinic, specialty, or subspecialty | `soc` | `SOC_VISIT_DATE` | Actualised SOC visit |
| Surgical workload, category, normal delivery, or emergency/elective | `surgery` | `SVISITDATE` | Procedure, not case |
| Department, cluster, MOH specialty, or subspecialty report | Relevant SOC, inpatient, or surgery table + `subspec-mapping.md` | Table-specific | Source row mapped by OU |

Read the matching table reference. For a SOC, inpatient, or surgery OU grouping, also
read `subspec-mapping.md`; for dashboard requests, also read `dashboard.md`.

## Universal controls

1. Quote mixed-case and reserved identifiers, including `"CURRENT_DATE"`.
2. Filter only on the documented primary date with a half-open range.
3. Do not group or filter by `Period`; it is inconsistent or incomplete across source eras.
4. Do not filter a date-range query by `UID` or `Hosp_ABBR`. In surgery, `UID` is an era discriminator inside the hybrid CASE only.
5. Generate annual totals and table subtotals programmatically. A yearly total must equal the same monthly rows displayed.
6. Use a table reference's own benchmark only when every documented filter, period, and classification exactly matches.
7. Never manually reconstruct SQL result rows. For mapped outputs, validate the SQL row count, total, and unique OU count, and report unmatched source OUs.

## Coverage and source-era limits

- `emd`: January 2023–June 2026; Children UCC (`NCPUCC`) starts January 2025.
- `soc`: January 2023–June 2026; native new/repeat and private/subsidised groups start in 2024.
- `surgery`: January 2023–June 2026; SAP is `UID IS NULL` through January 2024 and Epic is `UID IS NOT NULL` from February 2024.
- `inpatient_movement`: use `CASE_NO` before May 2025 and `EPIC_CSN` from May 2025 for the validated 2025 snapshot measures.

The source documents do not define safe cross-table join keys or cardinalities. Inspect the live schema and establish row grain before joining tables. The supplied subspecialty mapping is a lookup, not evidence that an OU has source records.

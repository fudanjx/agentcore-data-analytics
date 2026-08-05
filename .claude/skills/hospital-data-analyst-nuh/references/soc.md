---
name: nuh-analytics-soc
description: Column reference and SQL guidance for the NUH soc table. Use when analyzing NUH specialist outpatient clinic visits, new versus repeat visits, private versus subsidised activity, clusters, specialties, or NUWoC-relevant activity.
---

# NUH Analytics — soc table

**One row is an actualized specialist outpatient clinic visit. Primary date: `SOC_VISIT_DATE`.**

The supplied logic is QC-verified for January–December 2025.

## Base filter

```sql
FROM soc
WHERE "SOC_VISIT_DATE" IS NOT NULL
```

Do not add an `APPT_STATUS` filter: all rows in this table are already actualized
visits.

## Core fields and classifications

| Column | Use |
|---|---|
| `SOC_VISIT_DATE` | Visit date and monthly time series |
| `VISIT_TYPE_GRP` | Two-way visit classification: `New` or `Repeat` |
| `PTE_SUB_GRP` | Patient class: `Subsidised Patients` or `Private Patients` |
| `CLUSTER` | Cluster code, for example `(09) O&G` and `(06) UCMI` |
| `MOH_SPEC_DESC` | Specialty description |

Use `VISIT_TYPE_GRP`, not `NEW_REPEAT`. The latter has a third `Other` category
(PA/TM types) and does not produce a complete two-way split; the locked source
notes 4,848 affected records.

## Example: monthly visits by visit type

```sql
SELECT
  DATE_TRUNC('month', "SOC_VISIT_DATE") AS month,
  "VISIT_TYPE_GRP",
  COUNT(*) AS visits
FROM soc
WHERE "SOC_VISIT_DATE" IS NOT NULL
  AND "SOC_VISIT_DATE" >= DATE '2025-01-01'
  AND "SOC_VISIT_DATE" < DATE '2026-01-01'
GROUP BY 1, 2
ORDER BY 1, 2;
```

## NUWoC-relevant clusters

| Cluster | Description |
|---|---|
| `(09) O&G` | Obstetrics and Gynaecology |
| `(06) UCMI` | Paediatric / Child Medicine (UCMI) |

NUWoC total is the sum of these two clusters.

## 2025 locked benchmarks

| Metric | Count | Share |
|---|---:|---:|
| New visits | 220,081 | 22.5% |
| Repeat visits | 758,002 | 77.5% |
| Total SOC visits | 978,083 | 100% |
| Subsidised | 768,783 | 78.6% |
| Private | 209,300 | 21.4% |
| `(09) O&G` | 107,153 | 11.0% |
| `(06) UCMI` | 77,151 | 7.9% |
| NUWoC total | 184,304 | 18.8% |

## Mandatory 2025 QC

For a matching full-year query, calculate and confirm:

```text
New + Repeat = 978,083
Subsidised + Private = 978,083
Sum of every cluster = 978,083
Sum of monthly visits = 978,083
```

Inspect the relevant classification field when an assertion fails. Do not use
manual arithmetic as evidence of a passing check.

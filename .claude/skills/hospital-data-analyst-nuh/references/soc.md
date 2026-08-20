---
name: nuh-analytics-soc
description: Column reference and SQL guidance for NUH soc. Use when analyzing specialist outpatient visits or attendance, First or New versus Repeat visits or attendance, private versus subsidised activity, clinics, specialties, departments, clusters, or subspecialties.
---

# NUH Analytics — soc

**One row is one actualised SOC visit. Primary date: `SOC_VISIT_DATE`.** Count
rows; no deduplication or additional global filter is required for total SOC
visits. Use a half-open `SOC_VISIT_DATE` range. For department, cluster, MOH,
or subspecialty reporting, also read `subspec-mapping.md`.

## Locked new / repeat classification

The older SAP code mapping and the newer Epic native field are the approved
hybrid rule. Do not use the superseded `NEW_REPEAT` logic.

```sql
CASE
  WHEN "SOC_VISIT_DATE" < DATE '2024-01-01' AND "VISIT_TYPE" IN ('N','B') THEN 'New'
  WHEN "SOC_VISIT_DATE" < DATE '2024-01-01' THEN 'Repeat'
  ELSE "VISIT_TYPE_GRP"
END AS visit_type_group
```

For CY2023 and earlier, `N` (New Patient) and `B` (Pre-Admission) are New;
`R`, `T`, `S`, `A`, and every other SAP value are Repeat. From CY2024 use the
native `VISIT_TYPE_GRP` value directly. State null or unexpected native values
separately rather than recoding them.

For SOC reporting, `attendance` and `visit` are interchangeable. Treat First
Visit, First Attendance, New Visit, New Attendance, and New Patient as requests
for the hybrid `New` category. Treat Repeat Visit, Repeat Attendance, and Repeat
Patient as requests for the hybrid `Repeat` category. Do not use a similarly
named source column instead of the hybrid rule.

Match the displayed label to the user's wording:

- A visits request displays `First Visit` and `Repeat Visit`.
- An attendance request displays `First Attendance` and `Repeat Attendance`.

The display-label change does not alter the underlying hybrid classification.

## Locked private / subsidised classification

The current rule replaces the older `PTE_SUB_GRP` and extended SAP-class lists.

```sql
CASE
  WHEN "SOC_VISIT_DATE" < DATE '2024-01-01' AND "PATIENT_CLASS" IN ('P','E')
    THEN 'Private'
  WHEN "SOC_VISIT_DATE" < DATE '2024-01-01' AND "PATIENT_CLASS" IN ('A','B','C')
    THEN 'Subsidised'
  WHEN "SOC_VISIT_DATE" < DATE '2024-01-01' THEN 'Unclassified'
  ELSE "PAY_CAT"
END AS paying_group
```

For CY2024 onward, use the native `PAY_CAT` value directly. Do not substitute
`PTE_SUB_GRP`; report null or unexpected values separately.

## OU and grouping fields

`ATTENDING_OU` is the subspecialty OU join key for all periods.

| Reporting grouping | CY2023 | CY2024–2025 | CY2026 onward |
|---|---|---|---|
| Subspecialty name | Mapping lookup | `ATTENDING_OU_DESC` | `ATTENDING_OU_DESC` |
| Cluster | `CLUSTER` | `CLUSTER` | `CLUSTER` |
| MOH specialty | `MOH_SPEC_DESC` | `MOH_SPEC_DESC` | `MOH_SPEC_DESC` |
| Clinical department | Mapping lookup | Mapping lookup | `DEPT_MAPPING` |

Do not use `EPIC_CDEPT_MAPPING` for Clinical Department: it has an `Other`
catch-all and a non-comparable naming scheme. `MED_DIV_GRP` is a CY2026+
Medicine subdivision, not a replacement for Department Grouping.

## Locked total-visit benchmarks

| Period | Total SOC visits |
|---|---:|
| CY2023 | 917,919 |
| CY2024 | 945,716 |
| CY2025 | 978,083 |

The CY2023 total is the corrected August 2026 reference; do not use the earlier
917,990 figure or older new/repeat and paying/subsidised benchmark splits.

## QC

Compute totals in SQL and verify that monthly rows roll up to the same annual
total. For native groupings, report null or unexpected values separately before
asserting a subtotal equals total. Never manually reconstruct SQL results; use
fresh SQL output for reconciliation. For mapped reports, validate source row
count, total visits, and unique OU count after loading the SQL result.

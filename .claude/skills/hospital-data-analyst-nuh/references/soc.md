---
name: nuh-analytics-soc
description: Column reference and SQL guidance for NUH soc. Use when analyzing specialist outpatient visits, new versus repeat visits, private versus subsidised activity, clinics, or specialties across 2023 to June 2026.
---

# NUH Analytics — soc

**One row is one actualised SOC visit. Primary date: `SOC_VISIT_DATE`. Coverage:
January 2023–June 2026.** Do not add an `APPT_STATUS` filter.

## New / repeat hybrid logic

For CY2024 onward, use native `VISIT_TYPE_GRP`. For CY2023 and earlier, derive
from `VISIT_TYPE`:

```sql
CASE
  WHEN "SOC_VISIT_DATE" >= DATE '2024-01-01' THEN "VISIT_TYPE_GRP"
  WHEN "VISIT_TYPE" IN ('FV','PA','FD','FW','DF','TM','FS','GF') THEN 'New'
  WHEN "VISIT_TYPE" IN ('RV','RD','DR','RW') THEN 'Repeat'
  ELSE 'Unclassified'
END AS visit_type_grp_derived
```

Use this logic rather than `NEW_REPEAT`, which has an `Other` category.

## Private / subsidised hybrid logic

For CY2024 onward, use native `PTE_SUB_GRP`. For CY2023 and earlier, derive
from `PATIENT_CLASS`:

```sql
CASE
  WHEN "SOC_VISIT_DATE" >= DATE '2024-01-01' THEN "PTE_SUB_GRP"
  WHEN "PATIENT_CLASS" IN ('PTE','NR','PTRF','PTEP','A','B1','AP','ARF',
                            'NRB1','CRF','B1P','B1RF','B2RF') THEN 'Private Patients'
  WHEN "PATIENT_CLASS" IN ('SUB','SUBP','B2','C','B2P','CP') THEN 'Subsidised Patients'
  ELSE 'Unclassified'
END AS pte_sub_grp_derived
```

Flag unclassified records before reporting. The approved mappings have zero
unclassified CY2023 records.

## Other fields

Use `TREATMENT_OU_CLINIC` for clinic-level analysis and `MOH_SPEC_DESC` for
specialty. Do not apply the old 2025-only `CLUSTER` rule unless the user
specifically needs that legacy view.

## Locked benchmarks

| Period | Total | New | Repeat | Private | Subsidised |
|---|---:|---:|---:|---:|---:|
| CY2023 | 917,990 | 210,571 | 707,419 | 217,358 | 700,632 |
| CY2024 | 945,716 | 211,891 | 733,825 | 210,407 | 735,309 |
| CY2025 | 978,083 | 220,081 | 758,002 | 209,300 | 768,783 |
| H1 2026 | 497,978 | 111,209 | 386,769 | 104,160 | 393,818 |

For every period, calculate in SQL and confirm New plus Repeat equals total and
Private plus Subsidised equals total. Confirm the annual value is the sum of the
same monthly rows displayed.

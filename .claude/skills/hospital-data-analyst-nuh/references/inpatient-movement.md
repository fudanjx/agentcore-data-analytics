---
name: nuh-analytics-inpatient-movement
description: Column reference and SQL guidance for NUH inpatient_movement. Use when analyzing inpatient admissions, discharges, patient days, ALOS, TYPE_GRP, or paying and subsidised inpatient activity.
---

# NUH Analytics — inpatient_movement

**Monthly snapshot field: `CURRENT_DATE`. Always quote it.** For validated 2025
metrics, use the hybrid rules below. `MOVEMENT_CAT` is text: compare with `'1'`,
`'2'`, and `'20'`, never integers.

| Era | Snapshot range | Episode key | Discharge category | Admission category |
|---|---|---|---|---|
| SAP | before `DATE '2025-05-01'` | `CASE_NO` | `'2'` | `'1'` |
| Epic | from `DATE '2025-05-01'` | `EPIC_CSN` | `IN ('2','20')` | `IN ('1','20')` |

## Global and metric filters

Exclude Healthy Baby records from every inpatient count:

```sql
"TREATMENT_CAT" <> 'BBW'
```

For admissions, additionally require
`DATE_TRUNC('month', "ADATE") = DATE_TRUNC('month', "CURRENT_DATE")`.
For patient days, calculate `SUM("LSTAY")` and additionally exclude
`"TREATMENT_OU" NOT IN ('NW22','NWDSW','NWEDS','NWASW')`. Apply those OU
exclusions only to patient days.

Use snapshot month (`CURRENT_DATE`) for the validated discharge grouping, not
`DDATE`. Cast `"DDATE"::date` only for discharge-based ALOS arithmetic.

## Paying / subsidised discharges

Use this locked hybrid classification for a cross-era discharge breakdown:

```sql
CASE
  WHEN "CURRENT_DATE" < DATE '2025-05-01'
   AND "PATIENT_CLASS" IN ('A','AP','ARF','B1','B1P','B1RF','B2RF',
                           'CRF','NR','NRB1','PTE','PTEP','PTRF') THEN 'Paying'
  WHEN "CURRENT_DATE" < DATE '2025-05-01' THEN 'Subsidised'
  WHEN "ADM_PATIENT_CLASS_GROUP" = 'PTE' THEN 'Paying'
  WHEN "ADM_PATIENT_CLASS_GROUP" = 'SUB' THEN 'Subsidised'
  ELSE 'Unclassified'
END AS patient_type
```

Use `ADM_PATIENT_CLASS_GROUP`, not `DISCH_PATIENT_CLASS_GROUP`, for Epic. Include
unclassified records in the overall total and state them separately. One Epic
record was unclassified in August 2025; `ADM_PATIENT_CLASS_GROUP` is null for the
entire SAP era by design.

## ALOS

Use snapshot-based ALOS by default:

```text
monthly patient days / monthly discharge count
```

Use discharge-based ALOS only when explicitly requested:

```sql
SUM(CASE WHEN "DDATE"::date - "ADATE"::date = 0 THEN 1
         ELSE "DDATE"::date - "ADATE"::date END) / COUNT(*)
```

It includes episodes admitted in earlier months; inspect the row grain first.

## Additional fields

`TYPE_GRP`: `EM` Emergency, `EL` Elective, `TA` Transfer-In, `MA` Maternity;
null is unclassified older SAP data.

## CY2025 locked benchmarks

| Metric | Value |
|---|---:|
| Admissions | 74,461 |
| Discharges | 75,037 |
| Patient days | 389,331 |
| Snapshot ALOS | 5.19 |
| Paying discharges | 18,197 |
| Subsidised discharges | 56,839 |

Validate monthly roll-up, the April-to-May transition (investigate over 5%),
correct era-specific categories and keys, and Paying plus Subsidised plus stated
Unclassified equals total discharges.

---
name: nuh-analytics-surgery
description: Column reference and SQL guidance for NUH surgery. Use when analyzing procedure volume, day surgery, normal delivery, inpatient surgery, emergency versus elective activity, or surgical paying status across 2023 to June 2026.
---

# NUH Analytics — surgery

**Count procedures, not cases or patients. Primary date: `SVISITDATE`. Coverage:
January 2023–June 2026.** Use the unified table with a half-open `SVISITDATE`
range. Never filter or group by `Period`, `Hosp_ABBR`, or `UID`.

`UID` is only the hybrid era discriminator: SAP is `UID IS NULL` through January
2024; Epic is `UID IS NOT NULL` from February 2024. Never infer the era from
`S_CODE` nullness.

## Locked hybrid surgical-category CASE

```sql
CASE
  WHEN "PATIENT_TYPE" = 'D' THEN 'Day Surgery'
  WHEN "PATIENT_TYPE" = 'I' AND "UID" IS NULL
   AND "SUR_PROC_CODE" IN ('NSP836U','NSI836U','NSI038U','NSP038U')
    THEN 'Normal Delivery'
  WHEN "PATIENT_TYPE" = 'I' AND "UID" IS NOT NULL
   AND "S_CODE" IN ('SP836U','SI836U','SI038U','SP038U')
    THEN 'Normal Delivery'
  WHEN "PATIENT_TYPE" = 'I' AND "UID" IS NULL
   AND "SUR_PROC_CODE" NOT IN ('NSP836U','NSI836U','NSI038U','NSP038U')
    THEN 'Inpatient Surgery'
  WHEN "PATIENT_TYPE" = 'I' AND "UID" IS NOT NULL
   AND "S_CODE" NOT IN ('SP836U','SI836U','SI038U','SP038U')
    THEN 'Inpatient Surgery'
  ELSE 'Unclassified'
END AS surgical_category
```

`Surgery_Patient_Class` is not valid for surgical category. Any unclassified row
is a QC failure. The SAP-only codes `NSI038U`/`NSP038U` and Epic-only
`SI038U`/`SP038U` are retained for future-proofing although none were observed.

## Emergency / elective and private / subsidised

Use `"EMERG_IND" = 'X'` for Emergency and `"EMERG_IND" IS NULL` for Elective.
Do not use `EMERG_IND1` or `Surgery_Case_Type`.

For private/subsidised analysis, apply the verified hybrid rule below to both
RDS and S3. Resolve exact RDS capitalization from schema metadata; S3 uses
`patient_class` and `patient_class_grp`.

```sql
CASE
  WHEN "SVISITDATE" < DATE '2024-01-01'
   AND "PATIENT_CLASS" IN
       ('A','AP','ARF','B1','B1P','B1RF','B2RF','CRF','NR','NRB1',
        'PTE','PTEP','PTRF') THEN 'Private'
  WHEN "SVISITDATE" < DATE '2024-01-01'
   AND "PATIENT_CLASS" IN ('B2','B2P','C','CP','SUB','SUBP')
    THEN 'Subsidised'
  WHEN "SVISITDATE" < DATE '2024-01-01' THEN 'Unclassified'
  ELSE "Patient_Class_Grp"
END AS paying_group
```

Do not use the superseded `SP`/`RP`/`AC`/`AP`/`EP` private list. It matches
only `AP` in the verified CY2023 Surgery data and incorrectly classifies almost
all Private procedures as Subsidised. Never classify every value outside a
Private list as Subsidised.

The verified CY2023 S3 profile is:

| Classification | `PATIENT_CLASS` | Verified procedures |
|---|---|---:|
| Private | `A` | 4,426 |
| Private | `AP` | 734 |
| Private | `ARF` | 842 |
| Private | `B1` | 1,414 |
| Private | `B1P` | 170 |
| Private | `B1RF` | 111 |
| Private | `B2RF` | 95 |
| Private | `CRF` | 1,491 |
| Private | `NR` | 3,885 |
| Private | `NRB1` | 751 |
| Private | `PTE` | 10,186 |
| Private | `PTEP` | 1,642 |
| Private | `PTRF` | 2,491 |
| **Private total** |  | **28,238** |
| Subsidised | `B2` | 11,909 |
| Subsidised | `B2P` | 777 |
| Subsidised | `C` | 15,704 |
| Subsidised | `CP` | 1,083 |
| Subsidised | `SUB` | 49,934 |
| Subsidised | `SUBP` | 3,309 |
| **Subsidised total** |  | **82,716** |

Private 28,238 plus Subsidised 82,716 totals the locked 110,954 CY2023
procedures, with Unclassified 0. For CY2023, `Patient_Class_Grp` exists but is
null for every row and must not be used. From CY2024, use the native
`Patient_Class_Grp` value directly; report null or unexpected values separately
rather than forcing them into Subsidised.

Before private/subsidised reporting, profile every distinct source value.
Require `Private + Subsidised + Unclassified = procedure total` monthly and
annually. A two-category dashboard is permitted only when Unclassified is zero.
Treat an unexpected code or a difference from the verified CY2023 profile as a
QC investigation flag; total reconciliation alone is not sufficient.

## OU grouping and reconciliation

For department, cluster, MOH-specialty, or subspecialty reporting, read
`subspec-mapping.md`. Use only `Attending_Dept_OU` as the source mapping field
for every SAP and Epic period; for S3 Tables use `attending_dept_ou`. Resolve
exact RDS capitalization from schema metadata without selecting an alternative
OU field. Alias it as `source_ou` and join it to the mapping's
`organizational_unit`. Do not manually reconstruct mapped result rows; use fresh
SQL output to reconcile them.

## Surgery OU field meanings

- `Attending_Dept_OU`: attending subspecialty/department OU. This is the only
  field approved for filtering or grouping Surgery workload by clinical
  department, cluster, subspecialty, or MOH specialty.
- `Performing_OU`: where the surgery was carried out. Use it only for a request
  about performing or operating location. Never use it for organizational
  mapping.
- RDS `TREATMENT_OU` and S3 Tables `treatment_ou_1`: where the patient stayed.
  Use this field only for a request about the patient's treatment or stay
  location. Never use it for organizational mapping.
- `Treatment_OU` and S3 Tables `treatment_ou_2` are separate physical fields.
  Do not assume their meaning or use them as substitutes without an explicitly
  documented rule.

## Locked annual benchmarks

| Period | Day surgery | Normal delivery | Inpatient surgery | Total | Emergency |
|---|---:|---:|---:|---:|---:|
| CY2023 | 69,886 | 2,831 | 38,237 | 110,954 | 16,416 |
| CY2024 | 73,061 | 2,730 | 39,112 | 114,903 | 14,137 |
| CY2025 | 83,467 | 2,537 | 39,945 | 125,949 | 13,804 |
| H1 2026 | 42,210 | 1,146 | 20,716 | 64,072 | 7,560 |

QC: category total equals procedure total; Emergency plus Elective equals total;
Private plus Subsidised plus paying-status Unclassified equals total; monthly
totals roll to annual total; and no surgical-category `Unclassified` exists. For
CY2025, Elective is 112,145. Investigate a Feb–Sep 2024 total below 7,000 as a
likely source-filter error. After any mapped load, check the SQL row count,
total, and unique OU count before reporting.

## Fail-closed dashboard workflow

For a monthly department dashboard, export one complete row per month and
`source_ou` with these columns:

```text
month_date,source_ou,procedure_total,day_surgery,normal_delivery,
inpatient_surgery,unclassified,emergency,elective,unexpected_emerg_ind
```

Generate every measure in SQL from the same base rows using the locked hybrid
CASE and `Attending_Dept_OU`. `unexpected_emerg_ind` must count values other
than null and `X`; do not silently treat them as Elective or omit them.

Run `scripts/validate_surgery_dashboard.py` with the complete export and the
bundled mapping JSON. It must confirm exact month coverage, all 277 mapping
records, non-negative integral counts, both category reconciliations, zero
unclassified/unexpected rows, mapping and exclusion reconciliation, and all
locked benchmarks fully covered by the requested range. Any failed assertion is
a QC failure; never replace missing observations with generated data.

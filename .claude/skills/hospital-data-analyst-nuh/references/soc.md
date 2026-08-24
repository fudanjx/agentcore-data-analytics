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

The verified pre-2024 code mapping and the newer Epic native field are the
approved hybrid rule. Do not use the superseded `NEW_REPEAT` logic.

```sql
CASE
  WHEN "SOC_VISIT_DATE" < DATE '2024-01-01'
   AND "VISIT_TYPE" IN ('DF','FD','FS','FV','FW','GF','PA','TM') THEN 'New'
  WHEN "SOC_VISIT_DATE" < DATE '2024-01-01'
   AND "VISIT_TYPE" IN ('RV','RW','DR','RD') THEN 'Repeat'
  WHEN "SOC_VISIT_DATE" < DATE '2024-01-01' THEN 'Unclassified'
  ELSE "VISIT_TYPE_GRP"
END AS visit_type_group
```

For CY2023 and earlier, use the actual `VISIT_TYPE` vocabulary present in both
the current RDS and S3 tables:

| Code | Source description | Classification |
|---|---|---|
| `DF` | Telehealth Video FV (Dr) | New |
| `FD` | Telehealth Phone FV (Dr) | New |
| `FS` | First Staff Clinic | New |
| `FV` | First Visit | New |
| `FW` | Walk-in (First) | New |
| `GF` | TeleVGrp DR FV | New |
| `PA` | Pre-adm Testing | New |
| `TM` | Telehealth Assessment | New |
| `DR` | Telehealth Video RV (Dr) | Repeat |
| `RD` | Telehealth Phone RV (Dr) | Repeat |
| `RV` | Repeat Visit | Repeat |
| `RW` | Walk-in (Repeat) | Repeat |
| null or any other value | Not in the approved mapping | Unclassified |

Do not use the superseded `N`/`B` rule: neither the verified CY2023 RDS nor S3
table contains `N` or `B`. Do not force null or unexpected codes into New or
Repeat. From CY2024 use the native `VISIT_TYPE_GRP` value directly. State null
or unexpected native values separately rather than recoding them.

The verified CY2023 source profile for both current RDS and S3 is New 210,571,
Repeat 707,419, and Unclassified 0, totalling 917,990. The New total comprises
DF 461, FD 471, FS 1, FV 204,738, FW 575, GF 1, PA 4,269, and TM 55. The Repeat
total comprises DR 3,677, RD 18,229, RV 684,390, and RW 1,123. Use this profile
to detect source or logic changes, but retain the locked total benchmark below
as a separate QC assertion until its 71-visit discrepancy is resolved.

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

Use the verified pre-2024 `PATIENT_CLASS` mapping for both RDS and S3, and use
the newer Epic native field from CY2024. This rule replaces the incorrect
simplified `P`/`E` versus `A`/`B`/`C` rule and must not be replaced by
`PTE_SUB_GRP`.

```sql
CASE
  WHEN "SOC_VISIT_DATE" < DATE '2024-01-01'
   AND "PATIENT_CLASS" IN
       ('A','AP','ARF','B1','B1P','B1RF','B2RF','CRF','NR','NRB1',
        'PTE','PTEP','PTRF') THEN 'Private'
  WHEN "SOC_VISIT_DATE" < DATE '2024-01-01'
   AND "PATIENT_CLASS" IN ('B2','B2P','C','CP','SUB','SUBP')
    THEN 'Subsidised'
  WHEN "SOC_VISIT_DATE" < DATE '2024-01-01' THEN 'Unclassified'
  ELSE "PAY_CAT"
END AS paying_group
```

The verified CY2023 source profile is:

| Classification | `PATIENT_CLASS` | Verified visits |
|---|---|---:|
| Private | `A` | 360 |
| Private | `AP` | 46 |
| Private | `ARF` | 27 |
| Private | `B1` | 138 |
| Private | `B1P` | 15 |
| Private | `B1RF` | 2 |
| Private | `B2RF` | 7 |
| Private | `CRF` | 18 |
| Private | `NR` | 46,527 |
| Private | `NRB1` | 8 |
| Private | `PTE` | 119,378 |
| Private | `PTEP` | 23,290 |
| Private | `PTRF` | 27,542 |
| **Private total** |  | **217,358** |
| Subsidised | `B2` | 1,483 |
| Subsidised | `B2P` | 82 |
| Subsidised | `C` | 1,183 |
| Subsidised | `CP` | 65 |
| Subsidised | `SUB` | 656,397 |
| Subsidised | `SUBP` | 41,422 |
| **Subsidised total** |  | **700,632** |

Private 217,358 plus Subsidised 700,632 totals 917,990, with Unclassified 0.
Use this profile to detect source or mapping changes; retain the separate locked
CY2023 total benchmark and its 71-visit QC discrepancy.

For CY2024 onward, use the native `PAY_CAT` value directly. Do not substitute
`PTE_SUB_GRP`; report null or unexpected values separately. For pre-2024
reporting, profile every distinct `PATIENT_CLASS` and require
`Private + Subsidised + Unclassified = source total` monthly and annually. A
two-category dashboard is permitted only when Unclassified is zero; never drop
or reallocate null or unexpected codes.

## OU and grouping fields

`Attending_OU` is the only approved subspecialty/department OU join field for
all periods. Resolve its exact RDS capitalization from schema metadata; for S3
Tables use `attending_ou`. Do not substitute another OU-like field.

| Reporting grouping | CY2023 | CY2024–2025 | CY2026 onward |
|---|---|---|---|
| Subspecialty name | Mapping lookup | `ATTENDING_OU_DESC` | `ATTENDING_OU_DESC` |
| Cluster | `CLUSTER` | `CLUSTER` | `CLUSTER` |
| MOH specialty | `MOH_SPEC_DESC` | `MOH_SPEC_DESC` | `MOH_SPEC_DESC` |
| Clinical department | Mapping lookup | Mapping lookup | Mapping lookup |

For Clinical Department, join `Attending_OU` to `subspec-mapping.json` in every
period. From CY2026, use `DEPT_MAPPING` only for QC comparison, not as the
reporting source. Do not use `EPIC_CDEPT_MAPPING`: it has an `Other` catch-all
and a non-comparable naming scheme. `MED_DIV_GRP` is a CY2026+ Medicine
subdivision, not a replacement for Department Grouping.

## Locked total-visit benchmarks

| Period | Total SOC visits |
|---|---:|
| CY2023 | 917,919 |
| CY2024 | 945,716 |
| CY2025 | 978,083 |

The CY2023 benchmark is the corrected August 2026 reference. The current RDS
and S3 source tables return 917,990; treat the 71-visit difference as an open QC
discrepancy. Do not replace the benchmark with the source total or force the
source data to match the benchmark.

## QC

Compute totals in SQL and verify that monthly rows roll up to the same annual
total. For native groupings, report null or unexpected values separately before
asserting a subtotal equals total. Never manually reconstruct SQL results; use
fresh SQL output for reconciliation. For mapped reports, validate source row
count, total visits, and unique OU count after loading the SQL result.

For pre-2024 New/Repeat reporting, first profile every distinct `VISIT_TYPE`.
Require `New + Repeat + Unclassified = source total` monthly and annually.
Display Unclassified separately whenever it is non-zero. Treat an unexpected
code, a missing expected code group, or a difference from the verified CY2023
profile as a QC investigation flag. A two-category New/Repeat dashboard is
permitted only when Unclassified is zero; never silently drop or reallocate
Unclassified visits.

For a requested monthly range, assert that every requested calendar month is
present exactly once in the monthly-total reconciliation. A successful query
invocation is not proof that its complete result reached the analysis step.
Record the returned row count and observed first and last month before creating
any visual.

For SOC clinical-department output:

1. Query complete month-by-`attending_ou` aggregates for the requested range.
2. Export or page the result when it is too large for one tool response. Never
   continue from a preview, truncated response, or a manually selected sample.
3. Run `scripts/validate_soc_dashboard.py` with the complete export and bundled
   `subspec-mapping.json`.
4. Report mapped and unmapped OU/visit counts and any explicit non-clinical
   exclusions.
5. Require monthly department totals plus declared exclusions to equal monthly
   source totals. Require the same relationship for the full period.

Never infer unreturned months from earlier months, annual benchmarks, growth
rates, seasonality, or another dashboard. Benchmarks validate retrieved data;
they are not a source from which to manufacture monthly or departmental values.

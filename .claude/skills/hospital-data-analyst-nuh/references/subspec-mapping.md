---
name: nuh-subspecialty-mapping
description: Apply the August 2026 NUH organizational-unit mapping to SOC, inpatient, or surgery reports by subspecialty, clinical department, cluster, or MOH specialty.
---

# NUH subspecialty mapping

Use `subspec-mapping.json` as the August 13, 2026 reference lookup. It contains
277 unique OUs (198 Activated and 79 Deactivated); the primary key is
`organizational_unit`. It maps an OU to its display name, `department_grouping`,
`cluster_grouping`, `moh_specialty_code`, and `moh_specialty_description`.

## Apply the lookup safely

1. Query the source workload rows or OU-level aggregates first. Never create or manually type source result rows.
2. Load the JSON and merge with source data as a many-to-one lookup. For SOC use `ATTENDING_OU`; for inpatient and surgery inspect the live schema to find the OU field rather than guessing it.
3. Before applying exclusions, report unmatched source OUs and validate source row count, total workload, and unique OU count against the SQL output.
4. Exclude `cluster_grouping = 'XX Cluster'` (equivalently `department_grouping = 'xx Dept'`) only for clinical-workload reporting. This also excludes SOC Allied Health/non-consult OUs. Do not use `ou_status`, `valid_to`, or `remarks` as an analytical filter.
5. Aggregate by `department_grouping` for internal departments, `cluster_grouping` for internal clusters, or both MOH specialty fields for MOH reports. Preserve deactivated OUs when they occur in source data.

The lookup is a mapping reference, not proof that an OU has source records. Do not assume every mapping OU is present in a period.

## SOC native-field precedence

Use `CLUSTER` and `MOH_SPEC_DESC` directly for SOC in every period. For the
subspecialty description, use the JSON lookup in CY2023 and `ATTENDING_OU_DESC`
from CY2024. For Clinical Department, use the JSON lookup in CY2023–2025 and
`DEPT_MAPPING` from CY2026. Never use `EPIC_CDEPT_MAPPING` for Clinical
Department; it misclassifies significant OUs into `Other`.

## Time-sensitive MOH mapping

The JSON holds the current mapping. For MOH comparisons spanning April 2026,
apply the documented reclassification from that month: `NSTRKDO`, `NSTRKREC`,
`NSTRKPD`, `NSTRKPR`, `NSTRPPR`, `NSTRPREC`, and `NSTRGEN` changed from
Orthopaedic Surgery to Renal Medicine; `NSTRLPR`, `NSTRLREC`, and `NSTLGEN`
changed from General Surgery to Gastroenterology. State the chosen period rule.

For MOH reporting, `NSSUNS` (Neurosurgery), `NSSUCO` (Colorectal Surgery), and
`NSSUPL` (Plastic Surgery) remain under MOH Specialty Code 12, General Surgery,
while retaining their distinct internal subspecialty and Surgery-department labels.

## Required reconciliation controls

- Pipe SQL output directly into analysis; never retype or reconstruct rows.
- After loading, assert row count, workload total, and unique OU count against SQL.
- Pre-flight any mapping-derived OU list against the database before using it.
- Cross-check each department total with a fresh direct SQL count (within ±1).
- Re-query SQL during discrepancies; do not reconcile against a previously rebuilt table.

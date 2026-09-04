---
name: nuh-subspecialty-mapping
description: Apply the August 2026 NUH organizational-unit mapping to SOC, inpatient, or surgery reports by subspecialty, clinical department, or MOH specialty.
---

# NUH subspecialty mapping

Use `subspec-mapping.json` as the August 13, 2026 reference lookup. It contains
277 unique OUs (198 Activated and 79 Deactivated); the primary key is
`organizational_unit`. Each record contains only the three supported mapping
keys: `organizational_unit`, `moh_specialty_description`, and
`department_grouping`.

## Field aliases

Treat the following source-field names as aliases for the corresponding JSON
mapping keys:

| JSON mapping key | Source-field aliases |
|---|---|
| `organizational_unit` | `OU`, `Department_OU`, `Dept_OU` |
| `moh_specialty_description` | `MOH Grouping`, `MOH speciality`, `MOH SPEC` |
| `department_grouping` | `Clinical Department`, `Department`, `Discipline`, `Clinical Speciality` |

Resolve the physical source column using the source table's schema while
preserving the approved logical field contract below.

## Mandatory source OU field contract

Use only the approved source field below when joining workload data to
`subspec-mapping.json`:

| Source table | Approved logical OU field | S3 Tables field | Meaning |
|---|---|---|---|
| `soc` | `Attending_OU` | `attending_ou` | Attending subspecialty/department OU |
| `inpatient_movement` | `Dept_OU` | `dept_ou` | Inpatient subspecialty/department OU |
| `surgery` | `Attending_Dept_OU` | `attending_dept_ou` | Attending subspecialty/department OU |

These semantic fields do not change between SAP and Epic. For RDS, use table
schema metadata to resolve only the exact capitalization of the approved logical
field. Require exactly one case-insensitive name match. If it is absent or
ambiguous, stop and report the schema mismatch. Never choose another OU-like
field based on its name, sample values, completeness, or apparent relevance.

Alias the approved physical field as `source_ou`, then merge it many-to-one to
`organizational_unit`. For grouping, aggregate source workload by `source_ou`,
merge the mapping, then aggregate by the requested mapped dimension. For
filtering, derive the OU list from the mapping, validate it against the approved
source field, and filter that field only.

For Surgery, never use `Performing_OU`, `TREATMENT_OU`, `Treatment_OU`,
`performing_ou`, `treatment_ou_1`, or `treatment_ou_2` for clinical-department,
cluster, subspecialty, or MOH-specialty mapping.

For inpatient reporting, never use `DEPT_OU_DESC`, `dept_ou_desc`, or another
department-description field as a substitute for mapping `Dept_OU`/`dept_ou`
to `department_grouping`.

## Apply the lookup safely

1. Query the source workload rows or OU-level aggregates first. Never create or manually type source result rows.
2. Load the complete bundled JSON programmatically from its `records` array and
   assert its `source.record_count`, record count, and unique
   `organizational_unit` count are all 277. Never manually type, paste, or
   model-reconstruct mapping entries in Python, JavaScript, SQL, or HTML.
3. Merge the approved `source_ou` with `organizational_unit`
   as a many-to-one lookup. Do not inspect the schema to choose among OU fields.
4. Label unmatched source OUs `Unmapped`; never guess, abbreviate, or derive a
   department name from the OU code or description.
5. Before applying exclusions, report unmatched source OUs and validate source row count, total workload, and unique OU count against the SQL output.
6. The compact mapping does not contain cluster, status, validity-date, or
   remarks fields. Do not infer those values from an OU or substitute another
   mapping field; use an explicitly approved source-side field and rule when
   such filtering is required.
7. Aggregate by `department_grouping` for clinical departments or by
   `moh_specialty_description` for MOH-specialty reports. Preserve deactivated
   OUs when they occur in source data.

The lookup is a mapping reference, not proof that an OU has source records. Do not assume every mapping OU is present in a period.

## Complete export for large results

If a direct SQL result is too large, use the supported SQL-export operation to
retrieve the complete month-by-`source_ou` result as a file. Load that file and
the bundled JSON programmatically, apply the same many-to-one mapping, and run
the table-specific validator. If complete export is unavailable, retrieve the
result by pagination or smaller date batches; never use a preview, partial
lookup, native department-description field, or manually reconstructed rows.

## SOC native-field precedence

Use `CLUSTER` and `MOH_SPEC_DESC` directly for SOC in every period. For the
subspecialty description, use the JSON lookup in CY2023 and `ATTENDING_OU_DESC`
from CY2024. For Clinical Department in every period, use the approved
`Attending_OU` field plus `subspec-mapping.json`. Use native `DEPT_MAPPING` only
as a QC comparison from CY2026; it is not the reporting source. Never use
`EPIC_CDEPT_MAPPING` for Clinical Department; it misclassifies significant OUs
into `Other`.

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
- Reject preview-only or truncated SQL output. Retrieve the complete result by
  export, pagination, or smaller date batches before proceeding.
- After loading, assert row count, workload total, and unique OU count against SQL.
- Assert that the complete mapping has 277 unique OUs before joining.
- Pre-flight any mapping-derived OU list against the database before using it.
- Cross-check each department total with a fresh direct SQL count (within ±1).
- Require `mapped workload + unmapped workload = source workload` before
  exclusions and `plotted workload + declared exclusions = source workload`
  after exclusions.
- Re-query SQL during discrepancies; do not reconcile against a previously rebuilt table.

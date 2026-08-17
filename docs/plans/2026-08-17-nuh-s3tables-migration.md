# NUH Parquet to S3 Tables migration plan

## Goal

Create four independent Apache Iceberg tables in the existing S3 Tables bucket
`arn:aws:s3tables:ap-southeast-1:964340114883:bucket/nuh-analytics`, preserving
all source rows and physical data types:

| Source object | S3 Tables table |
|---|---|
| `em_encoded.parquet.gzip` | `nuh.emd` |
| `in_encoded.parquet.gzip` | `nuh.inpatient` |
| `sc_encoded.parquet.gzip` | `nuh.soc` |
| `su_encoded.parquet.gzip` | `nuh.surgery` |

S3 Tables identifiers are lowercase; the requested uppercase names are their
logical display names only.

S3 Tables definitions also require lowercase field names for Glue/Athena
discovery. The job therefore normalizes source field names to lowercase,
rejects any collision caused by that normalization, and records the complete
source-to-target field-name mapping in its verification report. Field order,
types, values, and nullability remain unchanged.

The SURGERY source has five capitalization-only duplicate pairs. Their approved
target names are: `treatment_ou_1`/`treatment_ou_2`,
`postal_code_1`/`postal_code_epic_2`, `bill_num_1`/`bill_num_2`,
`accident_type_1`/`ccident_type_2`, and
`hsp_disch_date_time_1`/`hsp_disch_date_time_2`.

## Safety constraints

- Treat `s3://nuh-analytics/` as read-only.
- Use a one-time, manually invoked job. Do not add S3 event notifications.
- Create each target table unpartitioned and commit one initial Iceberg snapshot
  per source file, avoiding partially visible batch loads.
- Abort before mutation if any target table already exists.
- Validate source and target row counts, field types/order, and recorded
  source-to-lowercase-target field mappings after each load.

## Implementation steps

1. Inspect the four source Parquet footers for row counts and Arrow schemas.
2. Build a dedicated containerised migration runner based on the existing
   PyIceberg/S3 Tables REST-catalog pattern.
3. Provision a least-privilege, task-only IAM role: source `GetObject` and
   the exact S3 Tables namespace/table metadata/data operations.
4. Run the task with enough temporary disk and memory for the largest source
   file; it creates namespace `nuh` and loads each source one at a time.
5. Query S3 Tables metadata and validate each table's initial snapshot,
   schema and row count against the recorded source footer values.
6. Leave the task definition and source data in place for auditability, but do
   not schedule the task or attach an upload trigger.

## Verification

- Four tables exist: `emd`, `inpatient`, `soc`, `surgery` in namespace `nuh`.
- Every table has exactly one initial snapshot after a successful run.
- Table schemas match source Parquet field types and order; their names match
  the recorded lowercase normalization mapping required by S3 Tables.
- Target row counts equal the source Parquet footer row counts.
- The source objects' ETags, sizes and last-modified times remain unchanged.

## Completion record — 2026-08-17

All four initial loads completed with one Iceberg snapshot per table:

| Target | Validated source rows | Validated target rows | Snapshots |
|---|---:|---:|---:|
| `nuh.emd` | 552,404 | 552,404 | 1 |
| `nuh.inpatient` | 1,232,974 | 1,232,974 | 1 |
| `nuh.soc` | 3,339,767 | 3,339,767 | 1 |
| `nuh.surgery` | 415,878 | 415,878 | 1 |

The source object ETags and last-modified timestamps remained unchanged during
the migration. The manually invoked Lambda remains unscheduled and has no S3
event notification.

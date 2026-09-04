# AH SOC monthly S3 Tables delta pilot

## Scope and isolation

This pilot is isolated from the existing AH tables and overwrite-based Lambda
loader. It uses the `ah-soc-delta-pilot` S3 Table bucket, `pilot.soc` table,
and `ah-soc-delta-pilot` Glue 5.0 job in `ap-southeast-1` (account
`964340114883`). It deliberately keeps all deployed resources for inspection.

The Glue job uses two `G.1X` workers, a 30-minute timeout, zero retries, and
maximum concurrency one. Its dedicated role can read/write only the pilot
landing/QC prefix, operate on the pilot table bucket, and write Glue logs and
metrics.

## Inputs and immutable landing

No healthcare data is stored in Git. The runner validates each local file's
SHA-256 before upload and refuses to replace a checksum-qualified key if the
remote object does not have the matching metadata and size.

| Delivery | Immutable landing object | Rows | SHA-256 |
|---|---|---:|---|
| May 2026 | `s3://ah-data-analytics/temp_s3_update/soc/SOC_202605_d7ebafa00ad5.parquet.gzip` | 1,117,856 | `d7ebafa00ad5fe568f5e5a50ae596ae264eff9c4eefff50ac51a7f6fc1df2234` |
| June 2026 | `s3://ah-data-analytics/temp_s3_update/soc/SOC_202606_345558fbc3ab.parquet.gzip` | 1,138,633 | `345558fbc3aba8522f79ff7549048daa70a0b236b4803a5a239ba8e52753f0f6` |

Each object has `sha256` and `delivery_month` metadata. The shared landing
bucket remains SSE-S3 encrypted and without bucket versioning; no bucket-wide
setting is changed.

## Contract and write policy

`contract.py` requires exactly the reviewed 51 source columns in their
expected order. Names are lowercased and `/`, spaces, hyphens and parentheses
become underscores. The four date fields are timestamps, `cnt` is `bigint`,
and all remaining values are strings. `visit_date` is partitioned by month.

The non-null hybrid business key is:

```text
E|<trimmed PAT_ENC_CSN_ID>
L|<trimmed Case_No>|<trimmed Visit_No>|<yyyy-mm-dd Visit_Date>
```

The job rejects source schema drift, invalid timestamp casts, null keys,
duplicate keys, changed existing rows, and failed reconciliation before a
write. It never calls `overwrite`, `createOrReplace`, merge, update, or a
row-level mutation. Bootstrap creates an empty Iceberg table with SQL and then
performs one atomic append. Delta uses a key anti-join and does not append an
empty delta. A Spark catalog refresh follows a successful append before
post-commit reconciliation, because S3 Tables REST metadata is session-cached.

The selected pilot policy is append-only: unchanged overlaps and prior keys
missing from a newer full snapshot are reported but not modified. Existing-key
changes fail the run. Bootstrap refuses any existing non-empty table or table
with a snapshot; an exact-schema empty table without snapshots is the only
recovery state.

## Operation

Run from the repository root with the existing AWS credential chain:

```bash
python -m s3tables_delta_pilot.pilot deploy
python -m s3tables_delta_pilot.pilot upload
python -m s3tables_delta_pilot.pilot bootstrap
python -m s3tables_delta_pilot.pilot delta
python -m s3tables_delta_pilot.pilot verify
```

Every Glue run writes aggregate-only QC JSON to
`s3://ah-data-analytics/temp_s3_update/qc/<run-id>/report.json`; reports do
not contain patient identifiers or raw source values.

## Pilot acceptance evidence (2026-08-28)

- Bootstrap QC `5b0d109f-1e06-4f5a-a21c-f9cde76611e3`: 1,117,856 source,
  target, and distinct keys; zero null or duplicate keys; one snapshot.
- Initial June comparison QC `1e66bfff-9dee-49ab-b07f-9a0d9f5a1233` found
  1,117,856 unchanged overlaps, 20,777 new keys, zero changed overlaps, and
  zero missing earlier keys. Its append committed snapshot 2; the job then
  detected a stale same-session catalog read and failed closed.
- Read-only verification QC `93e0bae5-b1a2-4d12-8260-bf75aa659345` confirmed
  1,138,633 final rows and keys, zero changed/missing overlap, and exactly
  20,777 June-2026 rows.
- Required repeat June delta QC `63e4be09-c719-4933-802c-081aa7b02248`
  confirmed zero new rows, 1,138,633 unchanged target rows/keys, and still two
  snapshots.
- S3 Tables managed compaction and snapshot management are both enabled.

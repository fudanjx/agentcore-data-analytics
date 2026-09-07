# S3 Tables uploader: performance work handover — 2026-09-07

**Status:** implemented, unit-tested, and confirmed by two successful live Glue runs against `ah.admint_test2` (see 4.1). Not benchmarked before/after, and only one shape of upload has run live (see 5).

**Scope of this document:** everything changed on 2026-09-07 in
`s3tables_delta_pilot/`, plus the Glue failure found during the first real test
run and the fix applied to it.

**Source documents**

| Document | Role |
|---|---|
| `doc/2026-09-07-upload-performance.md` | The seven-step implementation plan that this work follows. |
| `doc/upload-performance-review-2026-09-07.md` | The review that produced the plan: measured gaps, synthetic profile, UI fixes, and stated limits. |
| `doc/s3tables-uploader-v2-implementation-plan-2026-09-04.md` | Earlier design context. Not evidence of shipped behaviour. |
| `doc/s3tables-delta-pilot-backend-handover-2026-08-31.md` | The standing API/contract handover. Still current; nothing below changes an endpoint shape. |

## 1. Why the work was done

The review established, from source and from a controlled synthetic profile
(50,000 rows × 6 columns), that a single upload was read and copied four times
before ingestion even started, that `_first_upload_contract` accounted for about
92% of preflight time, that `profiled_iceberg_type` ran twice per column, and
that `parse_documented_date` was called 400,000 times for 50,000 rows. On the
Glue side, keyed appends still fingerprinted and grouped every incoming row and
spent several extra Spark actions counting frames that local preparation already
knew the size of.

That profile is a controlled fixture, not a production benchmark. It located
redundant work; it never predicted a speedup for a specific user file, and this
handover does not claim one either.

## 2. What changed

### 2.1 Local preflight: parse once, reuse everywhere

`_preflight` was split. It is now a thin wrapper that opens each upload once,
parses it once into an Arrow table, and computes one digest per file, then hands
the materialised tables to `_preflight_tables`, which does all validation
against them. Every downstream helper — `_nric_sanitization_review`,
`_first_upload_contract`, `_unsafe_cast_issues`, `_create_type_selection_samples`,
`_create_deduplication_candidates` — takes an optional pre-parsed table and only
falls back to reading the file when called without one, so their old call sites
keep working.

Session files are no longer copied at all. `_session_upload_files` tags each
`UploadFile` with `_pilot_session_path` and `_pilot_sha256`, and
`_temporary_upload_path` yields the private session path directly instead of
creating a temporary copy. The already-computed session SHA-256 is reused rather
than re-hashed. `_digest_path` is the one remaining hashing helper, used only for
non-session uploads.

`profile_table` in `ingest_contract.py` now returns the inferred contract, the
warnings, **and** the set of columns needing manual confirmation, in one pass.
`schema_from_table` is kept as a two-value wrapper for existing callers. This
removes the second full-column profiling pass that
`manual_confirmation_columns` used to perform.

### 2.2 Vectorized temporal validation

`temporal_array` replaces the row-by-row Python date parsing. It uses Polars
expressions: a regex guard per documented format, then a native `strptime`
inside `pl.when(...).then(...)`, coalesced across the date patterns. The guards
are the same compiled patterns the scalar parsers use, so permissive parsing
(single-digit fields, fractional seconds, alternate separators) is still
rejected, calendar validity is still enforced, and year 9999 is still supported.
`strict_temporal_type` now shape-checks natively before parsing at all, so an
ordinary category or measure column is classified without a parse pass.

`profiled_iceberg_type` keeps pandas' numeric inference (already vectorized) but
operates on a natively filtered `_populated_native` series. Its type decisions
are unchanged: name-token string protection first, any decimal or scientific
representation means `DOUBLE`, and only `_TIMESTAMP_NAME_TOKENS` columns with
non-numeric values become a manual choice.

`detect_nric_columns` in `sanitization.py` no longer materialises every textual
value into a Python list. It trims and filters with `pyarrow.compute`, samples
indices, and pulls only those five scalars into Python. The sample is still
seeded with `f"{seed}:{field.name}"`, so detection is deterministic and matches
the previous policy.

### 2.3 Reviewed metadata reused at ingestion, with freshness checks

Starting ingestion from a reviewed session no longer repeats preflight.
`_start_ingestion` accepts the reviewed preview and validates that it is still
applicable before trusting it:

- mode, table bucket ARN, namespace and table must match the payload, otherwise
  409 "The reviewed upload destination changed".
- for appends, the destination contract is re-read and compared against
  `_contract_fingerprint`, otherwise 409 "The table contract changed after
  review". The fingerprint covers `contract_version`, `schema`, the
  de-duplication columns/mode/policy, and the manual and automatic sanitisation
  column lists — the fields that can invalidate a review.

First-upload type overrides are now validated against the
`lossy_target_types` impact counts already computed during review, instead of
re-scanning every file. The documented lossy behaviour is unchanged: an explicit
first-upload DATE/TIMESTAMP choice nulls incompatible populated values; every
other unsafe cast is still a hard 422.

### 2.4 Local keyed de-duplication, and a versioned manifest

When a keyed upload starts from a reviewed session whose files are all on
session storage, `_raw_key_row_selection` performs the de-duplication locally
with Polars lazy frames, using exactly the raw key-impact semantics already
shown to the user: keys built from raw text with empty/NULL normalised to `~`,
groups counted by distinct full-row variant, single-variant groups retained and
conflicting groups excluded. It returns per-file retained row indices plus
metrics, and `_make_glue_compatible_parquet` accepts `row_indices` so only
retained rows are staged.

The manifest is now versioned and carries what Glue would otherwise recompute:

```json
{
  "manifest_version": 2,
  "prepared_contract_types": true,
  "prepared_row_count": 0,
  "incoming_row_count": 0,
  "local_key_deduplication": false,
  "local_deduplication_metrics": {},
  "files": [], "schema": [],
  "deduplication_columns": [], "deduplication_mode": "", "deduplication_policy": ""
}
```

`prepared_row_count` is summed from the staged Parquet footers, not estimated.
The webapp refuses to write the manifest (`RuntimeError`) if that footer total
disagrees with the local de-duplication result, so a mismatch fails before Glue
is invoked.

### 2.5 Glue: trust prepared manifests, keep the legacy path

`_project_incoming` takes a fast path for `prepared_contract_types` manifests:
it validates the staged Parquet schema against the manifest and projects
straight to the contract, skipping source-name lookup and per-column casting.
Manifests without the flag still take the original path unchanged, so tables
created before today keep working.

`_run_ingestion` now uses manifest counts where they are authoritative:
`incoming_row_count` instead of a `count()` action, `prepared_row_count` for
`unique_incoming_rows` and for `rows_appended` on create and unkeyed appends,
and `rows_after_target_key_filter` (returned by `_keyed_rows_to_append`) for
keyed appends. Persistence of the incoming frame is skipped when the frame is
read once and appended. When `local_key_deduplication` is set, Spark performs no
incoming de-duplication at all; the local metrics are reported instead.

None of this is taken on trust. Before using any manifest number, Glue checks
that `prepared_row_count >= 0` and `incoming_row_count >= prepared_row_count`;
that local de-duplication is only accepted for prepared, keyed manifests
carrying all four required metrics; and that
`metrics["incoming_rows"] == incoming_row_count` and
`metrics["rows_retained_after_local_deduplication"] == prepared_row_count`. Any
failure raises before the table is touched. Keyed appends still perform the
narrow target-key anti-join in Spark — target comparison was never moved local.

### 2.6 UI fixes

Carried over from the review, unchanged in intent: awaited polling instead of
fire-and-forget; a key-analysis busy state that persists through pending
requests, `QUEUED` and `KEY_ANALYSING`; preflight controls rendered once so
focus, search text, type/encryption selections and key choices survive a poll;
a case-insensitive column filter with visible-result count where hidden selected
columns stay selected; and key-analysis status shown next to its own button,
with receipt text distinguished from analysis text.

Server-side, `UploadSessionStore.start_key_analysis` atomically claims the
analysis inside the node-local store before the endpoint returns 202, clearing
any previous `key_impact`. This prevents duplicate background workers and stops
the first poll from seeing a stale ready state. It retains the existing
single-process pilot assumption; it is not a distributed lock.

Per-phase timings are recorded on the session (`phase_timings_ms`) for receipt,
parse, digest, schema/sanitisation, NRIC detection, type inference, schema
comparison and cast validation, type samples, and de-duplication candidates.
Only durations and column names are recorded — no cell values and no filenames
beyond what the session already stores.

## 3. The Glue failure found in testing, and its fix

**Observed:** first real test run of the new path failed.

```
job_run_id  jr_45d7570b1d9dba3befc89559eb9ba8996b274f66a7e70fc2cd414763ce742b8e
state       FAILED
mode        create
target      `s3_rest_catalog`.`ah`.`admit_test2`
upload_id   UPLOAD-EB21DD720024
error       ValueError: Prepared Parquet schema does not match manifest:
            expected=[... ('death_date','timestamp') ...]
            actual=[... ('death_date','timestamp_ntz') ...]
```

The two lists were identical in column names and order. Only the temporal
columns differed, and only in flavour.

**Cause.** Local preparation writes a contract `TIMESTAMP` column as Arrow
`timestamp("us")` with no timezone. In Parquet that becomes
`TIMESTAMP(MICROS, isAdjustedToUTC=false)` — confirmed by writing the type and
reading the resulting Parquet metadata back. Spark 3.4 and later, so Glue 5.0,
infers that as `TimestampNTZType`, printed as `timestamp_ntz`. The manifest
contract says `timestamp`. The new fast path compared the two with strict tuple
equality, so every timestamp column mismatched and the job aborted. `DATE`
columns were never affected: `date32()` stays Parquet `Date` and Spark `date`.

There was a second, latent problem behind the first. `_create` issues
`CREATE TABLE ... (col timestamp)`, which Iceberg maps to `timestamptz` and
Spark reads back as `TimestampType`. Merely loosening the comparison would have
handed a `timestamp_ntz` frame to `writeTo(TARGET).append()` and relied on an
implicit write-time conversion.

**Fix** — `generic_glue_job.py:182-227`:

- `_comparable_type` normalises `timestamp`, `timestamp_ntz` and
  `timestamp_ltz` to a single token, and the schema check compares normalised
  types. Names and column order are still compared exactly, and real mismatches
  (`date` vs `timestamp`, `bigint` vs `double`) still raise.
- the projection casts each column to the contract type when the staged flavour
  differs, so the frame handed to Iceberg matches the DDL type. The cast uses
  `spark.sql.session.timeZone`, whose Glue JVM default is UTC — the same
  semantics the legacy path's `cast("timestamp")` on strings already had, so
  wall-clock values are preserved.

**Deliberately not done.** `spark.sql.session.timeZone` was not pinned,
`spark.sql.parquet.inferTimestampNTZ.enabled` was not disabled, and the pyarrow
writer was not switched to `tz="UTC"`. Each would change instants for the legacy
path and for tables already created.

**Recovery for the failed run.** The `ValueError` is raised in
`_project_incoming`, before `_create`, so `ah.admit_test2` was never created and
no cleanup was required.

## 4. Validation performed

### 4.1 Live Glue runs

The fix was confirmed end to end the same evening against `ah.admint_test2` in
table bucket `ah-analytics`, using the real NGEMR admission basedeck:

| Run | Mode | File | Rows | Result |
|---|---|---|---|---|
| `UPLOAD-00FBA951C7C8` | create | `NGEMR_Admission_Basedeck_from_Jan_2023.xlsx`, 34 MB | 76,060 appended, target 0 → 76,060 | `SUCCEEDED`, `committed`, snapshot 3389103366773695168 |
| `UPLOAD-CA0D492280EA` | append | `NGEMR_Admission_Basedeck_July.xlsx`, 968 KB | 2,208 appended, target 76,060 → 78,268 | `SUCCEEDED`, `committed`, snapshot 4185490023994104410 |

Both used `encounter_no_csn` as the composite key, reported
`unsafe_cast_values: 0`, and recorded no within-upload conflicts and no existing
key overlap.

Three independent checks confirm these runs exercised the new code and the fix:

- the script at `s3://ah-data-analytics/temp_s3_update/_pilot_assets/generic_glue_job.py`,
  uploaded by `_ensure_web_job()` at 13:15:20 UTC, is byte-identical to the
  fixed source and contains `_comparable_type` and `column.cast(contract_type)`;
- both QC reports carry `rows_retained_after_local_deduplication` and the
  append report carries `rows_after_target_key_filter` — fields only the
  manifest v2 path emits, so local keyed de-duplication and the manifest counts
  were used rather than Spark recomputation;
- the create run ingested the same `death_date` and `admission_datetime`
  columns whose `timestamp_ntz` inference had failed on `admit_test2`. A
  pre-fix script would have raised the same `ValueError`; this one committed.

Per-phase timings from those two sessions, for reference rather than as a
benchmark (no before/after comparison was run on the same file):

| Phase | create, 34 MB | append, 968 KB |
|---|---|---|
| `parse` | 15,408.2 ms | 587.8 ms |
| `type_inference` | 414.5 ms | n/a (append) |
| `deduplication_candidates` | 151.0 ms | 0.0 ms |
| `nric_detection` | 64.1 ms | 14.9 ms |
| `local_copy_and_sha256` | 36.4 ms | 3.1 ms |
| `digest` | 0.0 ms (session digest reused) | 0.0 ms |
| `profile_total` | 16,042.7 ms | 647.4 ms |

Parsing the workbook now dominates preflight — 96% of the create profile —
which is where the next optimisation belongs. `digest` is 0.0 ms in both,
confirming the session SHA-256 reuse works.

### 4.2 Local checks

| Check | Result |
|---|---|
| `AWS_EC2_METADATA_DISABLED=true .venv/bin/python -m unittest discover -s s3tables_delta_pilot/tests -q` | 99 tests pass (92 before this work) |
| `node --test s3tables_delta_pilot/tests/test_ui_session_flow.cjs` | 4 tests pass |
| `node --check s3tables_delta_pilot/static/app.js` | pass |
| `.venv/bin/python -m py_compile s3tables_delta_pilot/generic_glue_job.py` | pass |
| `git diff --check` | pass |

Tests added today:

- `test_vectorized_temporal_parser_matches_strict_date_rules` — the Polars
  parser agrees with the scalar oracle on the documented date edge cases.
- `test_preflight_reads_a_session_file_only_once` — the repeated-read regression
  the review measured cannot come back silently.
- `test_local_key_selection_handles_duplicates_and_conflicts_across_files` —
  cross-file exact duplicates and conflicting key groups.
- `test_staging_writes_exact_contract_types_and_order`.
- `test_session_ingestion_reuses_review_and_writes_prepared_manifest` and
  `test_keyed_session_stages_only_locally_retained_rows` — manifest v2 dispatch.
- `test_only_one_concurrent_key_analysis_claim_succeeds`.
- `test_prepared_parquet_timestamp_without_timezone_matches_the_contract` — the
  timestamp fix, including an assertion that the explicit cast is still present.

`pyspark` and `awsglue` are not installed locally, so `generic_glue_job.py`
cannot be imported by a test. The timestamp test therefore parses the script
with `ast`, extracts `_TIMESTAMP_FLAVOURS` and `_comparable_type`, and evaluates
those definitions in isolation. Other Glue assertions remain text assertions on
the script, as before.

## 5. What is not verified

- **No performance benchmark of the new path.** The timings in 4.1 are a single
  post-change run; there is no warm before/after comparison on the same file and
  no Spark execution plan inspected for the keyed anti-join. The 224-second
  profiling figure from the review screenshot is derived from `phase_started_at`
  and remains unexplained in production terms.
- **Only one shape of data has run live.** Both runs in 4.1 were single-file,
  conflict-free, zero-overlap uploads of the same source system. Untested live:
  multi-file uploads, within-upload key conflicts, existing-key overlap on
  append, and a legacy (pre-manifest-v2) manifest taking the fallback path.
- **No rendered-browser verification** of the UI fixes; the node tests exercise
  the module logic, not a real DOM.
- **Concurrency guarantee is single-process only.** `start_key_analysis` is
  node-local. Running more than one webapp instance against the same sessions
  is still unsupported.

## 6. Deployment notes

- `_ensure_web_job()` re-uploads `generic_glue_job.py` from disk on every
  ingestion, so the Glue fix ships with the next upload. No separate Glue
  deployment step.
- The `webapp.py` / `ingest_contract.py` / `sanitization.py` /
  `upload_sessions.py` changes need a service restart to take effect.
- `polars` was already in `s3tables_delta_pilot/requirements.txt`; no dependency
  change was needed.
- Rollback is a revert of this commit plus a restart. Manifests written by the
  new code are `manifest_version: 2`; the reverted Glue script ignores the extra
  keys and takes the legacy path, so an in-flight manifest is not corrupted by a
  rollback. Tables created today carry an ordinary contract with no new fields.

## 7. Follow-ups, in order

1. Spot-check the committed timestamp values in `ah.admint_test2` against the
   source workbook. The runs in 4.1 prove the cast no longer fails; they do not
   prove the wall-clock values are what an analyst expects. Query a handful of
   `admission_datetime` rows and compare.
2. Attack workbook parsing. It is 96% of the create profile and now the only
   large item left. Everything else in `phase_timings_ms` is already sub-second.
3. Exercise the untested live shapes listed in section 5: a multi-file upload,
   an upload with within-upload key conflicts, an append with existing-key
   overlap, and one legacy manifest to confirm the fallback path still works.
4. Benchmark the same large real file before and after, warm, using
   `phase_timings_ms`. Record it here.
5. Inspect the Spark plan for the keyed target anti-join before changing its
   caching. The review flagged a possible recomputation cost that was never
   confirmed.
6. Decide whether target-key comparison should also move local. It was left in
   Spark on purpose; moving it would need target data locally and changes the
   trust model.
7. Re-examine the transport question only if measurement shows receipt or
   spooling actually matters. The review's recommendation stands: keep the
   single multipart POST for now.

## 8. Housekeeping observed while preparing this handover

Raw uploaded workbooks were found in the repository root as untracked
`<hex>/00.xlsx` plus `session.json` directories, one of them 34 MB. They are the
private session directories for the two runs in 4.1 — `651a563d0e…` is the
34 MB create, `038b5a847d…` is the append — stored as `<session_id>/<NN><suffix>`
by `UploadSessionStore.create`, which is why the original filename is only
recoverable from `session.json`.

They are in the repository because `PILOT_UPLOAD_SESSION_ROOT` was pointed at
the working directory; it defaults to the system temporary directory. Both
sessions are past their one-hour `expires_at`, but `cleanup_expired` only runs
while the service is up, so they persisted after it stopped.

They were left untracked and uncommitted. Run the pilot with
`PILOT_UPLOAD_SESSION_ROOT` set outside the working tree, and delete those
directories once they are no longer needed — they contain real uploaded patient
data.

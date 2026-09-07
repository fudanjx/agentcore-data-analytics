# Upload performance and key-analysis UI review — 2026-09-07

Scope: local implementation compared with `s3tables-uploader-v2-implementation-plan-2026-09-04.md`. That document is design context, not evidence that every item shipped. No deployment, service restart, real-data upload, or AWS mutation was performed.

## What “Review upload” does

1. `static/app.js` sends selected files and destination fields in one `multipart/form-data` POST to `/api/v2/upload-sessions`.
2. FastAPI supplies `UploadFile` streams. These use spooled temporary storage; large files can already be on disk before the endpoint starts. See https://fastapi.tiangolo.com/tutorial/request-files/.
3. `upload_sessions.py:77` copies each stream into a private session directory and calculates SHA-256.
4. `webapp.py:1904` runs background preflight and sets `PROFILING` after receipt and after acquiring local processing capacity.
5. The browser polls a JSON session endpoint. It does not keep transmitting source files during `PROFILING`.

The screenshot's 224 seconds is derived from the server's `phase_started_at`, not bytes transmitted. The old UI also left stale profiling text visible after completion, so the screenshot alone cannot establish exact completion duration. No production trace for the screenshot's request ID was obtained.

The plan retired three old multipart *routes*, but the replacement session creation route intentionally still accepts multipart form data. This is ordinary HTTP form encoding, not an S3 multipart-upload job or application-managed chunk/reassembly API.

## Confirmed processing gaps

| Area | Current source evidence | Consequence |
| --- | --- | --- |
| Repeated copying/parsing | `webapp.py:687`, `:784`, `:799`, `:883`, `:1214` | Each preflight helper independently copies and reads the upload. Excel/CSV still use pandas; Excel is not converted once into a reusable session artifact. |
| Duplicate type inference | `ingest_contract.py:227` and `:257`, invoked together at `webapp.py:799` | Schema inference and manual-confirmation discovery both invoke the same full-column profiling. |
| Row-by-row temporal checks | `ingest_contract.py:129`, `:134`, `:168`; `webapp.py:883` | Repeated pandas maps and Python date parsing; temporal checks also run before native numeric type classification. |
| Sampled NRIC detection | `sanitization.py:136` | Materializes all textual values into Python lists and filters all of them before sampling only five. |
| Preflight repeated at ingestion | `webapp.py:2240` | Reviewing a session does not eliminate the next preflight when ingestion begins. |
| Preparation still materializes data | `webapp.py:1351`; `sanitization.py:288` | Full Arrow tables and pandas frames remain; staged output is not a fully batched, contract-typed pipeline. |
| Glue still deduplicates incoming rows | `generic_glue_job.py:325` | Keyed mode still performs full-row fingerprints, grouping, conflict counts and deduplication. The standalone local deduplication helper is not integrated into this ingestion path. |
| Extra Glue actions | `generic_glue_job.py:311`, `:334`, `:353` | Incoming and retained rows are still counted with Spark actions, although persistence can reduce repeated reads. |
| Target-key comparison | `generic_glue_job.py:285` | Target projection is narrow, but the implementation uses a left join plus overlap filtering/counting; cached intermediates are released before the retained result is materialized, creating a possible recomputation cost that needs Spark-plan verification. |

The clean mode does bypass deduplication and target-key comparison. Snapshot row totals use metadata. These are real improvements, but the planned minimal Glue and single-pass local pipeline are not fully implemented.

## Synthetic profiling evidence

Read-only experiment with 50,000 rows and six columns: integer `record_id`, constant category, numeric measure, an ISO date string, other text and status. Generated in memory and written to a synthetic Parquet buffer. Called create-mode `_preflight` with `cProfile`; wrapped `_read_upload_table` and `_temporary_upload_path` to count invocations.

- Total with instrumentation: **1.848 seconds**.
- Complete table reads: **4**; temporary copies: **4**, excluding initial session receipt.
- `_first_upload_contract`: **1.700 seconds**, approximately 92% of total.
- `profiled_iceberg_type`: **12 calls for six columns**.
- `schema_from_table`: **0.854 seconds**; `manual_confirmation_columns`: **0.844 seconds**.
- Python `parse_documented_date`: **400,000 calls**.

This demonstrates redundant work on a controlled fixture. It is neither a production benchmark nor a prediction of speedup on the user's file. More ambiguous columns trigger additional sample/lossy-conversion passes. Create and append profiles will differ.

## Recommended performance work, in order

1. Add separate timings for receipt, copy/hash, parsing, schema inference, NRIC detection, cast validation, sampling and preparation. Keep raw values and filenames out of logs. Benchmark the same large file before/after, with warm runs.
2. Reuse session paths rather than creating temporary copies for every helper. Parse source files once into a reusable Arrow/Parquet artifact with bounded memory; reuse the session digest. Clean artifacts through existing session lifecycle handling.
3. Produce inferred type and manual-confirmation decision together. Use native Arrow/Polars aggregations for full-data validation and bounded conversion to Python for sample values. Preserve documented date formats, null semantics, year-9999 support, collision handling and the sampled NRIC policy through regression fixtures.
4. Reuse reviewed metadata during ingestion. Recheck current target contract/revision and user-selected conversions without blindly repeating every source scan.
5. Integrate raw local deduplication and contract-typed staging before simplifying Glue. Version the manifest so only verified prepared data bypasses incoming Spark deduplication. Preserve raw-key conflict semantics, audits and atomic writes. Use metadata/preparation counts where valid and inspect Spark execution before changing caching.

Transport alternatives:

- **Keep one multipart POST initially (recommended).** It already sends each file once from the browser. Fix duplicate server work and distinguish receipt progress from profiling progress.
- **Separate JSON session creation plus binary PUT per file.** Feasible if measurements show receipt/spooling overhead matters. Can stream bytes directly into the private session artifact while hashing, but requires complete-file validation, cleanup of interrupted uploads, ownership checks and an explicit finalize step. It does not eliminate file transmission or profiling.
- **Browser-to-S3 upload.** A separate architectural change: raw uploads would leave the server's private session storage, changing the existing raw-data handling policy. Not the first fix for this profiling delay.

## UI fixes implemented locally

- Polling now awaits completion rather than scheduling another poll and immediately returning to the click handler.
- Key-analysis busy state persists through pending requests, `QUEUED` and `KEY_ANALYSING`. The button is gray and disabled; repeat clicks are ignored even if selections change.
- Preflight controls are rendered once, preserving focus, search text, type/manual-encryption selections and key choices. Analysis acknowledgements are applied once per token.
- A single selected column is submitted; no-selection/missing-session conditions show an explanation.
- Case-insensitive column-name filter with visible-result count and no-match feedback. Hidden selected columns remain selected; “Select all columns” continues to mean all eligible columns, regardless of filtering.
- Key-analysis status appears next to its button. Receipt text distinguishes sending files from subsequent analysis.
- The server atomically claims analysis within the node-local store before returning 202 and scheduling background work; old impact results are cleared. This prevents duplicate workers and the first poll seeing a stale ready state. The concurrency guarantee retains the existing single-process pilot assumption.

## Validation and limits

- `AWS_EC2_METADATA_DISABLED=true .venv/bin/python -m unittest discover -s s3tables_delta_pilot/tests -q`: **92 tests passed**.
- `node --test s3tables_delta_pilot/tests/test_ui_session_flow.cjs`: **4 tests passed** (single-column/busy behavior, filtering, poll cancellation, selection/acknowledgement preservation).
- `node --check s3tables_delta_pilot/static/app.js` and `git diff --check`: passed.
- No rendered-browser verification or live Glue performance benchmark was performed. Performance pipeline changes above remain recommendations; the UI fixes do not claim to reduce the 224-second profiling workload.

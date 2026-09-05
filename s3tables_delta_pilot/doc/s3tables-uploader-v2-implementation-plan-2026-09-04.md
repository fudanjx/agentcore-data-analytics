# S3 Tables Uploader v2: Performance, Optional De-duplication, Sanitization Review, and Skill Upload

## Summary

Adopt a hybrid processing architecture:

- Upload each source file once into a private, 60-minute local session.
- Use Polars/PyArrow for profiling, optional within-upload de-duplication, sanitization, casting, and Parquet preparation.
- Keep Glue responsible for authoritative target-key comparison, audit records, and atomic Iceberg commits.
- Increase Glue capacity from two to four `G.1X` workers.
- Retire the existing multipart upload behavior and migrate the pilot UI completely to the new session APIs.
- Permit parallel uploads to different tables while serializing every mutation to the same table through a distributed lock.
- Add structured, correlation-friendly FastAPI logging for local troubleshooting and container log collection.

## Key Changes

### 1. Reusable local upload sessions

Add session-based endpoints:

- `POST /api/v2/upload-sessions`
  - Multipart fields: mode, bucket ARN, namespace, table, and files.
  - Copies each file once, calculates SHA-256, and starts background profiling.
  - Returns `session_id`, expiry time, file metadata, and current state.
- `GET /api/v2/upload-sessions/{session_id}`
  - Returns phase, progress message, safe error details, preflight result, key analysis, and ingestion status.
- `POST /api/v2/upload-sessions/{session_id}/key-impact`
  - Accepts proposed key columns and type selections.
  - Starts raw, local Polars analysis without sanitization, S3 writes, or Glue.
- `POST /api/v2/upload-sessions/{session_id}/ingestions`
  - Accepts user tag, type selections, sanitization selections, de-duplication mode, and acknowledgement token.
  - Prepares the staged Parquet, uploads it, and starts Glue.
- `DELETE /api/v2/upload-sessions/{session_id}`
  - Cancels the session and securely removes its temporary files.

Session states will expose progress such as `PROFILING`, `KEY_ANALYSING`, `DEDUPLICATING`, `SANITIZING`, `WRITING_PARQUET`, `UPLOADING_S3`, `STARTING_GLUE`, `GLUE_RUNNING`, `SUCCEEDED`, and `FAILED`.

Security and lifecycle:

- Bind every session to the authenticated user and selected bucket/namespace/table.
- Store files under a random private directory with directory mode `0700` and file mode `0600`.
- Delete files after successful submission, explicit cancellation, or 60 minutes.
- Run abandoned-session cleanup periodically and during service startup.
- Store the active session ID in browser `sessionStorage`, allowing a page refresh to reconnect.
- Never copy raw files to S3 or include raw values in logs.
- Continue retaining only sanitized Glue staging objects for 30 days.

Retire the multipart upload behavior of `/api/preflight`, `/api/key-impact-analysis`, and `/api/ingestions`. The pilot UI must stop calling them in the same release. Because there are no downstream consumers, do not maintain duplicate compatibility processing. Leave lightweight HTTP 410 tombstone handlers for one pilot release; they must not accept, copy, parse, or stage uploaded files and must return the replacement v2 route in a structured response. Remove the tombstones in the next planned API cleanup.

### 2. Single-pass local preparation

Replace repeated reads and pandas-wide processing with a reusable Polars/PyArrow pipeline:

- Parquet, CSV, and TSV use lazy or streaming scans.
- Excel is parsed once, converted to an internal Arrow/Parquet session artifact, then reused.
- Canonicalize column names once, including deterministic `_01`, `_02` collision suffixes.
- Calculate type profiles, safe samples, null counts, and sanitization detection through combined aggregations rather than separate full-column scans.
- Do not calculate expensive exact distinct counts for every column. Calculate key cardinality only when key analysis is requested.
- Perform documented date/time and numeric conversion locally.
- Produce Parquet whose names and physical types already match the table contract.
- Encrypt distinct identifier values once per column and apply the resulting mapping, instead of repeatedly encrypting identical values through pandas row callbacks.
- Write staged Parquet in batches to avoid materializing the entire dataset as a pandas DataFrame.

Add value-free timings for file receipt, parsing, profiling, key analysis, de-duplication, sanitization, Parquet writing, S3 upload, Glue startup, target comparison, and Iceberg commit.

### 3. FastAPI logging and diagnostics

Add structured application logging around every API request and background phase:

- Emit JSON logs to stdout by default so ECS/EKS can forward them to CloudWatch.
- Support an optional rotating local log file for the Mac pilot through `PILOT_LOG_FILE`; default rotation is 10 MB with five retained files.
- Configure `PILOT_LOG_LEVEL`, defaulting to `INFO`, without enabling verbose boto3 request-body logging.
- Add HTTP middleware that records request start/end, method, route template, status, duration, request ID, authenticated user ID, and response size.
- Generate or accept `X-Request-ID`, return it in the response, and propagate it into upload-session, S3-manifest, Glue-argument, QC, history, and lock records.
- Log phase start/end and elapsed time for receipt, profiling, key analysis, de-duplication, sanitization, Parquet writing, S3 staging, Glue queuing, Glue execution, Iceberg commit, and cleanup.
- Include safe identifiers such as session ID, operation ID, job-run ID, bucket/namespace/table, file count, total bytes, extension, and a shortened SHA-256. Do not log source values, full original filenames, ciphertext, encryption material, secrets, authorization headers, or request bodies.
- Install global exception handlers that record the full server-side traceback with an `error_id`, while returning only a safe phase-specific message, error code, request ID, and error ID to the browser.
- Reconcile Uvicorn access/error logging with the application formatter so a request produces one correlated access record rather than duplicate unstructured lines.

The UI must show the request ID, current phase, elapsed time, and returned error ID in failure details. Logs themselves remain server-side and are not exposed through a general browser log-download endpoint.

### 4. Explicit per-upload de-duplication mode

Add `deduplication_mode` with two values:

- `none`: “My data is clean; do not de-duplicate.”
- `keyed`: “De-duplicate using the table’s composite key.”

For `none`:

- Append every prepared row.
- Do not call Polars `unique`/`group_by`.
- Do not generate row fingerprints.
- Do not scan the existing S3 Table.
- Do not run Spark `dropDuplicates`.
- Warn clearly that repeated files and existing rows will be appended again.

For `keyed`:

- If the table has no key, require the user to select one or more columns, run key-impact analysis, and acknowledge it.
- The first acknowledged key becomes the table’s immutable key.
- If the table already has a key, display it as locked and reuse it without requiring another impact-analysis acknowledgement.
- Users may still select `none` for later uploads even after a key exists.
- If a table began with `none` and later defines a key, apply the key prospectively. Do not rewrite or remove older rows.

Within the incoming upload, Polars will:

- Treat the selected columns as a fixed tuple.
- Preserve missing components using explicit null sentinels.
- Retain one copy of an exact repeated row.
- Skip all rows in a same-key/different-row conflict group.
- Perform this comparison on canonical raw values before sanitization, matching the impact preview.

For keyed appends, Glue will:

- Read only the target key columns.
- Build a distinct set of existing keys, tolerating duplicates created by earlier no-dedup uploads.
- Left-anti join the prepared incoming data against those keys.
- Skip every incoming row whose key already exists.
- Report `existing_key_overlap_rows` without performing the expensive all-column exact-versus-conflict comparison.

### 5. Contract and audit evolution

Introduce uploader contract version 2 containing:

- Immutable table schema.
- Optional immutable composite key.
- Automatic sanitization actions.
- First-upload manual encryption columns.
- NRIC detection policy and version.
- Contract revision, creation actor, and timestamps.

Each ingestion manifest records its own `deduplication_mode`. An empty key means “not configured yet,” not implicit full-row de-duplication.

Migrate existing contracts lazily:

- Existing non-empty keys remain immutable.
- Existing empty or legacy full-row contracts become key-unconfigured.
- Existing tables default to no manual encryption columns.
- No existing S3 Table data is rewritten.

Use conditional contract updates when first assigning a late key, returning HTTP 409 if another request changed the contract concurrently.

Evolve uploader history with nullable fields for:

- De-duplication mode and key columns.
- Incoming duplicates removed.
- Conflicting rows skipped.
- Existing-key overlaps skipped.
- Automatically sanitized columns.
- Manually encrypted columns.
- Processing phase timings.

QC and audit output remain value-free.

### 6. Sanitization review and NRIC detection

Add a collapsed “Sanitization and anonymization” section to preflight.

Show:

- Automatically dropped columns and detection reason.
- Automatically encrypted identifier columns.
- Postal-code and age transformations.
- NRIC-detected columns.
- Columns available for additional manual encryption.
- Up to five non-empty samples for non-sensitive candidate columns.

Manual selections:

- Mean AES-CBC encryption using the existing Secrets Manager key and `enc:v1:` representation.
- Are available only when establishing the first table contract.
- Become immutable and are automatically applied to later uploads.
- Are invalidated if the user changes files before submission.

NRIC heuristic:

- Consider every otherwise-unsanitized textual column, regardless of its name.
- Select up to five non-null, non-empty values using deterministic pseudo-random sampling seeded by file digest and canonical column name.
- Match trimmed values case-insensitively against `^[STFGM][0-9]{7}[A-Z]$`.
- Treat three or more matches as an NRIC column.
- Encrypt every non-empty value in a detected column.
- Mask its sample values immediately in the UI.
- Record only sample count, match count, and detection decision.
- Describe this as a sampled NRIC heuristic because rare NRIC values in a mixed column can be missed.
- Do not implement the mathematical Singapore checksum algorithm in this change.

Automatic sensitive-column detection runs for every upload. Once a column is classified for automatic encryption, add it permanently to the contract’s sanitization union. A late automatic discovery encrypts new values and emits a warning that previously stored rows were not retroactively rewritten.

### 7. Minimal Glue execution

Refactor the Glue job so that:

- Staged Parquet is asserted to match the manifest schema instead of cast field-by-field.
- Any compatibility-path cast checks use one aggregate Spark action for all fields.
- Row counts come from Parquet metadata and Iceberg snapshot `total-records` summaries where available.
- No full-table `count()` is used solely to determine pre/post snapshot row totals.
- Mode `none` goes directly to atomic append.
- Mode `keyed` performs only the narrow target-key anti-join and append.
- Empty results do not create a data snapshot.
- Existing audit, rollback, and atomic Iceberg guarantees remain intact.

Configure the web Glue job with four `G.1X` workers, a 60-minute safety timeout, and zero automatic retries. Set `MaxConcurrentRuns` to five and enable Glue job-run queuing both on the job definition and each `StartJobRun` request. Up to five uploads to different tables may therefore run concurrently, consuming at most 20 `G.1X` DPUs; later jobs wait in Glue instead of failing with a concurrency-limit error. Make the limit configurable with `PILOT_GLUE_MAX_CONCURRENT_RUNS`, defaulting to five, and display Glue's `WAITING` state as “Queued for ETL capacity” in the UI. Verify through Glue logs that executor configuration is not silently limited to one executor.

Bound local CPU/memory pressure separately with `PILOT_LOCAL_PROCESSING_CONCURRENCY=2`. Additional session operations wait in a visible local `QUEUED` state rather than running too many Polars/Arrow transformations simultaneously.

### 8. Per-table mutation lock

Store distributed lease objects in the existing general-purpose S3 bucket under:

`s3://ah-data-analytics/temp_s3_update/web_ingest/table_locks/<target-id>.json`

The `target-id` is the SHA-256 of the canonical table-bucket ARN, namespace, and table name. Each SSE-S3-encrypted JSON object contains an unguessable owner token, user ID, request ID, session ID, operation, phase, acquired time, lease expiry, and optional Glue job-run ID. It must not contain source filenames, row values, identifiers, or secrets.

Lock behavior:

- Acquire the lock with `PutObject` and `If-None-Match: *` when the user submits create, append, rollback, or delete, before contract mutation, S3 staging, or Glue start. S3's conditional write makes exactly one competing request the owner.
- Allow preflight and key-impact analysis without a lock because they do not mutate the table.
- A repeated request with the same request ID is idempotent and returns its existing operation instead of creating another job.
- A different request for the same table receives HTTP 409 with code `TABLE_LOCKED`, the active operation/phase, safe lock-owner display, acquired time, expected lease expiry, and a `Retry-After` header.
- The UI must keep the later user's reviewed session intact, disable its upload action, and show that the selected table is busy. The user may retry after the active operation finishes without repeating file upload or preflight while the 60-minute session remains valid.
- Uploads to different tables use different lock keys and may proceed concurrently subject to the local-processing and Glue concurrency limits.
- Retain the current lock object's ETag. Renew the lease every five minutes with `PutObject` and `If-Match: <current-etag>` while local preparation or Glue is active; each successful renewal replaces the saved ETag. Use a 120-minute lease ceiling so it safely exceeds the 60-minute Glue timeout and local preparation allowance.
- Release the lock only after the mutation reaches a terminal state and snapshot reconciliation/history recording finishes. Use `DeleteObject` with `If-Match: <current-etag>` so an older worker cannot remove a newer owner's lock.
- Release immediately when staging or Glue startup fails before a job exists.
- When acquisition finds an existing object, read its body and ETag. If its lease has expired, first reconcile the recorded Glue job/session, then take over only with `PutObject` and `If-Match: <observed-etag>`; a failed precondition means another request changed the lock and must be treated as still locked.
- On FastAPI startup, list only the bounded `table_locks/` prefix, resume monitoring active Glue runs, release terminal runs, and conditionally remove expired pre-Glue locks whose local sessions no longer exist.
- Do not rely on the upload-archive lifecycle rule for lock correctness or cleanup. Explicit conditional release and startup reconciliation own the lock lifecycle.
- If S3 lock operations are unavailable, fail closed for table mutations and return a safe service-unavailable error; never start an unlocked write.

Grant the service identity scoped `s3:ListBucket` for the exact `temp_s3_update/web_ingest/table_locks/` prefix and `s3:GetObject`, `PutObject`, and `DeleteObject` for objects beneath it. No DynamoDB resource is introduced. Keep Iceberg's optimistic atomic commit as the final protection against writers outside this uploader.

### 9. Replace Dify skill generation with bundle upload

Rename the UI expander to “Upload a skill for this S3 Tables bucket.”

Remove:

- Dify instruction input.
- Dify HTTP call and generated-source download.
- `/api/skills/build`.
- Editable generated draft workflow.
- Dify environment variables and the pilot’s `httpx` dependency if unused elsewhere.

Add a bucket-scoped skill file explorer and a folder picker/drag-and-drop area:

```text
SKILL.md
references/
scripts/
assets/
```

Add these bucket-authorized APIs:

- `GET /api/skills/files` lists safe relative paths with size and last-modified metadata.
- `POST /api/skills/files` uploads a full folder or selected subset of files.
- `GET /api/skills/files/download` streams one safe relative path for download.
- `DELETE /api/skills/files` removes one explicitly confirmed safe relative path.

Validation:

- Require valid UTF-8 YAML frontmatter with a non-empty description whenever a root `SKILL.md` is uploaded.
- Force that file's `name` to the selected S3 Tables bucket name.
- Reject absolute paths, traversal, empty components, backslashes, NULs, and unsafe names.
- Default limits: 500 files, 50 MB per file, and 250 MB total.
- Recheck bucket authorization server-side.

Publish beneath:

`s3://<configured-bucket>/<configured-prefix>/<table-bucket-name>/`

Apply incremental file management:

1. Validate every file locally.
2. Upload resources first.
3. Upload `SKILL.md` last.
4. Overwrite only files with matching relative paths; retain omitted files.
5. Use the explicit delete endpoint to remove a file after confirmation.
6. Return created/overwritten paths, destination URI, and a reminder that the consuming runtime must restart or resynchronize.

The explorer renders an expandable hierarchy with file name, last-modified time,
size, download, and delete actions. Administrators and assigned editors may
use these actions only for their authorized table bucket's skill prefix.

Required IAM becomes scoped `s3:ListBucket`, `s3:GetObject`, `s3:PutObject`,
and `s3:DeleteObject` for the configured skill prefix.

## UI Behavior

- “Review upload” creates or reuses the local upload session and displays live profiling status.
- Preflight shows type review, sanitization review, and then the de-duplication choice.
- Option A immediately bypasses every key control.
- Option B displays either the stored immutable key or key candidates and analysis controls if no key exists yet.
- Any file, type, manual sanitization, or proposed-key change invalidates the relevant acknowledgement.
- “Upload and run ETL” polls the same session and shows local preparation phases before switching to Glue status.
- The pilot UI calls only the v2 session routes; it never posts files to the retired multipart routes.
- Different-table uploads show independent local/Glue queued or running states.
- A same-table HTTP 409 shows who/what currently holds the table lease, when it began, and when retry is expected; it does not discard the later user's session.
- Failure messages identify the exact phase and safe reason.
- Refreshing the page reconnects to the live session and Glue job.

## Test and Acceptance Plan

Automated tests:

- Session ownership, authorization, expiry, cleanup, cancellation, and refresh recovery.
- Raw files never written to S3 and staged objects contain only sanitized data.
- The pilot UI contains no calls to retired multipart endpoints; their one-release tombstones return HTTP 410 without reading request bodies or files.
- Structured logging correlation across API, session, lock, Glue, QC, and history; redaction tests ensure values, secrets, headers, full filenames, and ciphertext never appear.
- Option A appends duplicate input unchanged and executes no de-duplication or target-read code.
- Option B first-key definition, immutable reuse, late prospective activation, null components, exact duplicates, conflicts, and target overlaps.
- Existing duplicate target keys do not fail prospective keyed appends.
- NRIC sampling is deterministic; 3/5 triggers encryption, 2/5 does not, detected samples are masked, and every non-empty value is encrypted.
- Manual encryption is first-upload-only, immutable, and idempotent for existing ciphertext.
- Single aggregate cast validation replaces per-column Spark actions.
- Skill-file authorization, path traversal, limits, frontmatter enforcement, hierarchy metadata, incremental overwrite, download, and confirmed per-file deletion.
- Two uploads to different tables can run concurrently, the sixth Glue run waits rather than fails, and local processing above its limit reports `QUEUED`.
- Two mutations to the same table allow exactly one lock owner; the later request receives `TABLE_LOCKED`, can retry from the same session, and starts after release.
- Lock idempotency, conditional acquisition, ETag-protected renewal/release, stale takeover races, FastAPI restart reconciliation, and fail-closed S3 errors.
- Existing create, append, schema matching, rollback, history, table deletion, and identity tests continue to pass.

Performance acceptance:

- Confirm a source file is copied and parsed only once in the v2 UI workflow.
- Confirm option A’s Spark plan contains no `dropDuplicates`, grouping, row hashing, or target-table scan.
- Confirm option B reads only target key columns.
- Confirm the number of Spark validation actions does not grow with schema column count.
- Benchmark the existing 3,339,767-row `test_nuh_soc` case on the same environment.
- Record end-to-end and per-phase timings for at least two warm runs.
- Require at least a 40% reduction from the recorded 1,232-second Glue baseline for the no-dedup path before declaring the performance work complete.
- Run the pilot test suite and `git diff --check`.

## Assumptions

- Existing date/time, numeric, schema-match, encryption, age, postal-code, and dropped-column policies remain unchanged.
- Keyed cross-table overlaps are skipped; replacing old rows with new rows remains out of scope.
- No existing rows are rewritten when a key or newly detected sanitization rule is introduced.
- Upload-session files remain node-local. The local pilot runs as one instance; a future horizontally scaled deployment must use sticky routing or shared encrypted session storage, while the S3 conditional lock already protects mutations across instances.
- Five concurrent Glue runs at four `G.1X` workers each is the default cost/performance ceiling; operators may reduce it through configuration without changing code.
- The retired multipart routes retain only HTTP 410 tombstones for one pilot release and provide no compatibility processing.
- Application changes begin only after this implementation plan is accepted.

## Architecture Decisions and Trade-offs

- **S3 conditional lease instead of DynamoDB or an in-memory lock:** reuses the existing encrypted staging bucket and adds no new service. Conditional `If-None-Match`/`If-Match` operations protect acquisition, renewal, takeover, and release across page refreshes, FastAPI restarts, and future replicas. The trade-off is explicit lease/reconciliation code and bounded lock-prefix listing; a process-local lock cannot provide the same guarantee.
- **Five parallel Glue runs with queuing instead of unlimited concurrency:** allows unrelated tables to progress independently while placing a clear 20-DPU ceiling on the pilot. Queuing prevents transient concurrency-limit failures.
- **HTTP 410 tombstones instead of legacy wrappers:** avoids maintaining two ingestion implementations when no downstream client depends on the old routes, while still giving accidental callers a precise migration response.
- **Structured stdout logging with optional local rotation:** works naturally in ECS/EKS and remains useful on the operator's Mac without introducing a separate observability platform in this pilot.

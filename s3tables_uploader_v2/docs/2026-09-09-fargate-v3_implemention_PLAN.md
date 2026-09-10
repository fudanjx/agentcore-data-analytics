# V3 Deterministic Leased-Worker Design

## Summary

Yes—this is simpler and less prone to orchestration errors.

V3 will:

- Start one leased worker when files are selected.
- Choose 4 vCPU/16 GiB or 8 vCPU/32 GiB once, using extension and file size.
- Keep that worker for profiling, key analysis and final preparation.
- Perform no automatic promotion or task handoff.
- Offer a user-triggered **Retry using large worker** only when the base worker reaches a resource limit.
- Preserve all V1/V2 data-processing and Glue logic unchanged.

## Deterministic routing

Calculate:

```text
routing score = sum(file_size ÷ format_allowance)
```

| File format | 4/16 allowance |
|---|---:|
| `.parquet`, `.parquet.gzip` | 128 MiB |
| `.csv`, `.tsv` | 64 MiB |
| `.xls`, `.xlsx` | 32 MiB |

Routing:

- Score `≤ 1.0`: launch 4/16.
- Score `> 1.0`: launch 8/32.
- Unsupported extension: reject before launch.
- Missing or invalid size: reject rather than guess.
- Use case-insensitive extension matching.
- Check `.parquet.gzip` before shorter suffixes.
- Ignore browser MIME type for routing because it is unreliable.
- Validate the calculation again in FastAPI; never trust a worker size supplied by the browser.

Examples:

- One 100 MiB Parquet: base worker.
- One 300 MiB Parquet: large worker.
- Two 64 MiB Parquet files: base worker.
- One 64 MiB Parquet plus one 32 MiB CSV: large worker because the combined score is `1.0`.
- One 40 MiB XLSX: large worker.

## File-selection trigger

Add an invisible frontend request when the selected file list changes:

```http
POST /api/v3/worker-leases
```

The request contains filename and byte size for every selected file. The API calculates the routing score and launches the appropriate task through its dedicated FIFO queue and EventBridge Pipe.

- Changing the file selection cancels the old lease.
- A task already starting for a cancelled lease checks S3, observes cancellation and exits.
- Clicking Review attaches the upload session to the existing lease.
- Uploading and worker startup happen concurrently.
- If the worker is not ready when upload completes, the UI shows `Worker starting…` and continues polling.
- If JavaScript warm-up fails, Review falls back to creating the lease, preserving current functionality.

Lease expiry:

- No upload after selection: exit after 10 minutes.
- Waiting for key selection or confirmation: exit after 30 idle minutes.
- Processing underway: allow the phase to finish.
- Exit immediately after final preparation and Glue submission.

## Leased-worker processing

The same fixed-size task performs:

1. Initial profiling and anonymization analysis.
2. One or more user-requested key-impact analyses.
3. Final sanitization and pre-Glue validation.
4. Glue job submission.
5. Exit.

Each phase runs in a separate child process. The parent task:

- Monitors memory, temporary storage and timeout.
- Maintains a 10-second S3 heartbeat.
- Keeps the immutable uploaded file in encrypted ephemeral storage.
- Reuses the local file across phases.
- Deletes cached data on completion, cancellation or expiry.

S3 remains authoritative for source objects, commands, results and recovery. Local cache loss only causes a new worker to download the source again.

## Manual large-worker retry

There is no automatic promotion.

If the base worker encounters one of these conditions:

- Child-process RSS reaches 12 GiB.
- Ephemeral-storage use reaches 70%.
- Python `MemoryError`.
- Arrow or Polars allocation failure.
- ECS reports an out-of-memory termination.

Then:

1. Stop the current phase before any Glue submission.
2. Save completed earlier-phase results in S3.
3. Mark the session `RESOURCE_LIMIT_EXCEEDED`.
4. Terminate the base worker.
5. Show **Retry using large worker** in the existing outcome/status area.

The retry endpoint:

```http
POST /api/v3/worker-leases/{lease_id}/retry-large
```

It will:

- Require the authenticated session owner.
- Be available only after a recognized resource failure.
- Reuse the immutable S3 object version and checksum.
- Launch an 8/32 worker.
- Restore completed profile/key results.
- Restart only the interrupted phase from its beginning.
- Use the original request ID and job ID for idempotency.
- Never repeat an ambiguous or already-started Glue submission.

This is a deliberate user action, not automatic promotion. The base and large workers never run concurrently.

## Compatibility and status

Preserve the current UI design, authentication and processing APIs. Add only the warm-up and retry behavior.

Optional status fields:

```json
{
  "worker_state": "STARTING",
  "worker_size": "BASE",
  "routing_score": 0.78,
  "routing_reason": "PARQUET_WITHIN_BASE_ALLOWANCE",
  "can_retry_large": false
}
```

Worker states:

```text
STARTING
AWAITING_UPLOAD
PROFILING
AWAITING_KEY
ANALYSING_KEY
AWAITING_CONFIRMATION
PREPARING
STARTING_GLUE
COMPLETED
RESOURCE_LIMIT_EXCEEDED
FAILED
EXPIRED
CANCELLED
```

Continue using S3 for job records; do not add DynamoDB, EFS, EKS or Lambda.

## Verification

Test the following:

- Every supported extension routes at, below and above its boundary.
- Multiple-file routing uses the combined score.
- The browser and backend produce identical routing decisions.
- A 300 MiB Parquet selects 8/32 at file selection.
- Changing files cancels the earlier lease.
- Immediate Review works while the task is still starting.
- Profile, repeated key analysis and preparation use the same task.
- Local-cache loss reconstructs correctly from S3.
- Memory and storage guards fail safely before Glue starts.
- Manual retry launches only 8/32 and restarts only the incomplete phase.
- Duplicate retry requests launch no duplicate workers.
- Completed or ambiguous Glue submissions cannot be retried.
- Sanitization, anonymization, schema, deduplication, timestamp and Glue outputs match V2 exactly.

Roll out behind a feature flag, record routing score and peak resource usage, and adjust the three allowances later from observed metrics rather than adding dynamic promotion.

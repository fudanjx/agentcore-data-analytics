# V3 Production-Readiness Enhancement Plan

## Goal

Make concurrent uploads safe, observable, and recoverable without changing V1
data-processing rules or adding DynamoDB, EFS, EKS, or Lambda. S3 remains the
durable state store; SQS FIFO and ECS Fargate remain the execution layer.

## Confirmed constraints

- Independent users may profile and prepare uploads in parallel.
- A table mutation (create, append, rollback, or delete) must be serialised per
  `{table_bucket_arn, namespace, table}`. Separate tables must not block each
  other.
- Browser retries, API retries, SQS redelivery, and worker restarts must be
  idempotent.
- Workers must never overwrite a newer lease/session update with a stale
  heartbeat.
- A worker failure or lease expiry must become a durable terminal session
  result, not an indefinitely polling UI.

## Design

### 1. Compare-and-swap S3 records

Lease and compatibility-session records carry an S3 ETag and a monotonically
increasing `state_version`. A mutation reads the current object, applies only
its intended fields, then writes it with `IfMatch`. On `412` or
`ConditionalRequestConflict`, it reloads and retries a bounded number of
times. This prevents the previously observed race where a worker heartbeat
rewrote a lease with `session_id: null` after the API had attached an upload.

The API owns binding/cancellation/retry changes. The worker owns its
heartbeat/phase changes. Both use the same compare-and-swap method; neither
performs an unconditional overwrite of an existing record.

### 2. Table mutation lease

The existing `S3TableLockManager` becomes active immediately before Glue is
started. Its lock key is a SHA-256 of the complete table identity; its payload
contains only operational metadata (request/session/actor/operation), never
row values. It uses `IfNoneMatch: *` to acquire and ETag matching to renew or
release.

The worker passes the lock location and ETag to Glue. Glue releases the lock in
`finally`, after its write/audit/rollback result is terminal. If Glue cannot
start, the worker releases the lock. A stale lock has a bounded expiry and can
be safely taken over with an ETag-matched write. Rollback obtains the same
lock, so it cannot race an append.

### 3. Queue and capacity policy

- Lease messages use the lease ID as FIFO group ID: commands for one worker
  lease are ordered, while different users can profile concurrently.
- Final mutations are guarded by the table lock; the queue group remains the
  table identity for non-leased/legacy operations.
- Maintain explicit limits for base Fargate workers, large Fargate workers, and
  Glue concurrent runs. Excess work queues; it does not launch unlimited
  16/32 GiB tasks.
- DLQ, queue-age, stale-heartbeat, conditional-write-conflict, worker-failure,
  lock-age, Glue-failure, and cost alarms are required operational controls.

## Implementation tasks

### Task 1: Conditional durable records

**Files:** `job_store.py`, `api.py`, `worker.py`, `tests/test_job_store.py`,
`tests/test_api_jobs.py`.

1. Add ETag-aware read/write and bounded compare-and-swap helpers for lease and
   compatibility session records.
2. Make API bind, replace, cancel, and retry use the helper.
3. Make worker heartbeat and phase updates use the helper.
4. Add a regression test that simulates API binding between a worker read and
   heartbeat; assert the bound `session_id` and active expiry survive.
5. Persist a terminal session error if its attached worker expires before
   profiling.

### Task 2: Per-table mutation lock

**Files:** `table_lock.py`, `worker.py`, `api.py`, `glue_job.py`,
`infra/s3_uploader_v2_fargate.py`, tests.

1. Acquire the table lock before starting Glue and include lock bucket/key/ETag
   in Glue arguments.
2. Release on Glue start failure; release from Glue in `finally` for all
   terminal outcomes.
3. Apply the same gate to rollback.
4. Add IAM permissions limited to the managed landing-bucket lock prefix.
5. Test concurrent acquire, stale-lock takeover, duplicate request acquisition,
   and release.

### Task 3: Verification and rollout

1. Run targeted regression tests, then the full V3 test suite in the worker
   image.
2. Build immutable API and worker images; upload the V3 Glue script; deploy a
   CloudFormation change set only after confirming API/worker/Glue/IAM changes.
3. Verify stack completion, API health, task definitions, and no new workers
   for a same-size file-selection change.
4. Exercise two distinct-table uploads concurrently and prove a same-table
   second mutation waits/fails clearly rather than starting competing Glue work.

## Acceptance criteria

- No stale heartbeat can erase an attached session, cancellation, terminal
  state, or expiry extension.
- A failed/expired worker makes the UI terminal and retryable.
- Two users’ files, sessions, and artefacts never mix.
- Different tables process concurrently; only one active writer exists for a
  single target table.
- Duplicate clicks/messages cannot create duplicate workers, Glue runs, history
  events, or rollback actions.
- V1 sanitisation, schemas, timestamp conversion, deduplication, history, and
  rollback semantics remain unchanged.

## Deployment record

Deployed on 2026-09-10 (Singapore):

- API image: `s3-uploader-v2-api:20260910-production-readiness-1`
  (`sha256:47490762f54467109c225b5ff343c89da163aa251816282a8f9f5fb1fcf3e7df`),
  task definition `s3-uploader-v2-api:24`.
- Worker image: `s3-uploader-v2-worker:20260910-production-readiness-1`
  (`sha256:b4aa1408946ba2204bf98e32da0044b42ae169742940b789feaa7046fa37cd29`),
  task definitions `s3-uploader-v2-worker:31` (base) and `:30` (large).
- V3 Glue script: `s3://ah-data-analytics/temp_s3_update/s3_uploader_v3/generic_glue_job.py`,
  uploaded 2026-09-10T13:25:30Z, ETag `96eafa899ec674accc9aad8d396e8a4f`.
- CloudFormation stack `s3-uploader-v2`: `UPDATE_COMPLETE`.
- API health endpoint: `{"status":"ok"}`.
- Base, large, and legacy worker EventBridge Pipes: `RUNNING`.

The Glue execution role has `s3:DeleteObject` only for
`s3-uploader-v2/table-locks/*` in the landing bucket, allowing terminal Glue
runs to release their own table mutation lock without broad delete access.

# V3 Cancel & Start Over — Approved Implementation Plan

**Status:** Approved for implementation only after explicit follow-up approval.

## Objective

Add one **Cancel & Start Over** control for the period beginning with file
selection and ending when the backend accepts **Upload and run ETL**. It must
stop the leased pre-Glue worker cooperatively, remove the immutable raw source
objects, clear upload-specific browser state, and leave the selected destination
and authenticated identity unchanged.

The feature must not alter V1-compatible sanitisation, schema handling,
de-duplication, upload history, rollback, mutation FIFO ordering, or Glue
processing.

## User-facing behaviour

- Show **Cancel & Start Over** after files are selected, including while the
  base or large worker is starting.
- Keep it available through receipt, profiling, review, key-impact analysis,
  acknowledgement, and pre-ETL failure states.
- Ask for confirmation because cancellation permanently deletes staged raw
  source versions. It never changes the S3 Tables destination.
- On success, preserve the bucket, namespace, table/create-table choice, and
  current authenticated/emulated identity. Clear the file picker, user tag,
  preflight result, schema/type choices, sanitisation choices, key analysis and
  acknowledgement, status/outcome, browser session key, and browser lease key.
- Stop UI polling and abort an in-flight Review request before issuing the
  server cancellation request.
- Disable the control immediately when **Upload and run ETL** is clicked. If
  the request fails before backend acceptance, reconcile session state and
  restore the control only if the server says cancellation remains allowed.
- On a page reload, render the control from the authoritative
  `can_cancel_and_start_over` lease/session field.
- If warm-up never created a lease, clear browser-only state; if a late warm-up
  response arrives after reset, cancel that returned lease instead of retaining
  an orphan worker.

## API and durable-state contract

Extend the canonical lease cancellation route:

```http
DELETE /api/v3/worker-leases/{lease_id}
```

- Require the authenticated lease owner. A different user receives
  `403 WORKER_LEASE_FORBIDDEN` and no state or source object changes.
- Support both unattached file-selection leases and attached compatibility
  upload sessions.
- Allow cancellation only before ETL acceptance: worker states
  `STARTING`, `AWAITING_UPLOAD`, `PROFILING`, `AWAITING_KEY`,
  `ANALYSING_KEY`, and `AWAITING_CONFIRMATION`, plus attached session phases
  `RECEIVED`, `PROFILING`, `READY_FOR_REVIEW`, `KEY_ANALYSING`,
  `READY_FOR_ACKNOWLEDGEMENT`, and pre-ETL `FAILED`.
- Reject requests after acceptance, preparation, dispatch, Glue start, or an
  ambiguous durable ingestion record with
  `409 CANCEL_AND_START_OVER_UNAVAILABLE`.
- Return success idempotently for the owner when a lease was already cancelled;
  retry source cleanup if a previous request was incomplete.
- Add `can_cancel_and_start_over` to lease responses and the embedded lease in
  compatibility-session responses.

### ETL acceptance boundary

The ingestion endpoint obtains a CAS-protected cancellation lock before any
ingestion-side durable action, including late de-duplication contract
activation. Cancellation and ETL acceptance therefore cannot both win.

- Validation failures before the lock leave cancellation available.
- Once locked, the server must not unlock merely because the browser loses the
  response. Browser error handling fetches session state to determine the
  authoritative result.
- If setup fails after locking but before a safe recoverable result exists, the
  session remains locked and reports the failure rather than allowing reset of
  an ambiguous table operation.

### Cancellation transaction model

S3 records are not transactional, so cancellation is implemented as an
idempotent, ordered state machine:

1. Validate owner, attached session ownership, and absence of the acceptance
   lock.
2. CAS-transition the lease to terminal `CANCELLED`.
3. CAS-transition the attached compatibility session to terminal `DELETED`.
4. Delete every raw landing object using the recorded `(source_key,
   source_version_id)` pair.
5. Return success only when cleanup completes. Record `cleanup_pending` and
   return `503 CANCEL_CLEANUP_INCOMPLETE` when deletion cannot complete; a
   repeated DELETE resumes safely.

Lease cancellation and session deletion are monotonic terminal transitions.
Worker heartbeats, late child results, and stale browser writes must never move
either record back into an active state.

## Multipart and worker handling

### Review upload receipt

- Track the multipart upload IDs and completed object versions while receiving
  browser files.
- Abort unfinished multipart uploads and delete already-completed exact versions
  if the request is aborted, cancellation wins, or receipt fails before the
  compatibility session is committed.
- Re-check the supplied lease immediately before session binding. A cancelled
  lease returns `409 WORKER_LEASE_UNAVAILABLE`; it must not create a replacement
  lease or leave raw files behind.

### Cooperative worker shutdown

No API-side `ecs:StopTask` permission is required.

- During each active child process, reload the lease approximately every second.
- On `CANCELLED`, request child termination, wait a bounded grace period, then
  force-kill only that child if necessary.
- Exit the parent worker and remove the ephemeral cache directory.
- Change worker heartbeats to update heartbeat metadata only. Before any worker
  state transition, use a CAS guard that refuses to overwrite a terminal
  cancellation.
- Apply the same terminal-state guard to worker writes of compatibility-session
  results, preventing a just-finished profile/key phase from reviving a deleted
  session.

## Infrastructure and permissions

- Add `s3:DeleteObjectVersion` for the landing bucket to the API task role.
- Keep ECS task-stop permissions absent: shutdown is cooperative and
  lease-authoritative.
- Add noncurrent-version expiration to the existing versioned landing-bucket
  lifecycle rules so successful and cancelled raw objects cannot persist only
  as noncurrent versions.
- Preserve existing SSE-KMS, versioning, scoped bucket access, worker queues,
  and the single asynchronous mutation dispatcher.

## Implementation and verification

### Backend

- Add guarded lease/session transition helpers in `job_store.py`.
- Extend cancellation, session creation/binding, ingestion acceptance, and safe
  session response logic in `api.py`.
- Implement child-phase cancellation detection and non-destructive heartbeat
  behaviour in `worker.py`.

### Frontend

- Add the button to the existing upload-action area in `static/index.html` and
  its handler/state reset logic in `static/app.js`.
- Use abort controllers and a selection generation token to prevent late Review
  or warm-up responses from recreating state after reset.

### Tests

- Unattached and attached cancellation, owner authorization, and idempotency.
- Cancellation during profiling and key analysis, including the stale-heartbeat
  and late-child-result races.
- Exact-version deletion for one and many files; cleanup retry after an injected
  S3 failure.
- Multipart abort and partial receipt cleanup.
- Rejection after ETL acceptance and browser reconciliation after an ambiguous
  response.
- Frontend reset preserves destination/identity and clears all upload-specific
  controls; cancellation visibility follows server state after reload.
- CloudFormation IAM and lifecycle assertions.
- Existing V3 API, worker, dispatcher, sanitisation, de-duplication, history,
  rollback, and FIFO tests remain green.

## Deployment gate

After implementation approval: run the full targeted suite, build API and
worker images, publish immutable ECR tags, apply CloudFormation, deploy ECS,
and perform a browser smoke test for cancellation before and after ETL
acceptance. No implementation, image publication, or deployment is authorised
by this document alone.

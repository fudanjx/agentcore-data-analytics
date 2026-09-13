# Complete Production Flow

This document uses stable module names and relative repository paths. It is the
runtime flow for package version 3.0.0.

```text
file selection
  -> POST /api/v3/worker-leases
  -> API stores lease in S3 and sends lease:<id> to BASE or LARGE FIFO
  -> EventBridge Pipe runs one leased Fargate worker

multipart upload / review
  -> POST /api/v3/upload-sessions
  -> API stores immutable source versions and attaches session to lease
  -> worker profiles sources and persists review data in S3

key impact / confirmation
  -> worker reuses its cached source and persists durable results

ingestion accepted
  -> API writes immutable job request + mutation command + initial status
  -> worker writes sanitised Parquet and manifest
  -> worker marks job READY_FOR_MUTATION
  -> dispatcher starts Glue only when table lock and Glue capacity allow
  -> dispatcher watches terminal result, persists status, releases lock,
     then acknowledges the FIFO receipt
```

## Ownership

| Module | Authority |
| --- | --- |
| `api.py` | Browser state, identity, S3 Tables control plane, session/lease creation, mutation command enqueue. |
| `worker.py` | Profile, review/key analysis, sanitisation and preparation only. |
| `mutation_dispatcher.py` | FIFO receipt lifecycle, per-table locks, Glue capacity and Glue status. |
| `glue_job.py` | S3 Tables create/append/rollback and audit/QC projection. |
| `job_store.py` | Neutral-prefix durable state and read-only historical-prefix adapter. |
| `table_lock.py` | Conditional S3 table lock acquisition/recovery/release. |

## Ordering properties

The mutation queue uses the table identity hash as its FIFO message group. A
single dispatcher can supervise different-table Glue runs up to its configured
quota, but cannot release a same-table successor until the preceding mutation
has terminal state. Its S3 lock is a crash-recovery guard, not a second queue.

The base/large worker queues are separate only because Fargate task resources
are immutable after launch. They do not impose mutation ordering.

## Failure boundaries

- Before ingestion acceptance, cancellation terminates the lease and cleans
  temporary source/session data; no mutation is created.
- After acceptance, durable request/status/mutation records make retries and
  UI polling independent of API or worker restarts.
- Dispatcher restart reconstructs active table locks/statuses and checks Glue
  for an ambiguous start before attempting anything else.
- Worker-launch and mutation messages use separate DLQs. Operators inspect
  S3 state and Glue before a manual redrive.

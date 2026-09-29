# Complete Production Flow

This document uses stable module names and relative repository paths. It is the
runtime flow for package version 3.1.0 (post-refactor).

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
  -> POST /api/v3/upload-sessions/{session_id}/ingestions
  -> API writes immutable job request + mutation command + initial status
  -> worker writes sanitised Parquet and manifest
  -> worker marks job READY_FOR_MUTATION
  -> dispatcher starts Glue only when table lock and Glue capacity allow
  -> dispatcher watches terminal result, persists status, releases lock,
     then acknowledges the FIFO receipt
```

## Ownership

The old monolithic `api.py` is gone. HTTP responsibilities now live across
four cooperating packages:

| Module / package | Authority |
| --- | --- |
| `api/v3/*.py` | HTTP surface — one router per resource (`buckets.py`, `identity.py`, `upload_sessions.py`, `worker_leases.py`, `jobs.py`, `mutations.py`, `upload_history.py`, `skills.py`, `dev.py`). Decode request, call a service, return response. Never contains business logic or boto3 calls. |
| `api/static/frontend.py` | Cookie login, `/login`, `/logout`, `/`, `/static/{asset}`. Registered only when `settings.frontend_surface_enabled`. |
| `app/factory.py` | Single `create_app(settings)` factory — the only place `FastAPI(...)` is constructed. Registers every core router (and the frontend `identity` / `dev` routers when enabled) with `Depends(require_auth)`, which dispatches to bearer or cookie per request. Gates `docs_url`/`redoc_url` on LOCAL. |
| `app/lifespan.py` | Builds singletons (boto3 clients, `S3JobStore`, `BearerAuthService`), raises `anyio`'s default threadpool cap to `settings.async_thread_limit`, and yields the state as ASGI state so every request reads via `request.state`. |
| `app/dependencies.py` | `SettingsDep`, `UserDep`, `StoreDep`, service-factory Deps, `require_auth`, `resolve_user`, `enforce_ownership`. |
| `app/middlewares.py` | `CorrelationIdMiddleware` (always), `FrontendCookieGate` (frontend envs only; short-circuits when an `Authorization` header is present so bearer flows through `require_auth`). |
| `app/exception_handlers.py` | Global `UploaderError` / `ControlPlaneError` / `ClientError` → JSON. |
| `services/*.py` | Business logic — S3 Tables control plane, contract mutation, lease lifecycle, mutation enqueue, audit read, tag-filtered bucket listing, bearer secret cache. |
| `worker.py` | Profile, review/key analysis, sanitisation and preparation only. |
| `mutation_dispatcher.py` | FIFO receipt lifecycle, per-table locks, Glue capacity and Glue status. |
| `glue_job.py` | AWS Glue Spark script — S3 Tables create/append/rollback and audit/QC projection. Runs inside Glue's Spark runtime, not this container; the dispatcher invokes it via `glue.start_job_run(...)`. |
| `job_store.py` | Neutral-prefix durable state and read-only historical-prefix adapter. |
| `table_lock.py` | Conditional S3 table lock acquisition/recovery/release. |

## Authentication

Bearer auth is accepted in **every environment**. Cookie auth is a
second, opt-in path that is only available in the environments that
serve the temporary frontend (LOCAL when `S3_UPLOADER_SERVE_LOCAL_FRONTEND=true`,
and DEV). Both flows extract an end-user identity that is stamped onto every
persisted record.

| Environment | Bearer accepted | Cookie accepted | Identity header used |
| --- | :---: | :---: | --- |
| LOCAL (frontend) | ✓ | ✓ | bearer → `User-ID` (email); cookie → `X-Pilot-User-Id` (profile key) |
| LOCAL (API-only) | ✓ | ✗ | `User-ID` (email) |
| DEV | ✓ | ✓ | bearer → `User-ID` (email); cookie → `X-Pilot-User-Id` (profile key) |
| STG / PRD | ✓ | ✗ | `User-ID` (email) |

How the router picks the flow ([`app/dependencies.py`](../../app/dependencies.py),
[`app/middlewares.py`](../../app/middlewares.py)):

- If the request carries an `Authorization` header, `require_auth`
  validates it as `Bearer <token>` via `BearerAuthService.verify` and
  stamps `request.state.auth_method = "bearer"`. `FrontendCookieGate`
  short-circuits so cookie validation never runs on the same request.
  **When both flows are possible, bearer wins if the header is
  present** — there is no silent fallback to cookie.
- Otherwise, if the environment serves the frontend surface, the
  cookie gate validates the signed session and `require_auth` stamps
  `request.state.auth_method = "cookie"`.
- In bearer-only environments (no frontend surface) a missing
  `Authorization` header is rejected outright with `BearerAuthRequired`.

`resolve_user` then reads `request.state.auth_method` and picks
`User-ID` (bearer) or `X-Pilot-User-Id` (cookie) — the identity header
follows the resolved flow, not the environment.

Bearer secrets live in AWS Secrets Manager and are cached in-process by
`BearerAuthService` for 1 hour (`bearer_cache_ttl_seconds`), with an
opportunistic refresh-on-miss capped at once per 5 minutes
(`bearer_refresh_min_interval_seconds`). Because bearer auth is now
accepted everywhere, `S3_UPLOADER_BEARER_SECRET_ARN` is required at
startup in every environment (LOCAL included) — see
[`environment-variables.md`](environment-variables.md).

Ownership checks (`enforce_ownership`) run in every environment — the
calling application controls WHO can act, this API controls WHICH
RECORDS they can touch.

## Queue producers, consumers, and launch models

Three SQS FIFO queues carry work between components. The API is the **only**
producer for all three; nothing outside this repository writes to them.

| Queue | Producer | Message body | Consumer model |
| --- | --- | --- | --- |
| `s3-uploader-base.fifo` | API — [`LeaseService.dispatch`](../../services/leases.py:83) after a lease is created or rebound | `lease:<lease_id>` | EventBridge Pipe → one-shot Fargate task per message |
| `s3-uploader-large.fifo` | API — same helper, routed by `worker_size` | `lease:<lease_id>` | Same as base, larger task definition |
| `s3-uploader-mutations.fifo` | API — [`MutationEnqueuerService.enqueue`](../../services/mutations.py:19) at ingestion acceptance | mutation id | **Long-running** `s3-uploader-mutation-dispatcher` ECS service long-polls with `sqs.receive_message` |

The worker itself never calls `send_message`; grep for `send_message` in
`worker.py` returns zero hits. All fan-out originates in the API.

### Two consumer models — why the asymmetry

The base/large queues and the mutation queue use **different launch models**
on purpose, which is the reason there's still polling in the system even
though EventBridge Pipes launches workers on-the-fly.

**Base/large queues → per-message Fargate task (no polling).** The API
enqueues one `lease:<id>` message; an EventBridge Pipe converts that message
into an ECS `RunTask` call against the `s3-uploader-worker` task family. The
container starts, does exactly one job, and exits. There is nothing to poll
because the message *is* the task launch. Each worker sees exactly one job in
its lifetime.

**Mutation queue → dispatcher polls.** No Pipe is attached. A single
long-running ECS service (`s3-uploader-mutation-dispatcher`, desired count
= 1) calls `sqs.receive_message` in a loop. It needs polling — not
per-message task launch — because mutation processing requires **stateful
coordination** across multiple in-flight messages:

- **Per-table FIFO ordering.** A same-table successor must wait until the
  previous mutation reaches terminal state. A one-shot task per message
  cannot see the other in-flight mutations.
- **Glue capacity throttling.** The dispatcher caps concurrent Glue runs
  (`max_concurrent_glue`, default 5). A per-message launch model would need
  every task to re-read shared state to decide whether to start Glue.
- **Terminal-state authority.** The dispatcher is the single writer of
  mutation terminal state (see below). Long-polling keeps that ownership
  in one process.

So "polling vs. no polling" isn't a contradiction — it's the split between
work that fans out cleanly (workers) and work that needs a coordinator
(mutations).

## Worker launch flow

The full chain from lease creation to a running worker process:

```text
POST /api/v3/worker-leases
  -> LeaseService.create_lease writes lease JSON to S3
  -> LeaseService.dispatch calls sqs.send_message
        QueueUrl = base_worker_queue_url or large_worker_queue_url
        MessageBody = "lease:<lease_id>"
        MessageGroupId = "<lease_id>"          (per-lease FIFO)
  -> EventBridge Pipe (source: base/large FIFO queue)
       target = ECS RunTask against s3-uploader-worker task family
       target parameters override the container env:
         containerOverrides.environment = [
           { name: "S3_UPLOADER_JOB_ID", value: <message body> }
         ]
  -> Fargate schedules a new task
       image: s3-uploader-worker (Dockerfile.worker)
       CMD:   python3 -m s3tables_uploader.worker
  -> worker.main() runs:
       - reads os.environ["S3_UPLOADER_JOB_ID"]          (worker.py:670)
       - WorkerSettings.from_environ() picks up deployment config
       - dispatches to run_leased_worker(...) or process_job(...)
  -> worker exits when the job completes
       Fargate reaps the task; the next message launches a fresh task
```

Key properties:

- **One-shot, stateless.** The worker owns no listening socket, no long-lived
  in-memory state, and no queue polling. Its filesystem is disposable; S3 is
  the source of truth.
- **The message body is the payload.** EventBridge Pipes injects the SQS
  message body verbatim as `S3_UPLOADER_JOB_ID` at task-launch time. There
  is no other channel between the queue and the worker — no shared memory,
  no on-disk state, no environment inherited from the API.
- **`WorkerSettings` carries deployment config only.** Region, buckets,
  prefixes, Glue job name, encryption secret ARN — everything static per
  environment. Per-invocation payload (`S3_UPLOADER_JOB_ID`) stays outside
  `WorkerSettings` because it changes per message; adding it there would
  falsely imply it's deployment-scoped.
- **No queue URL on `WorkerSettings`.** The worker never talks to SQS, so
  the three queue URLs live only on the API-side `Settings` and (for the
  mutation queue) `MutationDispatcherSettings`. Adding queue URLs to
  `WorkerSettings` would only make sense if the launch model ever changed
  from "Pipe launches one task" to "worker polls" — which would defeat the
  point of the current architecture.

The dispatcher's launch model is the opposite of all of the above: it runs
as a normal ECS service with desired count = 1, holds boto3 clients across
requests, and its own `MutationDispatcherSettings` includes
`queue_url` because it DOES poll.

## Ordering properties

The mutation queue uses the table identity hash as its FIFO message group. A
single dispatcher can supervise different-table Glue runs up to its configured
quota, but cannot release a same-table successor until the preceding mutation
has terminal state. Its S3 lock is a crash-recovery guard, not a second queue.

The base/large worker queues are separate only because Fargate task resources
are immutable after launch. They do not impose mutation ordering.

## Terminal-state authority

The dispatcher is the **single writer** for mutation terminal state. The
old `api.py:_reconcile_glue_job()` no-op wrapper has been removed;
`GET /api/v3/upload-sessions/{id}` now mirrors the dispatcher-owned status
via `store.get_status(job_id)` directly — no cross-writer race.

## Data-plane invariants

- Every S3 write from this repository uses `ServerSideEncryption=AES256`
  (single source of truth: `core.constants.S3_SSE`). Applies to the API,
  `job_store.py`, and the worker's prepared-file/manifest writes.
- S3 Tables buckets created via `POST /api/v3/buckets` are tagged
  `{PROJECT-NAME: Bot-NUHS, PROJECT-NAME-SHORT: Bot-NUHS, APP: Data-Insights}`.
  `TableBucketService.list_buckets` filters listings by the `APP` tag so this
  API never surfaces other apps' buckets in the same account. Tag lookup is
  cached per-process for 6 hours; a one-shot `POST /api/v3/buckets/cache/purge`
  clears it after an out-of-band re-tag.
- Bucket and namespace deletion is administrator-only. Namespace deletion
  requires `confirm: true` and still deletes only an empty namespace.
  `DELETE /api/v3/buckets` requires `force: true`: it preflights every table
  lock, deletes every table (including non-uploader tables), permanently
  purges uploader contracts, deletes each namespace, and then deletes the
  bucket. `delete_skill_prefix: true` additionally purges every object version
  and delete marker below `skills/<table-bucket-name>/`; `false` preserves that
  prefix. Canonical S3 audit-history objects are always retained.
- Audit history reads only the canonical scoped prefix
  `<history_bucket>/<history_prefix>/<scope=hash(arn|ns)>/<table>/`. Legacy
  fallbacks were dropped; `scripts/migrate_legacy_audit.py` moves any tail
  from the old landing-bucket prefixes into the canonical layout.

## Failure boundaries

- Before ingestion acceptance, cancellation terminates the lease and cleans
  temporary source/session data; no mutation is created.
- After acceptance, durable request/status/mutation records make retries and
  UI polling independent of API or worker restarts.
- Dispatcher restart reconstructs active table locks/statuses and checks Glue
  for an ambiguous start before attempting anything else.
- Worker-launch and mutation messages use separate DLQs. Operators inspect
  S3 state and Glue before a manual redrive.

## Environment-specific behaviour

| Feature | LOCAL frontend | LOCAL API-only | DEV | STG | PRD |
| --- | :---: | :---: | :---: | :---: | :---: |
| `/`, `/login`, `/static/*`, `/api/v3/dev/*`, `/api/v3/identity` | ✓ | ✗ | ✓ | ✗ | ✗ |
| `docs_url` / `redoc_url` | ✓ | ✓ | ✗ | ✗ | ✗ |
| Bearer auth accepted | ✓ | ✓ | ✓ | ✓ | ✓ |
| Cookie auth accepted | ✓ | ✗ | ✓ | ✗ | ✗ |
| History bucket / skill-bundle destination defaults | ✓ | ✓ | ✓ | ✗ (required) | ✗ (required) |
| Uvicorn `--access-log` | ✓ | ✓ | ✓ | ✗ | ✗ |

Settings validation in `config.py` fails fast at startup for missing
required values: `S3_UPLOADER_BEARER_SECRET_ARN` is required in every
environment (bearer auth is universally accepted); history
bucket/prefix and skill-bundle bucket/prefix are required in STG/PRD
only (defaulted in LOCAL/DEV); `S3_UPLOADER_COOKIE_SECURE` must be
`true` in DEV; `S3_UPLOADER_RAW_RETENTION_DAYS` and
`S3_UPLOADER_ASYNC_THREAD_LIMIT` are bounds-checked (1..30 and
1..1000 respectively). See
[`environment-variables.md`](environment-variables.md) for the
authoritative env-var reference and
[`../2026-09-21-refactor-conventions.md`](../2026-09-21-refactor-conventions.md)
for the broader convention overview.

## Runtime tuning

The API lifespan raises `anyio`'s default threadpool cap to
`settings.async_thread_limit` (default 100) so sync FastAPI handlers
and `run_in_threadpool` calls in async upload endpoints share one
tunable ceiling ([`app/lifespan.py`](../../app/lifespan.py)). boto3
client `max_pool_connections` values are computed as ratios of that
ceiling — S3 = 1.0×, s3tables = 0.25×, sqs = 0.15×, glue = 0.10×,
secretsmanager = 0.10× — with a floor of 10 to match botocore's own
default. Sizing S3 aggressively reflects the hot path: every upload
endpoint issues at least one S3 request. Change the ceiling with a
single env var; the per-client caps follow automatically.

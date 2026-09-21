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
| `app/factory.py` | Single `create_app(settings)` factory — the only place `FastAPI(...)` is constructed. Registers routers with `Depends(require_bearer)` in hardened envs; gates `docs_url`/`redoc_url` on LOCAL. |
| `app/lifespan.py` | Builds singletons (boto3 clients, `S3JobStore`, `BearerAuthService`) and yields them as ASGI state so every request reads via `request.state`. |
| `app/dependencies.py` | `SettingsDep`, `UserDep`, `StoreDep`, service-factory Deps, `require_bearer`, `resolve_user`, `enforce_ownership`. |
| `app/middlewares.py` | `CorrelationIdMiddleware` (always), `FrontendCookieGate` (frontend envs only). |
| `app/exception_handlers.py` | Global `UploaderError` / `ControlPlaneError` / `ClientError` → JSON. |
| `services/*.py` | Business logic — S3 Tables control plane, contract mutation, lease lifecycle, mutation enqueue, audit read, tag-filtered bucket listing, bearer secret cache. |
| `worker.py` | Profile, review/key analysis, sanitisation and preparation only. |
| `mutation_dispatcher.py` | FIFO receipt lifecycle, per-table locks, Glue capacity and Glue status. |
| `glue_job.py` | AWS Glue Spark script — S3 Tables create/append/rollback and audit/QC projection. Runs inside Glue's Spark runtime, not this container; the dispatcher invokes it via `glue.start_job_run(...)`. |
| `job_store.py` | Neutral-prefix durable state and read-only historical-prefix adapter. |
| `table_lock.py` | Conditional S3 table lock acquisition/recovery/release. |

## Authentication

Two auth strategies coexist, selected by environment. Both flows extract an
end-user identity that is stamped onto every persisted record.

| Environment | Client auth | Identity header | Enforced at |
| --- | --- | --- | --- |
| LOCAL (frontend) | signed cookie (`s3_uploader_session`) | `X-Pilot-User-Id` | `FrontendCookieGate` middleware + `UserDep` |
| DEV | signed cookie | `X-Pilot-User-Id` | same as LOCAL (frontend) |
| LOCAL (API-only) | `Authorization: Bearer <secret>` | `User-ID` (email) | router-level `Depends(require_bearer)` + `UserDep` |
| STG / PRD | `Authorization: Bearer <secret>` | `User-ID` (email) | same as LOCAL (API-only) |

Bearer secrets live in AWS Secrets Manager and are cached in-process by
`BearerAuthService` for 1 hour, with an opportunistic refresh-on-miss capped
at once per 5 minutes (`bearer_refresh_min_interval_seconds`). Ownership
checks (`enforce_ownership`) run in every environment — the calling
application controls WHO can act, this API controls WHICH RECORDS they can
touch.

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
| Cookie login | ✓ | ✗ | ✓ | ✗ | ✗ |
| Bearer auth required | ✗ | ✓ | ✗ | ✓ | ✓ |
| History bucket / skill-bundle destination defaults | ✓ | ✓ | ✓ | ✗ (required) | ✗ (required) |
| Uvicorn `--access-log` | ✓ | ✓ | ✓ | ✗ | ✗ |

Settings validation in `config.py` fails fast at startup for missing
required values (bearer secret ARN, history bucket/prefix, skill-bundle
bucket/prefix in STG/PRD; secure cookie in DEV). See
[`../2026-09-21-refactor-conventions.md`](../2026-09-21-refactor-conventions.md)
for the full config reference.

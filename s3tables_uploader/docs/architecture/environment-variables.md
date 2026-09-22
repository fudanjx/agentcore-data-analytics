# Environment variables — three-app reference

Living reference for every environment variable read at runtime by the
three deployable processes in `s3tables_uploader/`. This is the
authoritative list; smaller tables elsewhere (`.env.example`, the
excerpt in [`../2026-09-21-refactor-conventions.md`](../2026-09-21-refactor-conventions.md))
should be treated as pointers to this document.

## What the three apps are

| App | Entrypoint | Settings class | Where it runs |
| --- | --- | --- | --- |
| API | [`entrypoint.py`](../../entrypoint.py) → uvicorn | [`Settings`](../../config.py) | Long-running Fargate service (public HTTP surface) |
| Worker | [`worker.py::main`](../../worker.py) | [`WorkerSettings`](../../config.py) | One-shot Fargate task launched per SQS `lease:<id>` message by an EventBridge Pipe |
| Mutation dispatcher | [`mutation_dispatcher.py::main`](../../mutation_dispatcher.py) | [`MutationDispatcherSettings`](../../mutation_dispatcher.py) | Long-running Fargate service (desired count = 1) polling the mutations FIFO queue |

The three processes read overlapping but distinct env vars. Each has
its own settings class and its own `from_environ` classmethod, and no
class inherits from another — a change to one no longer implicitly
changes the others (fixed in commit `2c43b32`).

## Definitions

- **MUST-set** — the value is read via `_required(...)`, which raises
  `ConfigurationError` at startup if the variable is missing or empty.
  In STG/PRD, some `_optional` values are additionally promoted to
  MUST-set by explicit validation; those cases are called out in the
  "MUST in" column.
- **SHOULD-set** — the code has a hard-coded default, but the default
  is a demo value or an untuned knob you almost certainly want to
  override for a real deployment.
- **Default** — the literal fallback used by `from_environ` when the
  variable is absent.

---

## Table A — API app (`Settings`)

Source of truth: [`Settings.from_environ`](../../config.py). All
values below are read by the API service.

### Must be set

| Env var | Type | MUST in | Notes |
| --- | --- | --- | --- |
| `AWS_REGION` | string | all | boto3 client region for every AWS SDK client the API constructs. |
| `S3_UPLOADER_LANDING_BUCKET` | string | all | Raw-upload landing bucket. |
| `S3_UPLOADER_LANDING_PREFIX` | string | all | Prefix under the landing bucket; leading/trailing `/` stripped. |
| `S3_UPLOADER_BASE_QUEUE_URL` | SQS URL | all | Base worker FIFO queue (small tasks). |
| `S3_UPLOADER_LARGE_QUEUE_URL` | SQS URL | all | Large worker FIFO queue (bigger task definition). |
| `S3_UPLOADER_MUTATION_QUEUE_URL` | SQS URL | all | Mutations FIFO queue; the API is the sole producer, the dispatcher is the sole consumer. |
| `S3_UPLOADER_LOGIN_PASSWORD` | string | all | Cookie-login password (single-user, cookie flow only; still required in bearer-only envs for construction symmetry). |
| `S3_UPLOADER_LOGIN_SECRET` | string ≥32 chars | all | HMAC signing key for the session cookie. Startup fails if shorter than 32 characters. |
| `S3_UPLOADER_GLUE_JOB_NAME` | string | all | Name of the Glue job the dispatcher invokes; also referenced by the API for symmetry. |
| `S3_UPLOADER_CONTRACT_BUCKET` | string | all | Bucket holding per-table contracts. |
| `S3_UPLOADER_CONTRACT_PREFIX` | string | all | Prefix under the contract bucket. |
| `S3_UPLOADER_BEARER_SECRET_ARN` | Secrets Manager ARN | **all** | ARN of the Secrets Manager secret whose `SecretString` is the bearer token. **Required in every environment** (LOCAL included) because bearer auth is now accepted in every environment — see [`complete-v3-flow.md §Authentication`](complete-v3-flow.md#authentication). |
| `S3_UPLOADER_HISTORY_BUCKET` | string | STG / PRD | LOCAL/DEV default: `ah-data-analytics`. STG/PRD startup fails if missing. |
| `S3_UPLOADER_HISTORY_PREFIX` | string | STG / PRD | LOCAL/DEV default: `temp_s3_update/web_ingest/upload_history`. STG/PRD startup fails if missing. |
| `S3_UPLOADER_SKILL_BUNDLE_BUCKET` | string | STG / PRD | LOCAL/DEV default: `agentcore-harness-dev`. STG/PRD startup fails if missing. |
| `S3_UPLOADER_SKILL_BUNDLE_PREFIX` | string | STG / PRD | LOCAL/DEV default: `skills`. STG/PRD startup fails if missing. |

### Should be set (has a default, override for real deploys)

| Env var | Default | Notes |
| --- | --- | --- |
| `S3_UPLOADER_ENVIRONMENT` | `LOCAL` | One of `LOCAL` / `DEV` / `STG` / `PRD`. Drives every computed property (`frontend_surface_enabled`, `docs_enabled`, `debug_logging_enabled`, `access_log_enabled`). Set explicitly in every non-local deploy. |
| `S3_UPLOADER_COOKIE_SECURE` | `true` | Must be `true` in DEV or startup fails. Irrelevant in STG/PRD (no cookies issued). LOCAL developers may set `false` for HTTP dev. |
| `S3_UPLOADER_SERVE_LOCAL_FRONTEND` | `true` | LOCAL only. `false` switches LOCAL into API-only mode (no cookie flow, bearer required to reach any endpoint). Ignored in DEV/STG/PRD. |
| `S3_UPLOADER_LOG_LEVEL` | `INFO` | Uppercased before use. LOCAL/DEV also enable uvicorn access logs unconditionally. |
| `S3_UPLOADER_RAW_RETENTION_DAYS` | `1` | 1..30; startup fails outside that range. Days to keep raw uploads before lifecycle purge. |
| `S3_UPLOADER_SESSION_TTL_SECONDS` | `43200` | 12 h. Cookie session TTL. |
| `S3_UPLOADER_BEARER_CACHE_TTL_SECONDS` | `3600` | In-memory bearer-secret cache TTL. |
| `S3_UPLOADER_BEARER_REFRESH_MIN_INTERVAL_SECONDS` | `300` | Damper on refresh-on-miss: at most one forced Secrets Manager fetch per this interval when a presented token misses the cache. |
| `S3_UPLOADER_ASYNC_THREAD_LIMIT` | `100` | 1..1000; startup fails outside that range. Raises `anyio`'s default threadpool cap so sync FastAPI handlers and `run_in_threadpool` calls share one tunable ceiling. boto3 client pool caps are computed as ratios of this value (S3 = 1.0×, s3tables = 0.25×, sqs = 0.15×, glue = 0.10×, secretsmanager = 0.10×) with a floor of 10. |
| `S3_UPLOADER_API_BASE_URL` | `""` | Optional; currently unused inside the API. Retained for future URL-building needs (CORS origin, email links, etc.). |

### Uvicorn envs (read by `scripts/start_api.py`, not by `Settings`)

Not part of `Settings.from_environ`; still relevant to any deploy that
uses the shipped launcher.

| Env var | Default | Notes |
| --- | --- | --- |
| `UVICORN_HOST` | `0.0.0.0` | Bind host. |
| `UVICORN_PORT` | `8090` | Bind port. |
| `UVICORN_WORKERS` | `1` | Uvicorn worker processes. |

---

## Table B — Worker app (`WorkerSettings`)

Source of truth: [`WorkerSettings.from_environ`](../../config.py).
Every field is `_required`, so all rows below are MUST-set.

| Env var | MUST in | Notes |
| --- | --- | --- |
| `AWS_REGION` | all | Same as API. |
| `S3_UPLOADER_LANDING_BUCKET` | all | Same as API. |
| `S3_UPLOADER_LANDING_PREFIX` | all | Same as API. |
| `S3_UPLOADER_GLUE_JOB_NAME` | all | The worker itself does not call Glue, but the value is carried on `WorkerSettings` for parity with pathing conventions. |
| `S3_UPLOADER_CONTRACT_BUCKET` | all | Contract bucket the worker reads. |
| `S3_UPLOADER_CONTRACT_PREFIX` | all | Contract prefix. |
| `S3_UPLOADER_ENCRYPTION_SECRET_ARN` | all | **Worker-only** — ARN of the Secrets Manager secret used by `sanitization` to decrypt/encrypt sensitive columns. Not read by the API or the dispatcher. |

### Non-config env vars

- `S3_UPLOADER_JOB_ID` — per-invocation payload injected by
  EventBridge Pipes as an ECS `containerOverrides.environment` entry.
  The worker reads it directly from `os.environ` in `main()`; it is
  intentionally **not** on `WorkerSettings` because it changes per
  message and would falsely imply deployment-scoped configuration.
  See [`complete-v3-flow.md §Worker launch flow`](complete-v3-flow.md#worker-launch-flow).

---

## Table C — Mutation dispatcher (`MutationDispatcherSettings`)

Source of truth:
[`MutationDispatcherSettings.from_environ`](../../mutation_dispatcher.py).
Fully independent from `WorkerSettings` since commit `2c43b32` — the
dispatcher reads its own env directly.

### Must be set

| Env var | Type | MUST in | Notes |
| --- | --- | --- | --- |
| `AWS_REGION` | string | all | boto3 region for the dispatcher's S3, SQS, and Glue clients. |
| `S3_UPLOADER_LANDING_BUCKET` | string | all | Landing bucket; table locks live under `<landing_prefix>/table-locks/`. |
| `S3_UPLOADER_LANDING_PREFIX` | string | all | Same as API. |
| `S3_UPLOADER_MUTATION_QUEUE_URL` | SQS URL | all | The FIFO queue the dispatcher long-polls. |
| `S3_UPLOADER_GLUE_JOB_NAME` | string | all | Name of the Glue job started via `glue.start_job_run`. |
| `S3_UPLOADER_HISTORY_BUCKET` | string | STG / PRD | LOCAL/DEV default: `HISTORY_BUCKET` from [`core.constants`](../../core/constants.py). STG/PRD startup fails if missing. |
| `S3_UPLOADER_HISTORY_PREFIX` | string | STG / PRD | LOCAL/DEV default: `HISTORY_PREFIX` from [`core.constants`](../../core/constants.py). STG/PRD startup fails if missing. |

### Should be set (has a default, tune per Glue capacity / queue depth)

| Env var | Default | Notes |
| --- | --- | --- |
| `S3_UPLOADER_ENVIRONMENT` | `LOCAL` | Drives the STG/PRD gate on history bucket/prefix. Set explicitly on every dispatcher deploy. |
| `S3_UPLOADER_MAX_CONCURRENT_GLUE` | `5` | Cap on simultaneous in-flight Glue runs; matches your Glue account quota. |
| `S3_UPLOADER_MAX_TRACKED_MUTATIONS` | `50` | Ceiling on messages the dispatcher holds visibility on concurrently. |
| `S3_UPLOADER_MUTATION_VISIBILITY_SECONDS` | `120` | Initial SQS visibility timeout per message. |
| `S3_UPLOADER_MUTATION_VISIBILITY_RENEWAL_SECONDS` | `30` | Interval at which the dispatcher extends visibility on tracked messages. |
| `S3_UPLOADER_MUTATION_POLL_SECONDS` | `10` | Sleep between `tick()` cycles. |

---

## Which app reads which var

The dispatcher and worker each carry a smaller subset than the API.
This cross-reference is here so an operator can size the container
env for a specific role without back-referring to source.

| Env var | API `Settings` | `WorkerSettings` | `MutationDispatcherSettings` |
| --- | :---: | :---: | :---: |
| `AWS_REGION` | ✓ | ✓ | ✓ |
| `S3_UPLOADER_ENVIRONMENT` | ✓ | — | ✓ |
| `S3_UPLOADER_LANDING_BUCKET` | ✓ | ✓ | ✓ |
| `S3_UPLOADER_LANDING_PREFIX` | ✓ | ✓ | ✓ |
| `S3_UPLOADER_GLUE_JOB_NAME` | ✓ | ✓ | ✓ |
| `S3_UPLOADER_CONTRACT_BUCKET` | ✓ | ✓ | — |
| `S3_UPLOADER_CONTRACT_PREFIX` | ✓ | ✓ | — |
| `S3_UPLOADER_BASE_QUEUE_URL` | ✓ | — | — |
| `S3_UPLOADER_LARGE_QUEUE_URL` | ✓ | — | — |
| `S3_UPLOADER_MUTATION_QUEUE_URL` | ✓ (producer) | — | ✓ (consumer) |
| `S3_UPLOADER_HISTORY_BUCKET` | ✓ | — | ✓ |
| `S3_UPLOADER_HISTORY_PREFIX` | ✓ | — | ✓ |
| `S3_UPLOADER_SKILL_BUNDLE_BUCKET` | ✓ | — | — |
| `S3_UPLOADER_SKILL_BUNDLE_PREFIX` | ✓ | — | — |
| `S3_UPLOADER_LOGIN_PASSWORD` | ✓ | — | — |
| `S3_UPLOADER_LOGIN_SECRET` | ✓ | — | — |
| `S3_UPLOADER_COOKIE_SECURE` | ✓ | — | — |
| `S3_UPLOADER_SERVE_LOCAL_FRONTEND` | ✓ | — | — |
| `S3_UPLOADER_SESSION_TTL_SECONDS` | ✓ | — | — |
| `S3_UPLOADER_BEARER_SECRET_ARN` | ✓ | — | — |
| `S3_UPLOADER_BEARER_CACHE_TTL_SECONDS` | ✓ | — | — |
| `S3_UPLOADER_BEARER_REFRESH_MIN_INTERVAL_SECONDS` | ✓ | — | — |
| `S3_UPLOADER_RAW_RETENTION_DAYS` | ✓ | — | — |
| `S3_UPLOADER_LOG_LEVEL` | ✓ | — | — |
| `S3_UPLOADER_ASYNC_THREAD_LIMIT` | ✓ | — | — |
| `S3_UPLOADER_API_BASE_URL` | ✓ | — | — |
| `S3_UPLOADER_ENCRYPTION_SECRET_ARN` | — | ✓ | — |
| `S3_UPLOADER_MAX_CONCURRENT_GLUE` | — | — | ✓ |
| `S3_UPLOADER_MAX_TRACKED_MUTATIONS` | — | — | ✓ |
| `S3_UPLOADER_MUTATION_VISIBILITY_SECONDS` | — | — | ✓ |
| `S3_UPLOADER_MUTATION_VISIBILITY_RENEWAL_SECONDS` | — | — | ✓ |
| `S3_UPLOADER_MUTATION_POLL_SECONDS` | — | — | ✓ |
| `S3_UPLOADER_JOB_ID` | — | ✓ (per-invocation, not on `WorkerSettings`) | — |
| `UVICORN_HOST` / `UVICORN_PORT` / `UVICORN_WORKERS` | ✓ (via `scripts/start_api.py`) | — | — |

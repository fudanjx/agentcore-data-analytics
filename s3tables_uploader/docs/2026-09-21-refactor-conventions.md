# S3 Tables Uploader — API Conventions

Long-lived reference for engineers working on the API side of
`s3tables_uploader/`. Complements the execution plan in
[`2026-09-18-refactor-plan.md`](2026-09-18-refactor-plan.md); this document
describes the **final state** and the patterns to follow going forward.

---

## 1. Folder layout at a glance

```
s3tables_uploader/
  entrypoint.py                    # 3-line ASGI shim (uvicorn imports :app)
  config.py                        # Settings + Environment enum

  api/                             # HTTP surface only — no business logic
    v3/                            # Versioned JSON API
      buckets.py                   # /api/v3/buckets, /namespaces, /tables
      identity.py, dev.py          # Frontend-only (env-gated)
      upload_history.py, mutations.py
      skills.py, jobs.py
      upload_sessions.py, worker_leases.py
      _common.py, _compat*.py      # Shared route helpers (leading underscore)
    static/                        # Non-API surface: /, /login, /static/{asset}
      frontend.py

  app/                             # FastAPI wiring — the ASSEMBLY layer
    factory.py                     # create_app(settings) -> FastAPI
    lifespan.py                    # AppState + boto3/store/bearer singletons
    dependencies.py                # SettingsDep, UserDep, service factories
    middlewares.py                 # CorrelationIdMiddleware, FrontendCookieGate
    exception_handlers.py          # UploaderError/ControlPlaneError -> JSON

  core/                            # Cross-cutting primitives, no domain
    constants.py                   # SSE mode, TTLs, tag set, path constants
    exceptions.py                  # UploaderError hierarchy
    logger.py                      # create_logger + create_structured_logger

  protocols/                       # Structural typing — extension points
    storage.py, secret_source.py, audit_reader.py, auth.py

  services/                        # Business logic — one class per file
    auth/{bearer,cookie}.py
    storage/{s3,local}.py
    tables.py, contracts.py, leases.py, mutations.py, audit.py
    profiles.py, secret_manager.py, skills.py

  models/                          # Persisted domain records
    destination.py, session.py, job.py, mutation.py, event.py

  utils/                           # Stateless helpers
    api_prefix.py, casing.py, hashing.py, time.py, upload_ids.py, models.py

  scripts/                         # Operator tools (not imported at runtime)
    start_api.py                   # Uvicorn launcher (Dockerfile CMD)
    migrate_legacy_audit.py        # One-shot: legacy audit -> canonical prefix
```

### The four architectural layers

1. **`api/`** — HTTP shape only. A router file decodes the request, calls a
   service, returns the response. **Never** put boto3 calls, retry logic or
   business rules here.
2. **`app/`** — FastAPI assembly. Anything that manipulates the `FastAPI`
   instance itself: middlewares, exception handlers, dependency wiring,
   lifespan.
3. **`services/`** — Domain logic. Every service is a class that takes its
   collaborators via constructor injection and is dependency-injected into
   routes.
4. **`core/`**, **`protocols/`**, **`models/`**, **`utils/`** — Reusable
   building blocks with **no dependency** on `api/` or `app/`. Anything in
   these folders must be importable from anywhere else.

Dependency direction is **strictly top-down**: `api` → `app` → `services` →
`{core, protocols, models, utils}`. `services` can import `protocols` and
`models`; nothing lower may import from a higher layer.

---

## 2. Environment matrix

`Settings.environment` is one of `LOCAL`, `DEV`, `STG`, `PRD`. Consumers
**must** read behaviour through computed properties on `Settings`, never
branch on the enum directly.

| Feature                              | LOCAL frontend | LOCAL API-only | DEV | STG | PRD |
|--------------------------------------|:--------------:|:--------------:|:---:|:---:|:---:|
| Static + `/` + cookie login          | ✓              | ✗              | ✓   | ✗   | ✗   |
| `/api/v3/dev/identity-profiles`      | ✓              | ✗              | ✓   | ✗   | ✗   |
| `/api/v3/identity`                   | ✓              | ✗              | ✓   | ✗   | ✗   |
| `docs_url` / `redoc_url`             | ✓              | ✓              | ✗   | ✗   | ✗   |
| Bearer auth accepted                 | ✓              | ✓              | ✓   | ✓   | ✓   |
| Cookie auth accepted                 | ✓              | ✗              | ✓   | ✗   | ✗   |
| Debug log level                      | ✓              | ✓              | ✓   | ✗   | ✗   |
| Uvicorn `--no-access-log`            | ✗              | ✗              | ✗   | ✓   | ✓   |

Read the flag, not the enum:

```python
def handler(settings: SettingsDep) -> ...:
    if settings.frontend_surface_enabled:   # not `settings.environment == LOCAL`
        ...
```

### Configuration env vars

The excerpt below covers the most commonly touched knobs on the API
`Settings`. It is intentionally short — the full authoritative list
(covering `Settings`, `WorkerSettings`, and `MutationDispatcherSettings`,
with MUST-set / SHOULD-set semantics and per-env validation) lives in
[`architecture/environment-variables.md`](architecture/environment-variables.md).

| Env var                                          | Required in            | Default (LOCAL/DEV)                                     |
|--------------------------------------------------|------------------------|---------------------------------------------------------|
| `S3_UPLOADER_ENVIRONMENT`                        | always (default LOCAL) | `LOCAL`                                                 |
| `S3_UPLOADER_SERVE_LOCAL_FRONTEND`               | LOCAL only             | `true`                                                  |
| `S3_UPLOADER_BEARER_SECRET_ARN`                  | **always** — bearer auth is accepted in every env | —                                                       |
| `S3_UPLOADER_BEARER_CACHE_TTL_SECONDS`           | any                    | `3600`                                                  |
| `S3_UPLOADER_BEARER_REFRESH_MIN_INTERVAL_SECONDS`| any                    | `300`                                                   |
| `S3_UPLOADER_HISTORY_BUCKET`                     | STG/PRD                | `ah-data-analytics`                                     |
| `S3_UPLOADER_HISTORY_PREFIX`                     | STG/PRD                | `temp_s3_update/web_ingest/upload_history`              |
| `S3_UPLOADER_LOG_LEVEL`                          | STG/PRD                | `INFO`                                                  |
| `S3_UPLOADER_ASYNC_THREAD_LIMIT`                 | any                    | `100` (1..1000; sizes anyio + boto3 pool caps)          |

---

## 3. Auth model

Bearer auth is accepted in **every environment**. Cookie auth is a
second, opt-in path available only where the temporary frontend is
served (LOCAL with `S3_UPLOADER_SERVE_LOCAL_FRONTEND=true`, and DEV).
The identity header follows the resolved auth path, not the
environment.

| Environment      | Bearer accepted | Cookie accepted | Identity header                                          |
|------------------|:---------------:|:---------------:|----------------------------------------------------------|
| LOCAL (frontend) | ✓               | ✓               | bearer → `User-ID` (email); cookie → `X-Pilot-User-Id` (profile key) |
| LOCAL (API-only) | ✓               | ✗               | `User-ID` (email)                                        |
| DEV              | ✓               | ✓               | bearer → `User-ID` (email); cookie → `X-Pilot-User-Id` (profile key) |
| STG / PRD        | ✓               | ✗               | `User-ID` (email)                                        |

`require_auth` ([`app/dependencies.py`](../app/dependencies.py))
selects the flow: if the request carries an `Authorization` header it
must be a valid `Bearer <token>` (no silent fallback to cookie);
otherwise, if the frontend surface is served, the cookie flow is
taken; else the request is rejected outright.
`FrontendCookieGate` short-circuits when an `Authorization` header is
present so the two flows never both run on the same request.
**When both are possible, bearer wins if the header is present.**

- **Bearer secret** is stored in AWS Secrets Manager; `BearerAuthService`
  caches it in-process for `bearer_cache_ttl_seconds` (default 1 hour).
- On a token mismatch, the service opportunistically re-fetches once per
  `bearer_refresh_min_interval_seconds` (default 5 min) — catches Secrets
  Manager rotations faster than TTL without giving attackers a DoS vector.
- **Ownership checks** are enforced in every environment via
  `enforce_ownership(record_owner_id, user)`.
- **Permission checks** (`is_admin`, `can_view_upload_history`,
  `can_rollback_uploads`) run only when the request resolved via the
  cookie flow; on the bearer flow the values are hard-coded to `True`
  because the calling application manages permissions externally.

---

## 4. Dependency conventions

### 4.1 The `Annotated[X, Depends(f)]` alias pattern

Every dependency exposes an `Annotated` alias so routes never write
`Depends(...)` inline:

```python
# app/dependencies.py
def get_lease_service(store: StoreDep, sqs: SqsDep, settings: SettingsDep) -> LeaseService:
    return LeaseService(store, sqs, settings)

LeaseServiceDep = Annotated[LeaseService, Depends(get_lease_service)]

# api/v3/worker_leases.py
def create_lease(
    payload: CreateWorkerLeaseRequest,
    user: UserDep,
    leases: LeaseServiceDep,
) -> dict[str, object]:
    ...
```

### 4.2 Two dependency layers

| Kind of thing                            | Where it lives          | Why                                              |
|------------------------------------------|-------------------------|--------------------------------------------------|
| Expensive singletons (boto3, `S3JobStore`, `BearerAuthService`) | Lifespan-yielded state on `request.state` | Reused across requests; bearer cache would be defeated otherwise |
| Thin stateless services (`LeaseService`, `TableBucketService`, …) | Per-request dependency factory | Cheap to build; keeps overrides simple in tests  |

Never store per-request state on `app.state`; **use only lifespan state
via `request.state`**. Tests must enter the TestClient context manager
(`self.enterContext(TestClient(app))`) so the lifespan fires.

### 4.3 Router-level auth

Every core router — and the frontend `identity` / `dev` routers when
they are registered — picks up `Depends(require_auth)` at
include-time in the factory, unconditionally:

```python
# app/factory.py
core_deps = [Depends(require_auth)]
for router in (buckets.router, upload_history.router, ...):
    app.include_router(router, dependencies=core_deps)
```

`require_auth` dispatches per request to bearer or cookie based on
whether an `Authorization` header is present (see §3). Individual
routes stay auth-agnostic. This means **no route file should ever
import `require_auth` directly** — inclusion-time wiring in
[`app/factory.py`](../app/factory.py) is the only place it lands.
The static `frontend` router (`/login`, `/`, `/static/{asset}`) is
intentionally the sole router without `require_auth`, because it
serves the login form and unauthenticated assets.

---

## 5. `get_api_prefix` convention

Every router computes its URL prefix from its file path:

```python
router = APIRouter(prefix=get_api_prefix(Path(__file__), "api"))
```

Rules the helper enforces automatically:
- Folder path becomes URL path — `api/v3/upload_sessions.py` →
  `/api/v3/upload-sessions`.
- **Underscores in filenames become hyphens in URLs** (Python module
  syntax requires underscores; REST URLs are idiomatically hyphenated).

Nested REST resources go in **one file** with `""` and `/child` route
paths, not two sibling files. See [`api/v3/buckets.py`](../api/v3/buckets.py):
one file, one router, three resource groups (`/buckets`,
`/buckets/namespaces`, `/buckets/tables`).

Do **not** put routes in `__init__.py`. Package-init modules stay empty
(just the docstring). This keeps the convention uniform and grep-friendly.

---

## 6. Logging conventions

Two entry points in [`core/logger.py`](../core/logger.py):

```python
logger = create_logger(__name__)                    # plain stdlib
logger = create_structured_logger(__name__)         # structlog + JSON
```

- Every request carries a correlation ID injected by
  `asgi_correlation_id.CorrelationIdMiddleware`. The structured logger's
  `_add_request_id` processor stamps it onto every event automatically.
- Values are safe metadata only. **Never log request bodies, raw filenames,
  cookie values, bearer tokens, or PHI/PII** — the whole logging design
  assumes logs can be shipped off-box.
- For errors that must reach the client, call `safe_error(logger,
  message, **fields)` and return the opaque `err-…` correlation id.

Env: LOCAL/DEV log level defaults to DEBUG; STG/PRD default INFO. Uvicorn
access logs run in LOCAL/DEV, disabled in STG/PRD (upstream ALB records
requests).

---

## 7. Exception handling

Handlers **raise typed exceptions**; the global handler translates them to
JSON. Never construct `HTTPException` in service code, and reach for it in
router code only when the response shape must exactly match a preserved
legacy contract.

```python
# services/tables.py
raise TableBucketForbidden("TABLE_BUCKET_FORBIDDEN")

# app/exception_handlers.py maps every UploaderError subclass:
{"code": "TABLE_BUCKET_FORBIDDEN", "detail": "TABLE_BUCKET_FORBIDDEN"}
# HTTP status 403 (from the exception class).
```

Full hierarchy in [`core/exceptions.py`](../core/exceptions.py).

---

## 8. SSE policy

Every S3 write in `s3tables_uploader/` uses `ServerSideEncryption="AES256"`
via [`core.constants.S3_SSE`](../core/constants.py). No `aws:kms` anywhere;
the constant is the single source of truth. If you need a different SSE
mode for a specific write in the future, update the constant or introduce
a per-call override — do not inline the string.

---

## 9. Bucket tagging

Every bucket the API creates is tagged with
[`APP_TAGS`](../core/constants.py):

```python
APP_TAGS = {
    "PROJECT-NAME": "Bot-NUHS",
    "PROJECT-NAME-SHORT": "Bot-NUHS",
    "APP": "Data-Insights",
}
```

- `TableBucketService.create_bucket(name)` creates the bucket then applies
  the tags. If tagging fails, the bucket is deleted (rollback) — no
  half-tagged buckets end up in the account.
- `TableBucketService.list_buckets()` filters out buckets whose `APP` tag
  is not `Data-Insights`, so listings never leak buckets from other apps
  in the same account.
- The service maintains a per-process 6-hour cache
  (`BUCKET_TAG_CACHE_TTL_SECONDS`) keyed by ARN. Cache is primed on
  create.
- `POST /api/v3/buckets/cache/purge` (admin) clears the cache on demand
  for the rare case an ARN was re-tagged in the AWS console.

To add a new tag or change the app filter, edit `core/constants.py`;
every service reads from that constant.

---

## 10. Audit read

- **Canonical layout**:
  `<history_bucket>/<history_prefix>/<scope>/<table>/<upload_id>.json`
  where `scope = sha256(f"{arn}|{namespace}")[:16]`.
- `ScopedS3AuditReader` reads only the scoped prefix — one paginated list
  per request, one `get_object` per matching entry.
- Legacy fallbacks (`<landing_bucket>/<landing_prefix>/audit/`,
  `<landing_bucket>/s3-uploader-v2/audit/`) are **gone**. Any records
  still living there are invisible to the API.
- Run [`scripts/migrate_legacy_audit.py`](../scripts/migrate_legacy_audit.py)
  once per environment during deploy:
  ```bash
  python -m s3tables_uploader.scripts.migrate_legacy_audit \
      --landing-bucket <bucket> \
      --landing-prefix <prefix> \
      --history-bucket $S3_UPLOADER_HISTORY_BUCKET \
      --history-prefix $S3_UPLOADER_HISTORY_PREFIX \
      --dry-run    # inspect first, then rerun without --dry-run
  ```
  Idempotent — safe to run multiple times.

---

## 11. Adding a new route

Checklist:

1. **Pick a file.** New resource → new `api/v3/<resource>.py`. New route on
   an existing resource → same file. REST-nested route → put the child in
   the same file as its parent, use a route path like `/children`.
2. **Router setup**:
   ```python
   router = APIRouter(prefix=get_api_prefix(Path(__file__), "api"))
   ```
3. **Colocate request / response models** at the top of the file — one
   `BaseModel` per body. No shared `schemas.py`.
4. **Use dependency aliases** for user + services (`UserDep`,
   `LeaseServiceDep`, …). Never write `Depends(...)` inline in the route
   signature.
5. **Raise typed exceptions**, not `HTTPException`, from anything that
   isn't a legacy status-code contract.
6. **Register the router** in [`app/factory.py:_register_routers`](../app/factory.py).
   Core routers get `Depends(require_auth)` from the `core_deps` list
   automatically. Frontend-only routers go behind the
   `settings.frontend_surface_enabled` branch — the authenticated
   ones (`identity`, `dev`) also pick up `core_deps`; the public
   static `frontend` router stays without it because it serves the
   login form and unauthenticated assets.
7. **Write a test.** Use `self.enterContext(TestClient(create_app(...)))`
   so the lifespan fires.

---

## 12. Adding a new service

1. Create the class under `services/<name>.py`.
2. Constructor takes its collaborators (boto3 client, `Settings`, other
   services) — no hidden globals.
3. Raise typed exceptions from `core.exceptions` when appropriate.
4. Add a per-request factory in `app/dependencies.py`:
   ```python
   def get_x_service(store: StoreDep, settings: SettingsDep) -> XService:
       return XService(store, settings)
   XServiceDep = Annotated[XService, Depends(get_x_service)]
   ```
5. Never store service instances on `app.state` or `request.state`.
   Singletons that MUST be shared (with in-process state like caches) go
   in lifespan state instead — see `BearerAuthService`.

---

## 13. Adding a new `BaseStorage` / `SecretSource` / `AuthService` impl

The `protocols/` layer defines structural typing hooks for these three
extension points. To add a new implementation:

1. Create the class under `services/<layer>/<name>.py`.
2. Match the protocol's method signatures — Python's structural typing
   accepts any class that has the same public shape.
3. Wire it in `app/lifespan.py` (for singletons like `BearerAuthService`)
   or `app/dependencies.py` (for per-request instances). Env-driven
   selection lives in the factory or the dependency function — never
   inside a route handler.

Examples in-tree:
- `services/storage/{s3,local}.py` — S3 for prod, in-memory for tests.
- `services/secret_manager.py` — AWS Secrets Manager and an in-memory
  test double.
- `services/auth/{bearer,cookie}.py` — the two auth strategies.

---

## 14. Uvicorn startup

[`scripts/start_api.py`](../scripts/start_api.py) is the Dockerfile CMD.
It reads `S3_UPLOADER_ENVIRONMENT` and passes `--access-log` (LOCAL/DEV)
or `--no-access-log` (STG/PRD) to uvicorn. Also respects
`UVICORN_HOST`, `UVICORN_PORT`, `UVICORN_WORKERS`.

The runtime image is Azure Linux distroless — **there is no shell**, so a
`.sh` launcher would not run. The Python wrapper is functionally
equivalent to what a shell script would do and works on the distroless
base.

---

## 15. Testing conventions

- **`TestClient` must run inside its context manager** so the ASGI
  lifespan fires. In `unittest.TestCase.setUp`, use
  `self.enterContext(TestClient(create_app(...)))`.
- **Never use `app.state`** to smuggle data past the lifespan — this was
  removed intentionally.
- **Override dependencies**, not module state:
  `app.dependency_overrides[get_bearer_auth] = lambda: fake_service`.
- Service unit tests build their collaborators from fakes/stubs; no
  boto3 in unit tests. See `tests/test_services_*.py` for examples.
- Environment-matrix regression tests live in
  [`tests/test_env_gating.py`](../tests/test_env_gating.py) — extend it
  whenever a new route becomes env-conditional so a future refactor
  cannot accidentally leak frontend surface into hardened envs.

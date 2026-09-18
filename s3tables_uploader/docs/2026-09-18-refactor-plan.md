# S3 Tables Uploader — API Refactor Plan (2026-09-18)

Reference document for the refactor of `s3tables_uploader/api.py` and adjacent
API-side files. Owned by Dedric (Engineering Lead). This file is the **execution
plan**; a separate `docs/2026-09-18-refactor-conventions.md` will be authored in
Phase 8 to capture the conventions themselves for future engineers.

Workers, Glue job scripts, mutation dispatcher, sanitization, table locks,
ingest contract, local deduplication and shared job-store code are **out of
scope** except for a one-line SSE flip in Phase 6.

---

## 1. Design decisions (locked)

### 1.1 Environments

```python
class Environment(str, Enum):
    LOCAL = "LOCAL"   # default
    DEV   = "DEV"
    STG   = "STG"
    PRD   = "PRD"
```

`LOCAL` has a sub-mode toggle: `Settings.serve_local_frontend: bool = True`.

Behaviour matrix:

| Feature                              | LOCAL frontend | LOCAL API-only | DEV | STG | PRD |
|--------------------------------------|:--------------:|:--------------:|:---:|:---:|:---:|
| Static + `/` + cookie login          | ✓              | ✗              | ✓   | ✗   | ✗   |
| Hardcoded profiles + `/api/dev/*`    | ✓              | ✗              | ✓   | ✗   | ✗   |
| `docs_url` / `redoc_url` served      | ✓              | ✓              | ✗   | ✗   | ✗   |
| Bearer auth required                 | ✗              | ✓              | ✗   | ✓   | ✓   |
| Debug log level                      | ✓              | ✓              | ✓   | ✗   | ✗   |
| Uvicorn `--no-access-log`            | ✗              | ✗              | ✗   | ✓   | ✓   |
| Admin/permission (`is_admin`, `can_*`) checks | profile-driven | skipped | profile-driven | skipped | skipped |
| Ownership checks on records          | ✓              | ✓              | ✓   | ✓   | ✓   |
| Bucket listing scoped to profile     | ✓              | ✗ (all)        | ✓   | ✗ (all) | ✗ (all) |

`Settings` exposes computed properties so consumers never branch on the raw enum:

```python
@property
def frontend_surface_enabled(self) -> bool: ...   # static + cookie + profiles + /api/dev/*
@property
def bearer_auth_required(self) -> bool: ...       # STG/PRD or LOCAL-API-only
@property
def ownership_checks_enabled(self) -> bool: True  # always true; kept for symmetry
```

### 1.2 Auth model

| Environment      | Client auth              | Identity header       | Identity format |
|------------------|--------------------------|-----------------------|-----------------|
| LOCAL (frontend) | signed cookie            | `X-Pilot-User-Id`     | profile key     |
| DEV              | signed cookie            | `X-Pilot-User-Id`     | profile key     |
| LOCAL (API-only) | Bearer (Secrets Manager) | `User-ID`             | email           |
| STG              | Bearer (Secrets Manager) | `User-ID`             | email           |
| PRD              | Bearer (Secrets Manager) | `User-ID`             | email           |

- Bearer secret cached in-memory in `BearerAuthService` (singleton, lifespan state).
- Cache TTL: 3600 s (`bearer_cache_ttl_seconds`).
- Refresh-on-miss: on token mismatch, force-refetch from Secrets Manager if the last forced refresh was ≥ 300 s ago (`bearer_refresh_min_interval_seconds`). Protects rotation-in-flight while capping DoS surface.
- Identity resolution is dispatched at the request layer:
  - `resolve_frontend_user` for frontend-enabled modes.
  - `resolve_hardened_user` for hardened modes.
  - `resolve_user(settings)` picks between them; `UserDep` is the sole per-route dep.

`UserContext`:
```python
@dataclass(frozen=True)
class UserContext:
    user_id: str
    is_admin: bool                       # always True in hardened
    can_view_upload_history: bool        # always True in hardened
    can_rollback_uploads: bool           # always True in hardened
    visible_buckets: list[BucketGrant] | None   # None in hardened = "all"
```

Ownership checks are one-liners at ~10 sites, applied in every environment.

### 1.3 Bucket tagging

New constant tag set applied on every API-originated bucket create:

```python
APP_TAGS = {
    "PROJECT-NAME": "Bot-NUHS",
    "PROJECT-NAME-SHORT": "Bot-NUHS",
    "APP": "Data-Insights",
}
APP_TAG_FILTER_KEY = "APP"
APP_TAG_FILTER_VALUE = "Data-Insights"
BUCKET_TAG_CACHE_TTL_SECONDS = 6 * 60 * 60   # 6 hours
```

- **Create flow**: `create_table_bucket(name)` → `tag_resource(arn, APP_TAGS)`. Tag failure triggers rollback (`delete_table_bucket`) and returns 502. On success the new ARN is primed into the cache as matches_app=True.
- **List flow**: filter `list_table_buckets` result by tag membership. N+1 calls on cache miss (one `list_tags_for_resource` per bucket); per-process `dict[arn -> _CachedTag]` with 6-hour TTL. Positive and negative results both cached.
- **Migration**: existing untagged buckets that should be visible must be tagged out-of-band; ship `scripts/tag_existing_buckets.py` (one-shot, manual run) for this.
- **Cache purge endpoint**: `POST /api/v3/buckets/cache/purge` clears the per-process cache. Useful when a bucket is re-tagged in the AWS console and ops does not want to wait 6 h.

### 1.4 Server-side encryption

Standardise on `AES256` on every S3 write from the API path. Phase 6 sweeps:

- `api.py:1073` (`create_multipart_upload` in JSON session create) — `aws:kms` → `AES256`.
- `api.py:1123` (`create_multipart_upload` in multipart compat session) — `aws:kms` → `AES256`.
- `job_store.py:220` (`put_object` for job records) — `aws:kms` → `AES256`.
- `worker.py:420` (`upload_file` for prepared file) — `aws:kms` → `AES256`. **Worker touch = 1 line only.**
- `worker.py:445` (`put_object` for manifest) — `aws:kms` → `AES256`. **Worker touch = 1 line only.**

All other worker/glue/dispatcher/sanitization code stays untouched.

### 1.5 Audit read

- **AS-IS** (`api.py:_history_entries`, ~40 lines): fully paginates + `get_object`s **three** prefixes on every `/api/upload-history` and `/api/rollbacks`. Filter by `(table_bucket_arn, namespace, target_table)` runs **after** download. O(all-audits) per request.
- **TO-BE** (`ScopedS3AuditReader`): read only `_HISTORY_PREFIX/<scope=hash(arn|ns)>/<table>/`. Legacy fallbacks dropped. `scripts/migrate_legacy_audit.py` copies remaining records from the two legacy prefixes into the canonical prefix; commit but do not auto-run.

### 1.6 Miscellaneous

- Inline `_reconcile_glue_job` (no-op wrapper; replace call at `api.py:1259` with `store.get_status(job_id).status`).
- `create_compat_session`'s JSON-or-multipart branching stays on one URL; internal dispatcher picks a typed sub-handler.
- All previously-unversioned routes promoted to `/api/v3/*`.

---

## 2. Target folder layout

```
s3tables_uploader/
  entrypoint.py                    # 3 lines: app = create_app(Settings.from_environ())
  scripts/
    start_api.sh                   # env-conditional --access-log flag
    migrate_legacy_audit.py        # Phase 6, manual run
    tag_existing_buckets.py        # Phase 6, manual run

  api/
    v3/
      identity.py, buckets.py, upload_sessions.py, worker_leases.py,
      jobs.py, mutations.py, upload_history.py, skills.py
    static/                        # frontend-mode-only: /, /static/{asset}, /login, /logout, /api/dev/identity-profiles

  app/
    factory.py                     # create_app(settings) -> FastAPI
    lifespan.py                    # TypedDict AppState; boto3 clients + store + BearerAuthService
    dependencies.py                # UserDep, SettingsDep, StoreDep, ServiceDeps, require_bearer, enforce_ownership
    middlewares.py                 # CorrelationIdMiddleware + DEV cookie gate
    exception_handlers.py          # global ClientError/ControlPlaneError/LoginRequired mappers

  core/
    constants.py                   # _HISTORY_*, _UPLOAD_*, _LEASE_*, SECRET_REFRESH_SECONDS, S3_SSE,
                                   # APP_TAGS, BUCKET_TAG_CACHE_TTL_SECONDS, header names
    logger.py                      # create_logger, create_structured_logger, safe_error, new_error_id,
                                   #   _add_request_id
    exceptions.py                  # LoginRequired, TableBucketForbidden, ControlPlaneError, etc.

  protocols/
    storage.py                     # BaseStorage
    secret_source.py               # SecretSource
    audit_reader.py                # AuditReader
    auth.py                        # AuthService

  services/
    auth/
      bearer.py                    # BearerAuthService (hardened) — SINGLETON, lives in lifespan state
      cookie.py                    # CookieAuthService (frontend modes) — absorbs auth.py
    storage/{s3.py, local.py}      # BaseStorage implementations
    audit.py                       # ScopedS3AuditReader
    profiles.py                    # LocalIdentityProfileService (frontend-mode only)
    tables.py                      # TableBucketService (owns 6-h tag cache)
    contracts.py                   # ContractService
    leases.py                      # LeaseService
    mutations.py                   # MutationEnqueuerService
    secret_manager.py              # SecretsManagerSource
    skills.py                      # API-facing skill logic extracted from skill_bundle.py

  models/                          # split from models.py; __init__ re-exports for back-compat

  utils/
    api_prefix.py                  # get_api_prefix(path, stop_folder_name) — verbatim from user file
    casing.py                      # pascal_to_snake — verbatim
    models.py                      # apply_func_to_model_attr_type — verbatim
    time.py, hashing.py, upload_ids.py

  config.py                        # Settings + Environment + all new fields + computed properties
  requirements.txt                 # + structlog, asgi-correlation-id
  Dockerfile.api                   # CMD → scripts/start_api.sh

  # UNTOUCHED (worker / glue / dispatcher / data-plane / shared locks):
  worker.py, worker_analysis.py, worker_routing.py,
  glue_job.py, mutation_dispatcher.py,
  sanitization.py, ingest_contract.py, local_deduplication.py, contract.py,
  job_store.py (except SSE line 220), table_lock.py, Dockerfile.worker

  # FOLDED (thin shims kept only if tests reference them):
  auth.py       -> services/auth/cookie.py
  observability.py -> core/logger.py
  skill_bundle.py (validation stays) -> services/skills.py

  docs/                            # this file + Phase-8 conventions doc
  static/                          # unchanged
  tests/                           # updated per phase
```

---

## 3. Runtime patterns

### 3.1 Lifespan state (singletons only)

Reserved for objects that MUST be shared across requests: boto3 clients, `S3JobStore`, `BearerAuthService` (owns the secret cache — creating a new instance per request would defeat the cache entirely).

```python
class AppState(TypedDict):
    settings: Settings
    s3: Any
    sqs: Any
    glue: Any
    s3tables: Any
    store: S3JobStore
    bearer_auth: BearerAuthService

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[AppState]:
    configure_logging()
    settings = Settings.from_environ()
    s3, sqs, glue, s3tables = _boto_clients(settings)
    store = S3JobStore(s3, settings.landing_bucket, settings.landing_prefix)
    bearer_auth = BearerAuthService(...)
    if settings.bearer_auth_required:
        bearer_auth.warm()
    yield {
        "settings": settings, "s3": s3, "sqs": sqs, "glue": glue,
        "s3tables": s3tables, "store": store, "bearer_auth": bearer_auth,
    }
```

State is accessed on requests via `request.state.<key>`. **No `app.state`.**

### 3.2 Dependencies (thin services built per-request)

Stateless services are constructed per request via `Depends`, reading singletons from state:

```python
def get_settings(request: Request) -> Settings: return request.state.settings
SettingsDep = Annotated[Settings, Depends(get_settings)]

def get_store(request: Request) -> S3JobStore: return request.state.store
StoreDep = Annotated[S3JobStore, Depends(get_store)]

def get_lease_service(store: StoreDep, sqs: SqsDep, settings: SettingsDep) -> LeaseService:
    return LeaseService(store, sqs, settings)
LeaseServiceDep = Annotated[LeaseService, Depends(get_lease_service)]
```

Rationale: singletons in state avoid boto3 reconstruction and preserve the bearer cache; thin services in dependencies keep coupling low and make `app.dependency_overrides` clean for tests.

### 3.3 Router-level bearer auth

```python
router = APIRouter(
    prefix=get_api_prefix(Path(__file__), "api"),
    dependencies=[Depends(require_bearer)] if bearer_required else [],
)
```

`require_bearer` raises 401 on missing/mismatched `Authorization: Bearer <secret>` header. `UserDep` is a separate per-route dep that reads the identity header (`User-ID` in hardened, `X-Pilot-User-Id` in frontend modes).

### 3.4 Ownership check helper

```python
def enforce_ownership(record_owner_id: str, user: UserContext) -> None:
    if record_owner_id != user.user_id:
        raise HTTPException(403, "FORBIDDEN")
```

Called at every site that today does `if record.owner_user_id != user_id: raise 403`.

---

## 4. Phase list (also mirrored as tasks)

| # | Phase | Summary |
|---|-------|---------|
| 1 | Foundation | deps, `core/constants.py`, `core/logger.py`, `core/exceptions.py`, `utils/*`, `Settings` extensions, `scripts/start_api.sh`, `Dockerfile.api` update. No behavior change. |
| 2 | Protocols + services | `protocols/*`, `services/*` (auth, storage, tables, contracts, leases, mutations, audit, profiles, secret_manager, skills). `api.py` delegates. |
| 3 | App wiring | `app/factory.py`, `app/lifespan.py`, `app/dependencies.py`, `app/middlewares.py`, `app/exception_handlers.py`. `entrypoint.py` → 3 lines. `enforce_ownership` introduced. |
| 4 | Router split | Endpoints move into `api/v3/*.py`. `UserDep` conventions. Form handlers use FastAPI-native params. All previously-unversioned routes promoted. |
| 5 | DEV-only static gating | Static + cookie login + `/api/dev/*` registered only when `frontend_surface_enabled`. `docs_url`/`redoc_url` gated on LOCAL. |
| 6 | Immediate changes | SSE→AES256 sweep; inline `_reconcile_glue_job`; audit fallback drop + migration script; bearer enforcement on hardened routers; cache-purge endpoint. |
| 7 | Model split | `models.py` → `models/{destination,session,job,mutation,event}.py` with back-compat re-exports. |
| 8 | Documentation | Author `docs/2026-09-18-refactor-conventions.md` covering everything above for future engineers. |
| 9 | Cleanup | Delete `api.py`, `auth.py`, `observability.py` (or shim). Full test-suite run. |

Each phase stops for your review + your commit. Nothing auto-commits.

---

## 5. Open items / follow-ups

- If ops confirms bucket count is small (< 10) at deploy time, the 6-hour cache stays; otherwise consider migrating to Resource Groups Tagging API (`get_resources` with `TagFilters`), which needs one extra IAM statement (`tag:GetResources`).
- Bearer damper default (300 s) is `bearer_refresh_min_interval_seconds`; tune per env if rotation cadence changes.
- Legacy audit records: run `scripts/migrate_legacy_audit.py` once during Phase 6 deploy; verify canonical prefix contains everything before dropping the fallback code path.
- `X-Pilot-User-Id` header is retained only for LOCAL-frontend and DEV; long-term the codebase should converge on `User-ID` once the temporary frontend is retired.

---

## 6. Files that MUST NOT change (safety fence)

- `worker.py` — except lines 420 and 445 (SSE flip only).
- `worker_analysis.py`, `worker_routing.py`
- `glue_job.py`
- `mutation_dispatcher.py`
- `sanitization.py`, `ingest_contract.py`, `local_deduplication.py`, `contract.py`
- `table_lock.py`
- `Dockerfile.worker`

If any refactoring appears to require touching these outside of the SSE flip, stop and confirm with Dedric first.

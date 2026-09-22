"""Explicit runtime configuration for the production uploader."""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum


class Environment(StrEnum):
    """Deployment environments recognised by the API service.

    Behaviour flags (frontend surface, docs, bearer auth, log level, uvicorn
    access log) are derived from this value through computed properties on
    :class:`Settings` — consumers must never branch on the raw enum.
    """

    LOCAL = "LOCAL"
    DEV = "DEV"
    STG = "STG"
    PRD = "PRD"


_DEBUG_ENVIRONMENTS = frozenset({Environment.LOCAL, Environment.DEV})
_HARDENED_ENVIRONMENTS = frozenset({Environment.STG, Environment.PRD})


class ConfigurationError(ValueError):
    """Raised when a required deployment setting is absent or unsafe."""


def _required(name: str, environ: dict[str, str]) -> str:
    value = environ.get(name, "").strip()
    if not value:
        raise ConfigurationError(f"{name} is required")
    return value


def _optional(name: str, environ: dict[str, str], default: str = "") -> str:
    return environ.get(name, default).strip()


def _boolean(name: str, environ: dict[str, str], default: bool) -> bool:
    value = environ.get(name)
    if value is None:
        return default
    if value.lower() in {"1", "true", "yes"}:
        return True
    if value.lower() in {"0", "false", "no"}:
        return False
    raise ConfigurationError(f"{name} must be true or false")


def _integer(name: str, environ: dict[str, str], default: int) -> int:
    raw = environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as error:
        raise ConfigurationError(f"{name} must be an integer") from error


def _environment(environ: dict[str, str]) -> Environment:
    raw = environ.get("S3_UPLOADER_ENVIRONMENT", Environment.LOCAL.value).strip().upper()
    try:
        return Environment(raw)
    except ValueError as error:
        allowed = ", ".join(item.value for item in Environment)
        raise ConfigurationError(
            f"S3_UPLOADER_ENVIRONMENT must be one of: {allowed}"
        ) from error


@dataclass(frozen=True)
class Settings:
    region: str
    landing_bucket: str
    landing_prefix: str
    base_worker_queue_url: str
    large_worker_queue_url: str
    mutation_queue_url: str
    login_password: str
    login_secret: str
    cookie_secure: bool
    session_ttl_seconds: int
    raw_retention_days: int
    glue_job_name: str
    contract_bucket: str
    contract_prefix: str
    environment: Environment = Environment.LOCAL
    log_level: str = "INFO"
    serve_local_frontend: bool = True
    bearer_secret_arn: str = ""
    bearer_cache_ttl_seconds: int = 3600
    bearer_refresh_min_interval_seconds: int = 300
    history_bucket: str = ""
    history_prefix: str = ""
    skill_bundle_bucket: str = ""
    skill_bundle_prefix: str = ""
    api_base_url: str = ""

    # ------------------------------------------------------------------
    # Computed properties — the ONLY way consumers should branch on env.
    # ------------------------------------------------------------------

    @property
    def frontend_surface_enabled(self) -> bool:
        """Whether the temporary frontend (static + cookie + profile switcher) is served."""
        if self.environment is Environment.DEV:
            return True
        if self.environment is Environment.LOCAL:
            return self.serve_local_frontend
        return False  # STG, PRD

    @property
    def docs_enabled(self) -> bool:
        """Whether FastAPI's /docs, /redoc and /openapi.json endpoints are exposed."""
        return self.environment is Environment.LOCAL

    @property
    def debug_logging_enabled(self) -> bool:
        return self.environment in _DEBUG_ENVIRONMENTS

    @property
    def access_log_enabled(self) -> bool:
        """Whether uvicorn should emit request access logs."""
        return self.environment in _DEBUG_ENVIRONMENTS

    @classmethod
    def from_environ(cls, environ: dict[str, str] | None = None) -> Settings:
        env = dict(os.environ if environ is None else environ)
        environment = _environment(env)
        cookie_secure = _boolean("S3_UPLOADER_COOKIE_SECURE", env, True)
        # Cookies are only issued in envs where the temporary frontend is
        # served. LOCAL is typically HTTP so the developer keeps the choice;
        # STG/PRD do not serve cookies at all; DEV is a real HTTPS deploy
        # so cookie_secure=false there is almost certainly a misconfig.
        if environment is Environment.DEV and not cookie_secure:
            raise ConfigurationError(
                "S3_UPLOADER_COOKIE_SECURE must be true in DEV"
            )
        secret = _required("S3_UPLOADER_LOGIN_SECRET", env)
        if len(secret) < 32:
            raise ConfigurationError(
                "S3_UPLOADER_LOGIN_SECRET must be at least 32 characters"
            )
        raw_retention_days = _integer("S3_UPLOADER_RAW_RETENTION_DAYS", env, 1)
        if not 1 <= raw_retention_days <= 30:
            raise ConfigurationError(
                "S3_UPLOADER_RAW_RETENTION_DAYS must be 1 through 30"
            )
        serve_local_frontend = _boolean(
            "S3_UPLOADER_SERVE_LOCAL_FRONTEND", env, True
        )
        bearer_secret_arn = _required("S3_UPLOADER_BEARER_SECRET_ARN", env)

        # History bucket / prefix. LOCAL and DEV fall back to the well-known
        # values so developers can boot without extra config; STG and PRD
        # must set them explicitly so a staging deploy never accidentally
        # writes to the production history bucket.
        history_bucket = _optional("S3_UPLOADER_HISTORY_BUCKET", env)
        history_prefix = _optional("S3_UPLOADER_HISTORY_PREFIX", env)
        if environment in _HARDENED_ENVIRONMENTS:
            if not history_bucket:
                raise ConfigurationError(
                    "S3_UPLOADER_HISTORY_BUCKET is required in STG/PRD"
                )
            if not history_prefix:
                raise ConfigurationError(
                    "S3_UPLOADER_HISTORY_PREFIX is required in STG/PRD"
                )
        else:
            history_bucket = history_bucket or "ah-data-analytics"
            history_prefix = history_prefix or "temp_s3_update/web_ingest/upload_history"

        # Skill-bundle destination follows the same rule: default in
        # LOCAL/DEV so operators can boot without extra config; hardened
        # envs must set the destination explicitly so uploaded skills
        # never land in the DEV bucket by accident.
        skill_bundle_bucket = _optional("S3_UPLOADER_SKILL_BUNDLE_BUCKET", env)
        skill_bundle_prefix = _optional("S3_UPLOADER_SKILL_BUNDLE_PREFIX", env)
        if environment in _HARDENED_ENVIRONMENTS:
            if not skill_bundle_bucket:
                raise ConfigurationError(
                    "S3_UPLOADER_SKILL_BUNDLE_BUCKET is required in STG/PRD"
                )
            if not skill_bundle_prefix:
                raise ConfigurationError(
                    "S3_UPLOADER_SKILL_BUNDLE_PREFIX is required in STG/PRD"
                )
        else:
            skill_bundle_bucket = skill_bundle_bucket or "agentcore-harness-dev"
            skill_bundle_prefix = skill_bundle_prefix or "skills"
        return cls(
            region=_required("AWS_REGION", env),
            landing_bucket=_required("S3_UPLOADER_LANDING_BUCKET", env),
            landing_prefix=_required("S3_UPLOADER_LANDING_PREFIX", env).strip("/"),
            base_worker_queue_url=_required("S3_UPLOADER_BASE_QUEUE_URL", env),
            large_worker_queue_url=_required("S3_UPLOADER_LARGE_QUEUE_URL", env),
            mutation_queue_url=_required("S3_UPLOADER_MUTATION_QUEUE_URL", env),
            login_password=_required("S3_UPLOADER_LOGIN_PASSWORD", env),
            login_secret=secret,
            cookie_secure=cookie_secure,
            session_ttl_seconds=_integer(
                "S3_UPLOADER_SESSION_TTL_SECONDS", env, 43200
            ),
            raw_retention_days=raw_retention_days,
            api_base_url=_optional("S3_UPLOADER_API_BASE_URL", env).rstrip("/"),
            glue_job_name=_required("S3_UPLOADER_GLUE_JOB_NAME", env),
            contract_bucket=_required("S3_UPLOADER_CONTRACT_BUCKET", env),
            contract_prefix=_required("S3_UPLOADER_CONTRACT_PREFIX", env).strip("/"),
            environment=environment,
            log_level=_optional("S3_UPLOADER_LOG_LEVEL", env, "INFO").upper(),
            serve_local_frontend=serve_local_frontend,
            bearer_secret_arn=bearer_secret_arn,
            bearer_cache_ttl_seconds=_integer(
                "S3_UPLOADER_BEARER_CACHE_TTL_SECONDS", env, 3600
            ),
            bearer_refresh_min_interval_seconds=_integer(
                "S3_UPLOADER_BEARER_REFRESH_MIN_INTERVAL_SECONDS", env, 300
            ),
            history_bucket=history_bucket,
            history_prefix=history_prefix.strip("/"),
            skill_bundle_bucket=skill_bundle_bucket,
            skill_bundle_prefix=skill_bundle_prefix.strip("/"),
        )


@dataclass(frozen=True)
class WorkerSettings:
    region: str
    landing_bucket: str
    landing_prefix: str
    glue_job_name: str
    contract_bucket: str
    contract_prefix: str
    encryption_secret_arn: str

    @classmethod
    def from_environ(cls, environ: dict[str, str] | None = None) -> WorkerSettings:
        env = dict(os.environ if environ is None else environ)
        return cls(
            region=_required("AWS_REGION", env),
            landing_bucket=_required("S3_UPLOADER_LANDING_BUCKET", env),
            landing_prefix=_required("S3_UPLOADER_LANDING_PREFIX", env).strip("/"),
            glue_job_name=_required("S3_UPLOADER_GLUE_JOB_NAME", env),
            contract_bucket=_required("S3_UPLOADER_CONTRACT_BUCKET", env),
            contract_prefix=_required("S3_UPLOADER_CONTRACT_PREFIX", env).strip("/"),
            encryption_secret_arn=_required("S3_UPLOADER_ENCRYPTION_SECRET_ARN", env),
        )

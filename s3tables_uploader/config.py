"""Explicit runtime configuration for the production uploader."""

from __future__ import annotations

import os
from dataclasses import dataclass


class ConfigurationError(ValueError):
    """Raised when a required deployment setting is absent or unsafe."""


def _required(name: str, environ: dict[str, str]) -> str:
    value = environ.get(name, "").strip()
    if not value:
        raise ConfigurationError(f"{name} is required")
    return value


def _boolean(name: str, environ: dict[str, str], default: bool) -> bool:
    value = environ.get(name)
    if value is None:
        return default
    if value.lower() in {"1", "true", "yes"}:
        return True
    if value.lower() in {"0", "false", "no"}:
        return False
    raise ConfigurationError(f"{name} must be true or false")


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
    api_base_url: str
    glue_job_name: str
    contract_bucket: str
    contract_prefix: str

    @classmethod
    def from_environ(cls, environ: dict[str, str] | None = None) -> Settings:
        env = dict(os.environ if environ is None else environ)
        environment = env.get("S3_UPLOADER_ENV", "production").lower()
        cookie_secure = _boolean("S3_UPLOADER_COOKIE_SECURE", env, True)
        if environment == "production" and not cookie_secure:
            raise ConfigurationError("S3_UPLOADER_COOKIE_SECURE must be true in production")
        secret = _required("S3_UPLOADER_LOGIN_SECRET", env)
        if len(secret) < 32:
            raise ConfigurationError("S3_UPLOADER_LOGIN_SECRET must be at least 32 characters")
        raw_retention_days = int(env.get("S3_UPLOADER_RAW_RETENTION_DAYS", "1"))
        if not 1 <= raw_retention_days <= 30:
            raise ConfigurationError("S3_UPLOADER_RAW_RETENTION_DAYS must be 1 through 30")
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
            session_ttl_seconds=int(env.get("S3_UPLOADER_SESSION_TTL_SECONDS", "43200")),
            raw_retention_days=raw_retention_days,
            api_base_url=_required("S3_UPLOADER_API_BASE_URL", env).rstrip("/"),
            glue_job_name=_required("S3_UPLOADER_GLUE_JOB_NAME", env),
            contract_bucket=_required("S3_UPLOADER_CONTRACT_BUCKET", env),
            contract_prefix=_required("S3_UPLOADER_CONTRACT_PREFIX", env).strip("/"),
        )


@dataclass(frozen=True)
class WorkerSettings:
    region: str
    landing_bucket: str
    landing_prefix: str
    glue_job_name: str
    contract_bucket: str
    contract_prefix: str

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
        )

"""Explicit runtime configuration for the v2 API and worker."""

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
    queue_url: str
    login_password: str
    login_secret: str
    cookie_secure: bool
    session_ttl_seconds: int
    raw_retention_days: int
    api_base_url: str

    @classmethod
    def from_environ(cls, environ: dict[str, str] | None = None) -> "Settings":
        env = dict(os.environ if environ is None else environ)
        environment = env.get("S3_UPLOADER_V2_ENV", "production").lower()
        cookie_secure = _boolean("S3_UPLOADER_V2_COOKIE_SECURE", env, True)
        if environment == "production" and not cookie_secure:
            raise ConfigurationError("S3_UPLOADER_V2_COOKIE_SECURE must be true in production")
        secret = _required("S3_UPLOADER_V2_LOGIN_SECRET", env)
        if len(secret) < 32:
            raise ConfigurationError("S3_UPLOADER_V2_LOGIN_SECRET must be at least 32 characters")
        raw_retention_days = int(env.get("S3_UPLOADER_V2_RAW_RETENTION_DAYS", "1"))
        if not 1 <= raw_retention_days <= 30:
            raise ConfigurationError("S3_UPLOADER_V2_RAW_RETENTION_DAYS must be 1 through 30")
        return cls(
            region=_required("AWS_REGION", env),
            landing_bucket=_required("S3_UPLOADER_V2_LANDING_BUCKET", env),
            landing_prefix=env.get("S3_UPLOADER_V2_LANDING_PREFIX", "s3-uploader-v2").strip("/"),
            queue_url=_required("S3_UPLOADER_V2_QUEUE_URL", env),
            login_password=_required("S3_UPLOADER_V2_LOGIN_PASSWORD", env),
            login_secret=secret,
            cookie_secure=cookie_secure,
            session_ttl_seconds=int(env.get("S3_UPLOADER_V2_SESSION_TTL_SECONDS", "43200")),
            raw_retention_days=raw_retention_days,
            api_base_url=_required("S3_UPLOADER_V2_API_BASE_URL", env).rstrip("/"),
        )

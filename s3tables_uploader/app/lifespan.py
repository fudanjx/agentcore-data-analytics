"""ASGI lifespan: build the singletons every request will read.

State is a ``TypedDict`` and is returned from the ``@asynccontextmanager`` so
FastAPI mounts it on ``request.state`` for every incoming request. Only
long-lived, expensive-to-construct objects live here: boto3 clients, the
``S3JobStore``, and ``BearerAuthService`` (which owns the Secrets Manager
cache and MUST be a singleton).

Thin services (``TableBucketService``, ``LeaseService``, …) are built
per-request in :mod:`app.dependencies` from these singletons — that keeps
coupling low and makes ``app.dependency_overrides`` clean for tests.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator
from typing import Any, TypedDict

import boto3
from fastapi import FastAPI

from ..config import Settings
from ..core.logger import configure_logging, create_structured_logger
from ..job_store import S3JobStore
from ..services.auth.bearer import BearerAuthService
from ..services.secret_manager import SecretsManagerSource


class AppState(TypedDict):
    """Long-lived singletons mounted on every ``request.state``."""

    settings: Settings
    s3: Any
    sqs: Any
    glue: Any
    s3tables: Any
    secrets_manager: Any
    store: S3JobStore
    bearer_auth: BearerAuthService


def _boto_clients(settings: Settings) -> dict[str, Any]:
    return {
        "s3": boto3.client("s3", region_name=settings.region),
        "sqs": boto3.client("sqs", region_name=settings.region),
        "glue": boto3.client("glue", region_name=settings.region),
        "s3tables": boto3.client("s3tables", region_name=settings.region),
        "secrets_manager": boto3.client("secretsmanager", region_name=settings.region),
    }


def build_state(
    settings: Settings,
    *,
    clients: dict[str, Any] | None = None,
    bearer_auth: BearerAuthService | None = None,
) -> AppState:
    """Assemble the app state without entering the lifespan context.

    Exposed for tests that need to construct the state dict inline instead
    of driving the ASGI lifecycle. Tests can pass a pre-built
    ``bearer_auth`` service to avoid a real Secrets Manager fetch.
    """
    boto_clients = clients if clients is not None else _boto_clients(settings)
    store = S3JobStore(
        boto_clients["s3"], settings.landing_bucket, settings.landing_prefix
    )
    if bearer_auth is None:
        bearer_auth = BearerAuthService(
            SecretsManagerSource(boto_clients["secrets_manager"]),
            settings.bearer_secret_arn,
            cache_ttl_seconds=settings.bearer_cache_ttl_seconds,
            refresh_min_interval_seconds=settings.bearer_refresh_min_interval_seconds,
        )
    return {
        "settings": settings,
        "s3": boto_clients["s3"],
        "sqs": boto_clients["sqs"],
        "glue": boto_clients["glue"],
        "s3tables": boto_clients["s3tables"],
        "secrets_manager": boto_clients["secrets_manager"],
        "store": store,
        "bearer_auth": bearer_auth,
    }


def make_lifespan(
    settings: Settings,
    *,
    clients: dict[str, Any] | None = None,
    bearer_auth: BearerAuthService | None = None,
):
    """Return an ``@asynccontextmanager`` bound to a specific ``Settings``.

    The factory pattern lets tests inject a settings override without going
    through the environment. ``clients`` and ``bearer_auth`` let tests
    supply fake dependencies.
    """

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[AppState]:
        configure_logging()
        logger = create_structured_logger("s3tables_uploader.lifespan")
        state = build_state(settings, clients=clients, bearer_auth=bearer_auth)
        state["bearer_auth"].warm()
        logger.info("bearer_auth_warmed", environment=settings.environment.value)
        logger.info(
            "lifespan_started",
            environment=settings.environment.value,
            frontend=settings.frontend_surface_enabled,
        )
        try:
            yield state
        finally:
            logger.info("lifespan_stopped")

    return lifespan

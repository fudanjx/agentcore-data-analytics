"""FastAPI application factory.

The factory is the ONLY place a ``FastAPI`` instance is constructed. It:

- Attaches the lifespan (which builds and yields the app state singletons).
- Installs middlewares (correlation id, and DEV cookie gate when enabled).
- Registers global exception handlers.
- Gates ``docs_url`` / ``redoc_url`` on ``settings.docs_enabled``.
- Registers routers, gating identity and dev routers on
    ``settings.frontend_surface_enabled``.
"""

from __future__ import annotations

from typing import Any

from fastapi import Depends, FastAPI

from ..config import Settings
from ..services.auth.bearer import BearerAuthService
from .dependencies import require_bearer
from .exception_handlers import install_exception_handlers
from .lifespan import make_lifespan
from .middlewares import install_middlewares


def create_app(
    settings: Settings,
    s3_client: Any | None = None,
    sqs_client: Any | None = None,
    s3tables_client: Any | None = None,
    glue_client: Any | None = None,
    *,
    lifespan_clients: dict[str, Any] | None = None,
    lifespan_bearer_auth: BearerAuthService | None = None,
) -> FastAPI:
    """Build a fully wired FastAPI app for the given settings.

    Positional ``s3_client``/``sqs_client``/``s3tables_client``/``glue_client``
    parameters accept fakes for tests without going through Secrets Manager.
    ``lifespan_clients`` and ``lifespan_bearer_auth`` are the keyword-only
    equivalents when the whole state needs to be assembled explicitly.
    """
    if lifespan_clients is None and any(
        client is not None for client in (s3_client, sqs_client, s3tables_client, glue_client)
    ):
        lifespan_clients = {
            "s3": s3_client,
            "sqs": sqs_client,
            "glue": glue_client,
            "s3tables": s3tables_client,
            "secrets_manager": None,
        }
    app = FastAPI(
        title="S3 Tables Uploader",
        version="3.1.0",
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url="/redoc" if settings.docs_enabled else None,
        lifespan=make_lifespan(
            settings, clients=lifespan_clients, bearer_auth=lifespan_bearer_auth
        ),
    )
    install_middlewares(app, settings)
    install_exception_handlers(app)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    _register_routers(app, settings)
    return app


def _register_routers(app: FastAPI, settings: Settings) -> None:
    """Attach every v3 router, gating environment-specific ones."""
    from ..api.v3 import (
        buckets,
        jobs,
        mutations,
        skills,
        upload_history,
        upload_sessions,
        worker_leases,
    )

    hardened_deps = [Depends(require_bearer)] if settings.bearer_auth_required else []

    # Core routers registered in every environment.
    for router in (
        buckets.router,
        upload_history.router,
        mutations.router,
        mutations.rollbacks_router,
        skills.router,
        upload_sessions.router,
        worker_leases.router,
        jobs.router,
        jobs.ingestions_router,
    ):
        app.include_router(router, dependencies=hardened_deps)

    # Frontend-only routers.
    if settings.frontend_surface_enabled:
        from ..api.static import frontend
        from ..api.v3 import dev, identity

        app.include_router(frontend.router)
        app.include_router(identity.router)
        app.include_router(dev.router)

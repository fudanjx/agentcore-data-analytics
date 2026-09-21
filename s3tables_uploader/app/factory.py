"""FastAPI application factory.

The factory is the ONLY place a ``FastAPI`` instance is constructed. It:

- Attaches the lifespan (which builds and yields the app state singletons).
- Installs middlewares (correlation id, and DEV cookie gate when enabled).
- Registers global exception handlers.
- Gates ``docs_url`` / ``redoc_url`` on ``settings.docs_enabled``.
- Registers routers.

Routers themselves land in Phase 4 under ``api/v3/*.py``; this factory
already exposes ``/healthz`` so the app is verifiable end-to-end today.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from ..config import Settings
from ..services.auth.bearer import BearerAuthService
from .exception_handlers import install_exception_handlers
from .lifespan import make_lifespan
from .middlewares import install_middlewares


def create_app(
    settings: Settings,
    *,
    lifespan_clients: dict[str, Any] | None = None,
    lifespan_bearer_auth: BearerAuthService | None = None,
) -> FastAPI:
    """Build a fully wired FastAPI app for the given settings.

    ``lifespan_clients`` and ``lifespan_bearer_auth`` let tests bypass real
    boto3 client construction and Secrets Manager fetches.
    """
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

    return app

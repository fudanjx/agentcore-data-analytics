"""ASGI middlewares.

- ``asgi_correlation_id.CorrelationIdMiddleware`` is always mounted; it stamps
    every request with an ``X-Request-ID`` and makes it available to the
    structured logger via ``core.logger._add_request_id``.

- ``FrontendCookieGate`` is only mounted when ``settings.frontend_surface_enabled``.
    It replaces the ``@app.middleware("http")`` inside the old ``api.py`` that
    redirected unauthenticated browser requests to ``/login`` and returned a
    JSON ``LOGIN_REQUIRED`` for API paths.

Bearer auth is enforced router-side via ``Depends(require_auth)`` on every
core router — not here. When a request presents an ``Authorization`` header
the cookie gate steps out of the way and lets ``require_auth`` validate it.
"""

from __future__ import annotations

from typing import Awaitable, Callable

from asgi_correlation_id import CorrelationIdMiddleware
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from ..config import Settings
from ..core.exceptions import LoginRequired
from ..services.auth.cookie import COOKIE_NAME, read_cookie


class FrontendCookieGate(BaseHTTPMiddleware):
    """Redirect unauthenticated browser requests; JSON 401 for API paths.

    Paths on ``EXEMPT`` never require a cookie (health check, login form,
    login submission). Everything else must present a valid signed cookie.
    """

    EXEMPT: frozenset[str] = frozenset(
        {"/login", "/healthz", "/docs", "/redoc", "/openapi.json"}
    )

    def __init__(self, app: ASGIApp, settings: Settings):
        super().__init__(app)
        self._settings = settings

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if request.url.path in self.EXEMPT:
            return await call_next(request)
        # A cookie-authed browser request never carries Authorization; if one
        # is present, this is a bearer client — let require_auth verify it.
        if request.headers.get("Authorization"):
            return await call_next(request)
        try:
            read_cookie(request.cookies.get(COOKIE_NAME), self._settings)
        except LoginRequired:
            if request.url.path.startswith("/api/"):
                return JSONResponse(
                    status_code=401,
                    content={
                        "code": "LOGIN_REQUIRED",
                        "detail": "Log in before using the uploader API.",
                    },
                )
            return RedirectResponse(url="/login", status_code=303)
        except HTTPException as exc:
            # Preserve any explicit HTTPException raised by future extensions
            # (e.g. rate limiting) without swallowing it.
            return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
        return await call_next(request)


def install_middlewares(app: FastAPI, settings: Settings) -> None:
    """Attach all conditional and unconditional middlewares to ``app``.

    Called from :func:`app.factory.create_app`.
    """
    # Correlation-id middleware runs FIRST (outermost) so the id is set
    # before any downstream middleware or handler logs.
    app.add_middleware(CorrelationIdMiddleware, header_name="X-Request-ID")
    if settings.frontend_surface_enabled:
        app.add_middleware(FrontendCookieGate, settings=settings)

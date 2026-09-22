"""Frontend-surface routes: index, static assets, cookie login/logout.

Registered only when ``settings.frontend_surface_enabled``. In hardened
modes the entire router is skipped, so a caller hitting ``/login`` in PRD
receives a 404 rather than a login form.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from fastapi import (
    APIRouter,
    Header,
    HTTPException,
    Path as PathParam,
    Request,
    Response,
)
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from ...app.dependencies import SettingsDep
from ...services.auth.cookie import COOKIE_NAME, login_cookie, valid_password


router = APIRouter()

_STATIC_ROOT = Path(__file__).parents[2] / "static"


class LoginRequest(BaseModel):
    password: str


@router.get("/")
def landing() -> FileResponse:
    return FileResponse(_STATIC_ROOT / "index.html", headers={"Cache-Control": "no-store"})


@router.get("/static/{asset}")
def static_asset(
    asset: Annotated[Literal["app.js", "style.css"], PathParam()],
) -> FileResponse:
    return FileResponse(_STATIC_ROOT / asset, headers={"Cache-Control": "no-store"})


@router.get("/login")
def login_form() -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><title>S3 Uploader login</title>"
        "<form method='post'>"
        "<label>Password <input name='password' type='password' autofocus></label>"
        "<button>Sign in</button>"
        "</form>"
    )


@router.post("/login")
async def login(
    request: Request,
    settings: SettingsDep,
    content_type: Annotated[str, Header()] = "",
) -> Response:
    is_json = content_type.startswith("application/json")
    if is_json:
        payload = LoginRequest.model_validate(await request.json())
    else:
        form = await request.form()
        payload = LoginRequest(password=str(form.get("password", "")))
    if not valid_password(payload.password, settings):
        if is_json:
            raise HTTPException(401, "LOGIN_FAILED")
        return HTMLResponse(
            "<!doctype html><p>Invalid password.</p><a href='/login'>Try again</a>",
            status_code=401,
        )
    response: Response = (
        JSONResponse({"authenticated": True})
        if is_json
        else RedirectResponse(url="/", status_code=303)
    )
    response.set_cookie(
        COOKIE_NAME,
        login_cookie(settings),
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        max_age=settings.session_ttl_seconds,
        path="/",
    )
    return response


@router.post("/logout")
def logout(accept: Annotated[str, Header()] = "") -> Response:
    response = (
        JSONResponse({"authenticated": False})
        if accept.startswith("application/json")
        else RedirectResponse(url="/login", status_code=303)
    )
    response.delete_cookie(COOKIE_NAME, path="/")
    return response

"""Minimal temporary password/cookie authentication retained from the pilot."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

from fastapi import HTTPException, Request
from .config import Settings

COOKIE_NAME = "s3_uploader_v2_session"


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _signature(value: str, settings: Settings) -> str:
    return _b64(hmac.new(settings.login_secret.encode("utf-8"), value.encode("ascii"), hashlib.sha256).digest())


def login_cookie(settings: Settings) -> str:
    payload = _b64(json.dumps({"user_id": "shared-operator", "issued_at": int(time.time())}, separators=(",", ":")).encode("utf-8"))
    return f"{payload}.{_signature(payload, settings)}"


def require_user(request: Request, settings: Settings) -> str:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(401, "LOGIN_REQUIRED")
    try:
        payload, signature = token.split(".", 1)
        if not hmac.compare_digest(signature, _signature(payload, settings)):
            raise ValueError("invalid signature")
        values = json.loads(_unb64(payload).decode("utf-8"))
        if int(time.time()) - int(values["issued_at"]) > settings.session_ttl_seconds:
            raise ValueError("expired")
        return str(values["user_id"])
    except (ValueError, KeyError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HTTPException(401, "LOGIN_REQUIRED") from error


def valid_password(candidate: str, settings: Settings) -> bool:
    return hmac.compare_digest(candidate, settings.login_password)

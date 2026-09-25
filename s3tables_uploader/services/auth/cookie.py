"""Cookie-based authentication for LOCAL/DEV frontend modes.

Ports the logic from the legacy ``auth.py`` verbatim; the split lets the
factory pick between cookie and bearer auth based on env, and keeps the two
implementations discoverable under ``services/auth/``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

from ...config import Settings
from ...core.exceptions import LoginRequired


COOKIE_NAME = "s3_uploader_session"


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _signature(value: str, settings: Settings) -> str:
    return _b64(
        hmac.new(
            settings.login_secret.encode("utf-8"),
            value.encode("ascii"),
            hashlib.sha256,
        ).digest()
    )


def login_cookie(settings: Settings) -> str:
    """Return the signed cookie value that grants an authenticated session."""
    payload = _b64(
        json.dumps(
            {"user_id": "shared-operator", "issued_at": int(time.time())},
            separators=(",", ":"),
        ).encode("utf-8")
    )
    return f"{payload}.{_signature(payload, settings)}"


def read_cookie(token: str | None, settings: Settings) -> str:
    """Return the ``user_id`` embedded in the cookie or raise ``LoginRequired``."""
    if not token:
        raise LoginRequired("Login is required")
    try:
        payload, signature = token.split(".", 1)
        if not hmac.compare_digest(signature, _signature(payload, settings)):
            raise ValueError("invalid signature")
        values = json.loads(_unb64(payload).decode("utf-8"))
        if int(time.time()) - int(values["issued_at"]) > settings.session_ttl_seconds:
            raise ValueError("expired")
        return str(values["user_id"])
    except (ValueError, KeyError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LoginRequired("Login is required") from error


def valid_password(candidate: str, settings: Settings) -> bool:
    """Constant-time comparison against the configured login password."""
    return hmac.compare_digest(candidate, settings.login_password)

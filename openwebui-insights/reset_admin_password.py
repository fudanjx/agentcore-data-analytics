#!/usr/bin/env python3
"""Rotate the protected Insights bootstrap administrator password."""

from __future__ import annotations

import json
import os
import secrets
import tempfile
import urllib.error
import urllib.request
from pathlib import Path


ENV_FILE = Path("/home/ubuntu/app/insights/admin-bootstrap.env")
BASE_URL = "http://127.0.0.1:3001"


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def post_json(path: str, payload: dict[str, str], token: str | None = None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"HTTP {error.code} from {path}") from error


def persist_password(path: Path, password: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    rewritten = [
        f"ADMIN_PASSWORD={password}" if line.startswith("ADMIN_PASSWORD=") else line
        for line in lines
    ]
    fd, temp_name = tempfile.mkstemp(prefix="admin-bootstrap-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as temp_file:
            temp_file.write("\n".join(rewritten) + "\n")
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
    except Exception:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
        raise


def main() -> None:
    values = load_env(ENV_FILE)
    for required in ("ADMIN_EMAIL", "ADMIN_PASSWORD"):
        if not values.get(required):
            raise RuntimeError(f"Missing {required} in bootstrap file")

    session = post_json(
        "/api/v1/auths/signin",
        {"email": values["ADMIN_EMAIL"], "password": values["ADMIN_PASSWORD"]},
    )
    token = session.get("token")
    if not token or session.get("role") != "admin":
        raise RuntimeError("Bootstrap credentials cannot establish an admin session")

    new_password = f"I9@{secrets.token_urlsafe(24)}"
    changed = post_json(
        "/api/v1/auths/update/password",
        {"password": values["ADMIN_PASSWORD"], "new_password": new_password},
        token,
    )
    if changed is not True:
        raise RuntimeError("OpenWebUI did not confirm the password change")

    verified = post_json(
        "/api/v1/auths/signin",
        {"email": values["ADMIN_EMAIL"], "password": new_password},
    )
    if verified.get("role") != "admin":
        raise RuntimeError("New password did not establish an admin session")

    persist_password(ENV_FILE, new_password)
    print(f"admin_email={values['ADMIN_EMAIL']}")
    print(f"temporary_password={new_password}")
    print("signin_verified=true")


if __name__ == "__main__":
    main()

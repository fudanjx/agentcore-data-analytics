#!/usr/bin/env python3
"""Create and remove a temporary account to verify default pending access."""

from __future__ import annotations

import argparse
import json
import urllib.request
import uuid
from pathlib import Path

from e2e_smoke import load_env, request_json


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path("/home/ubuntu/app/insights/admin-bootstrap.env"),
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:3001")
    args = parser.parse_args()
    base_url = args.base_url.rstrip("/")
    credentials = load_env(args.env_file)

    admin = request_json(
        f"{base_url}/api/v1/auths/signin",
        payload={
            "email": credentials["ADMIN_EMAIL"],
            "password": credentials["ADMIN_PASSWORD"],
        },
    )
    admin_token = admin.get("token")
    if not admin_token or admin.get("role") != "admin":
        raise RuntimeError("Could not establish the cleanup admin session")

    suffix = uuid.uuid4().hex
    account = request_json(
        f"{base_url}/api/v1/auths/signup",
        payload={
            "name": "Pending Role Smoke",
            "email": f"pending-smoke-{suffix}@example.invalid",
            "password": f"Smoke-{suffix}",
        },
    )
    user_id = account.get("id")
    role = account.get("role")
    if not user_id or role != "pending":
        raise RuntimeError(f"Expected pending signup, got role={role!r}")

    request = urllib.request.Request(
        f"{base_url}/api/v1/users/{user_id}",
        headers={
            "Authorization": f"Bearer {admin_token}",
            "Content-Type": "application/json",
        },
        method="DELETE",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        deleted = json.load(response)
    if deleted is not True:
        raise RuntimeError("Temporary pending user cleanup failed")

    print("new_user_role=pending")
    print("temporary_user_deleted=true")


if __name__ == "__main__":
    main()

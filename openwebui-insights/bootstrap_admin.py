#!/usr/bin/env python3
"""Create the first OpenWebUI Insights administrator without exposing secrets."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path("/home/ubuntu/app/insights/admin-bootstrap.env"),
    )
    parser.add_argument(
        "--url",
        default="http://127.0.0.1:3001/api/v1/auths/signup",
    )
    args = parser.parse_args()

    values = load_env(args.env_file)
    required = {"ADMIN_NAME", "ADMIN_EMAIL", "ADMIN_PASSWORD"}
    missing = sorted(required - values.keys())
    if missing:
        raise RuntimeError(f"Missing bootstrap variables: {', '.join(missing)}")

    payload = json.dumps(
        {
            "name": values["ADMIN_NAME"],
            "email": values["ADMIN_EMAIL"],
            "password": values["ADMIN_PASSWORD"],
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        args.url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        print(f"bootstrap_http_status={error.code}", file=sys.stderr)
        return 1

    role = result.get("role")
    if role != "admin":
        print(f"bootstrap_role={role or 'missing'}", file=sys.stderr)
        return 1

    print(f"bootstrap_role={role}")
    print(f"bootstrap_email={values['ADMIN_EMAIL']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

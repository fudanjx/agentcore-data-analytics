"""Uvicorn launcher for the API container.

The runtime image is distroless (no shell), so this wrapper replaces what a
``start_api.sh`` would do elsewhere. Behaviour:

- Reads ``S3_UPLOADER_ENVIRONMENT`` (defaults to ``LOCAL``).
- Emits ``--access-log`` in ``LOCAL`` and ``DEV`` for request traceability.
- Emits ``--no-access-log`` in ``STG`` and ``PRD`` to keep prod logs quiet
    when a load balancer already records requests upstream.

Invoked as the container ``CMD``: ``python3 -m s3tables_uploader.scripts.start_api``.
"""

from __future__ import annotations

import os
import sys


_ACCESS_LOG_ENVIRONMENTS = {"LOCAL", "DEV"}


def _access_log_flag(environment: str) -> str:
    return "--access-log" if environment.upper() in _ACCESS_LOG_ENVIRONMENTS else "--no-access-log"


def _build_argv() -> list[str]:
    environment = os.environ.get("S3_UPLOADER_ENVIRONMENT", "LOCAL")
    host = os.environ.get("UVICORN_HOST", "0.0.0.0")
    port = os.environ.get("UVICORN_PORT", "8090")
    workers = os.environ.get("UVICORN_WORKERS", "1")
    return [
        sys.executable,
        "-m",
        "uvicorn",
        "s3tables_uploader.entrypoint:app",
        "--host",
        host,
        "--port",
        port,
        "--workers",
        workers,
        "--proxy-headers",
        _access_log_flag(environment),
    ]


def main() -> None:
    argv = _build_argv()
    os.execvp(argv[0], argv)


if __name__ == "__main__":
    main()

"""Structured and plain loggers for the uploader.

Ported from the reference logger.py: structlog with an asgi_correlation_id
processor that stamps the request id on every event. Two entry points:

- ``create_logger(name)``: plain stdlib logger for scripts and tests.
- ``create_structured_logger(name)``: structlog logger for request handlers,
    services and lifespan.

``configure_logging()`` installs the shared root handler once (called from
the ASGI lifespan). ``safe_error`` and ``new_error_id`` are preserved from
the previous observability.py so worker/dispatcher code that imports them
keeps working during the transition.
"""

from __future__ import annotations

import logging
import os
import sys
import traceback
import uuid
from typing import Any

import structlog
from asgi_correlation_id import correlation_id


_DEBUG_ENVIRONMENTS = {"LOCAL", "DEV"}


def _resolve_log_level() -> int:
    """Read the desired log level from env with a safe default.

    DEV/LOCAL default to DEBUG; other environments use ``S3_UPLOADER_LOG_LEVEL``
    (or INFO). Kept env-driven at this level so the module has no cyclic
    import on Settings.
    """
    environment = os.environ.get("S3_UPLOADER_ENVIRONMENT", "LOCAL").upper()
    if environment in _DEBUG_ENVIRONMENTS:
        return logging.DEBUG
    name = os.environ.get("S3_UPLOADER_LOG_LEVEL", "INFO").upper()
    return getattr(logging, name, logging.INFO)


def _add_request_id(
    logger: logging.Logger,
    method_name: str,
    event_dict: structlog.typing.EventDict,
) -> structlog.typing.EventDict:
    """Attach the current asgi_correlation_id request id when present."""
    if request_id := correlation_id.get():
        event_dict["request_id"] = request_id
    return event_dict


_STRUCTLOG_PROCESSORS: list[Any] = [
    _add_request_id,
    structlog.contextvars.merge_contextvars,
    structlog.processors.TimeStamper(fmt="iso", utc=False),
    structlog.processors.add_log_level,
    structlog.processors.dict_tracebacks,
    structlog.processors.EventRenamer("message"),
    structlog.processors.JSONRenderer(),
]


def configure_logging() -> None:
    """Install a single stdout handler on the root logger.

    Called once from the ASGI lifespan. Uvicorn/FastAPI log records flow
    through the same handler so their output is captured alongside the
    application's structured events.
    """
    root = logging.getLogger()
    if getattr(root, "_pilot_configured", False):
        return
    root.setLevel(logging.DEBUG)  # structlog filters at wrapper level
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            fmt="[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s",
            datefmt="%d-%m-%Y %H:%M:%S",
        )
    )
    root.addHandler(handler)
    root._pilot_configured = True  # type: ignore[attr-defined]


def create_logger(name: str) -> logging.Logger:
    """Return a plain stdlib logger sharing the root handler."""
    logger = logging.getLogger(name)
    logger.setLevel(_resolve_log_level())
    logger.propagate = True
    return logger


def create_structured_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a structlog logger writing JSON events to the shared handler."""
    stdlib_logger = logging.getLogger(name)
    stdlib_logger.setLevel(logging.DEBUG)
    stdlib_logger.propagate = True
    return structlog.wrap_logger(
        stdlib_logger,
        processors=list(_STRUCTLOG_PROCESSORS),
        wrapper_class=structlog.make_filtering_bound_logger(_resolve_log_level()),
        cache_logger_on_first_use=True,
    )


def new_error_id() -> str:
    """Opaque correlation identifier surfaced to clients on server errors."""
    return f"err-{uuid.uuid4().hex[:16]}"


def safe_error(logger: logging.Logger, message: str, **fields: Any) -> str:
    """Log a traceback server-side and return only an opaque correlation ID.

    Raw values (request bodies, ciphertext, filenames) are never included so
    logs remain safe to ship off-box.
    """
    error_id = new_error_id()
    logger.error(message, exc_info=True, extra={"error_id": error_id, **fields})
    return error_id


def safe_exception_text() -> str:
    """For tests and guarded handlers that need a traceback without values."""
    return "".join(traceback.format_exc(limit=20))

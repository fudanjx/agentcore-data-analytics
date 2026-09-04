"""Safe structured logging for the local S3 Tables uploader service.

The service processes healthcare uploads, so log records deliberately carry
only operational metadata.  Raw cell values, ciphertext, request bodies,
authorization headers, and full source filenames must never be logged.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import sys
import traceback
import uuid
from logging.handlers import RotatingFileHandler
from typing import Any


request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)
user_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("user_id", default=None)


class JsonFormatter(logging.Formatter):
    """Render only safe structured fields to a single JSON log record."""

    _standard = frozenset(logging.LogRecord(None, 0, "", 0, "", (), None).__dict__)

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = getattr(record, "request_id", None) or request_id_var.get()
        user_id = getattr(record, "user_id", None) or user_id_var.get()
        if request_id:
            payload["request_id"] = request_id
        if user_id:
            payload["user_id"] = user_id
        for key, value in record.__dict__.items():
            if key not in self._standard and key not in {"message", "asctime"}:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, separators=(",", ":"))


def configure_logging() -> logging.Logger:
    """Configure the uploader logger once for local and container execution."""
    logger = logging.getLogger("s3tables_delta_pilot")
    if getattr(logger, "_pilot_configured", False):
        return logger
    logger.setLevel(os.environ.get("PILOT_LOG_LEVEL", "INFO").upper())
    logger.propagate = False
    formatter = JsonFormatter()
    stdout = logging.StreamHandler(sys.stdout)
    stdout.setFormatter(formatter)
    logger.addHandler(stdout)
    log_path = os.environ.get("PILOT_LOG_FILE")
    if log_path:
        handler = RotatingFileHandler(log_path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    logger._pilot_configured = True  # type: ignore[attr-defined]
    return logger


def new_error_id() -> str:
    return f"err-{uuid.uuid4().hex[:16]}"


def safe_error(logger: logging.Logger, message: str, **fields: Any) -> str:
    """Log a traceback server-side and return only an opaque correlation ID."""
    error_id = new_error_id()
    logger.error(message, exc_info=True, extra={"error_id": error_id, **fields})
    return error_id


def safe_exception_text() -> str:
    """For tests and guarded handlers that need a traceback without values."""
    return "".join(traceback.format_exc(limit=20))

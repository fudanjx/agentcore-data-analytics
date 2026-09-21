"""Identifier helpers for uploads and correlation."""

from __future__ import annotations

import uuid


def new_upload_id() -> str:
    """Return a V1-compatible immutable upload identifier."""
    return f"UPLOAD-{uuid.uuid4().hex[:12].upper()}"


def new_correlation_id() -> str:
    """Return a fresh UUID hex string suitable for request correlation."""
    return uuid.uuid4().hex

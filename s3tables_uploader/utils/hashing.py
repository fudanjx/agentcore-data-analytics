"""Hashing helpers for stable request identifiers and scope keys."""

from __future__ import annotations

import hashlib


def sha256_hex(*parts: str, separator: str = "\x1f") -> str:
    """Return the hex SHA-256 digest of ``separator``-joined ``parts``.

    The unit-separator character keeps composite keys unambiguous when their
    fragments might contain slashes or dots.
    """
    joined = separator.join(parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def scope_key(*parts: str, length: int = 16) -> str:
    """Return a short deterministic identifier for grouping records.

    Used for audit and contract layout where ``(table_bucket_arn, namespace)``
    is folded into a stable prefix segment.
    """
    return sha256_hex(*parts, separator="|")[:length]

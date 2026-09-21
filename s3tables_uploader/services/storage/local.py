"""In-memory :class:`BaseStorage` implementation for tests."""

from __future__ import annotations

import uuid
from typing import Iterable


class LocalStorage:
    """Dict-backed storage that mimics the S3 semantics used by services."""

    def __init__(self) -> None:
        self._items: dict[str, tuple[bytes, str, str]] = {}

    def get(self, key: str) -> bytes:
        if key not in self._items:
            raise KeyError(key)
        return self._items[key][0]

    def put(
        self,
        key: str,
        body: bytes,
        *,
        content_type: str = "application/octet-stream",
        if_match: str | None = None,
        if_none_match: str | None = None,
    ) -> str:
        current_etag = self._items.get(key, (b"", "", ""))[2]
        if if_match is not None and current_etag != if_match:
            raise RuntimeError("PreconditionFailed")
        if if_none_match == "*" and key in self._items:
            raise RuntimeError("PreconditionFailed")
        etag = uuid.uuid4().hex
        self._items[key] = (body, content_type, etag)
        return etag

    def delete(self, key: str) -> None:
        self._items.pop(key, None)

    def head(self, key: str) -> dict[str, str]:
        if key not in self._items:
            raise KeyError(key)
        body, content_type, etag = self._items[key]
        return {
            "etag": etag,
            "content_type": content_type,
            "content_length": str(len(body)),
        }

    def list(self, prefix: str) -> Iterable[str]:
        for key in sorted(self._items):
            if key.startswith(prefix):
                yield key

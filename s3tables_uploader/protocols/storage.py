"""Abstract storage protocol.

Concrete implementations live under ``services/storage/`` (S3 for production;
Local for tests and offline development). The protocol keeps callers unaware
of the backend, so a handler that talks to ``BaseStorage`` is testable with
the in-memory implementation.
"""

from __future__ import annotations

from typing import Iterable, Protocol


class BaseStorage(Protocol):
    """Minimal key/value operations required by services in this app."""

    def get(self, key: str) -> bytes:
        """Return the raw bytes stored at ``key`` or raise ``KeyError``."""
        ...

    def put(
        self,
        key: str,
        body: bytes,
        *,
        content_type: str = "application/octet-stream",
        if_match: str | None = None,
        if_none_match: str | None = None,
    ) -> str:
        """Store ``body`` at ``key`` and return the resulting entity tag."""
        ...

    def delete(self, key: str) -> None:
        """Remove the object at ``key`` (no-op if it never existed)."""
        ...

    def head(self, key: str) -> dict[str, str]:
        """Return metadata (``ETag``, size, ContentType, …) for ``key``."""
        ...

    def list(self, prefix: str) -> Iterable[str]:
        """Yield every key under ``prefix``."""
        ...

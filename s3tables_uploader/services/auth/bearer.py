"""Bearer authentication with an in-memory Secrets Manager cache.

The cache is a per-process singleton owned by ``BearerAuthService``. Its
lifecycle:

- First ``verify`` call fetches the secret from Secrets Manager.
- Subsequent calls compare the presented token against the cached value.
- On TTL expiry the next verify triggers a background-safe refresh.
- On a token mismatch we opportunistically refresh once (bounded by the
    ``refresh_min_interval_seconds`` damper) so an in-flight rotation
    propagates faster than the TTL.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass

from ...core.exceptions import BearerAuthFailed, BearerAuthRequired
from ...protocols.secret_source import SecretSource


_BEARER_PREFIX = re.compile(r"^Bearer\s+(?P<token>.+?)\s*$", re.IGNORECASE)


@dataclass
class _CachedSecret:
    value: str
    fetched_at: float


class BearerAuthService:
    """Validate ``Authorization: Bearer <secret>`` against a Secrets Manager value."""

    def __init__(
        self,
        secret_source: SecretSource,
        secret_arn: str,
        *,
        cache_ttl_seconds: int,
        refresh_min_interval_seconds: int,
        clock: "callable[[], float]" = time.monotonic,
    ):
        self._source = secret_source
        self._secret_arn = secret_arn
        self._ttl = cache_ttl_seconds
        self._refresh_min_interval = refresh_min_interval_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._cached: _CachedSecret | None = None
        self._last_forced_refresh: float | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def warm(self) -> None:
        """Prefetch the secret so the first request does not stall."""
        with self._lock:
            self._refresh_locked()

    def verify(self, authorization_header: str | None) -> None:
        """Raise unless ``authorization_header`` matches the cached secret."""
        token = self._extract_token(authorization_header)
        expected = self._current_secret()
        if _constant_time_equals(token, expected):
            return
        refreshed = self._maybe_force_refresh()
        if refreshed is not None and _constant_time_equals(token, refreshed):
            return
        raise BearerAuthFailed("Bearer token does not match the current secret")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _extract_token(self, header: str | None) -> str:
        if not header:
            raise BearerAuthRequired("Authorization header is required")
        match = _BEARER_PREFIX.match(header)
        if not match:
            raise BearerAuthRequired("Authorization header must use the Bearer scheme")
        token = match.group("token")
        if not token:
            raise BearerAuthRequired("Bearer token is empty")
        return token

    def _current_secret(self) -> str:
        with self._lock:
            if self._cached is None or self._expired_locked():
                self._refresh_locked()
            assert self._cached is not None
            return self._cached.value

    def _maybe_force_refresh(self) -> str | None:
        """Force a refresh if the damper allows it. Returns the new value or None."""
        with self._lock:
            now = self._clock()
            if (
                self._last_forced_refresh is not None
                and (now - self._last_forced_refresh) < self._refresh_min_interval
            ):
                return None
            self._refresh_locked()
            self._last_forced_refresh = now
            assert self._cached is not None
            return self._cached.value

    def _refresh_locked(self) -> None:
        value = self._source.get_secret(self._secret_arn)
        self._cached = _CachedSecret(value=value, fetched_at=self._clock())

    def _expired_locked(self) -> bool:
        assert self._cached is not None
        return (self._clock() - self._cached.fetched_at) >= self._ttl


def _constant_time_equals(candidate: str, expected: str) -> bool:
    if len(candidate) != len(expected):
        return False
    diff = 0
    for a, b in zip(candidate, expected):
        diff |= ord(a) ^ ord(b)
    return diff == 0

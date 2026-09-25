"""Unit tests for the bearer auth service."""

from __future__ import annotations

import unittest

from s3tables_uploader.core.exceptions import BearerAuthFailed, BearerAuthRequired
from s3tables_uploader.services.auth.bearer import BearerAuthService
from s3tables_uploader.services.secret_manager import InMemorySecretSource


class _StepClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class BearerAuthServiceTests(unittest.TestCase):
    def _service(self, secret: str = "topsecret") -> tuple[BearerAuthService, _StepClock, InMemorySecretSource]:
        clock = _StepClock()
        source = InMemorySecretSource({"arn": secret})
        service = BearerAuthService(
            source,
            "arn",
            cache_ttl_seconds=3600,
            refresh_min_interval_seconds=300,
            clock=clock,
        )
        return service, clock, source

    def test_missing_header_requires_bearer(self):
        service, _, _ = self._service()
        with self.assertRaises(BearerAuthRequired):
            service.verify(None)

    def test_non_bearer_scheme_rejected(self):
        service, _, _ = self._service()
        with self.assertRaises(BearerAuthRequired):
            service.verify("Basic abc123")

    def test_valid_token_passes(self):
        service, _, _ = self._service("topsecret")
        service.verify("Bearer topsecret")

    def test_mismatch_raises(self):
        service, _, _ = self._service("topsecret")
        with self.assertRaises(BearerAuthFailed):
            service.verify("Bearer nope")

    def test_refresh_on_miss_catches_rotation(self):
        service, clock, source = self._service("old")
        service.verify("Bearer old")
        source.set_secret("arn", "new")
        # Damper hasn't fired yet, so the very next miss triggers a refresh.
        service.verify("Bearer new")

    def test_damper_prevents_repeated_refreshes(self):
        service, clock, source = self._service("current")
        # Bad tokens keep coming; only one refresh should occur inside the window.
        with self.assertRaises(BearerAuthFailed):
            service.verify("Bearer bogus-1")
        source.set_secret("arn", "rotated")
        with self.assertRaises(BearerAuthFailed):
            service.verify("Bearer bogus-2")
        # Advance past the damper and a legitimate new-secret call succeeds.
        clock.advance(400)
        service.verify("Bearer rotated")

    def test_ttl_expiry_refreshes(self):
        service, clock, source = self._service("first")
        service.verify("Bearer first")
        source.set_secret("arn", "second")
        clock.advance(4000)  # past 1h TTL
        service.verify("Bearer second")


if __name__ == "__main__":
    unittest.main()

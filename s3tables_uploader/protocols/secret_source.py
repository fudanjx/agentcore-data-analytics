"""Abstract secret source for bearer authentication.

``BearerAuthService`` composes a ``SecretSource`` so tests can substitute an
in-memory provider without touching AWS Secrets Manager.
"""

from __future__ import annotations

from typing import Protocol


class SecretSource(Protocol):
    """Return the raw string value of a secret."""

    def get_secret(self, secret_arn: str) -> str:
        """Fetch the current value of ``secret_arn`` from the backing store."""
        ...

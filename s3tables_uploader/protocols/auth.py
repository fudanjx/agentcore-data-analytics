"""Abstract auth protocol.

Two concrete implementations exist:

- ``services.auth.cookie.CookieAuthService`` — LOCAL frontend and DEV modes.
- ``services.auth.bearer.BearerAuthService`` — LOCAL API-only, STG, PRD.

The service is chosen at dependency-resolution time from
``settings.bearer_auth_required``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class BucketGrant:
    """A single bucket grant recorded on a user profile."""

    table_bucket_arn: str
    namespace: str
    label: str


@dataclass(frozen=True)
class UserContext:
    """Resolved identity for a single request.

    ``user_id`` is a profile key in frontend modes and an email address in
    hardened modes. Permission flags are always ``True`` in hardened mode —
    permissioning is enforced by the client application that holds the bearer
    secret, not by this API.
    """

    user_id: str
    is_admin: bool
    can_view_upload_history: bool
    can_rollback_uploads: bool
    visible_buckets: list[BucketGrant] | None = None


class AuthService(Protocol):
    """Extract a ``UserContext`` from an incoming request."""

    def authenticate(self, request: Any) -> UserContext:
        """Return the resolved user context or raise an ``UploaderError``."""
        ...

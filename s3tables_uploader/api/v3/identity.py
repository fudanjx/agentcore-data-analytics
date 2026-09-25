"""Identity endpoints.

Returns the resolved user context so the browser can render its scope.
Registered in every environment; the underlying resolver decides whether
it reads a cookie or a bearer/User-ID header.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from fastapi import APIRouter

from ...app.dependencies import UserDep
from ...core.constants import FRONTEND_IDENTITY_HEADER
from ...utils.api_prefix import get_api_prefix


router = APIRouter(prefix=get_api_prefix(Path(__file__), "api"))


@router.get("")
def identity(user: UserDep) -> dict[str, object]:
    grants = user.visible_buckets
    return {
        "user_id": user.user_id,
        "is_admin": user.is_admin,
        "can_view_upload_history": user.can_view_upload_history,
        "can_rollback_uploads": user.can_rollback_uploads,
        "scope_mode": "all-discoverable-buckets-and-namespaces"
        if user.is_admin or grants is None
        else "configured-bucket-and-namespace-scopes",
        "buckets": None if grants is None else [asdict(grant) for grant in grants],
        "request_context": {
            "header_name": FRONTEND_IDENTITY_HEADER,
            "header_value": user.user_id,
            "roles_and_grants_sent_by_browser": False,
        },
    }

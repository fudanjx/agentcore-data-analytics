"""Hardcoded identity profile listing for the temporary frontend.

Frontend-only route: the factory registers it only when
``settings.frontend_surface_enabled``. In hardened modes profiles do not
exist — the calling application owns permissions.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter

from ...app.dependencies import ProfileServiceDep
from ...core.constants import FRONTEND_IDENTITY_HEADER
from ...utils.api_prefix import get_api_prefix


router = APIRouter(prefix=get_api_prefix(Path(__file__), "api"))


@router.get("/identity-profiles")
def identity_profiles(profiles: ProfileServiceDep) -> dict[str, object]:
    assert profiles is not None, "dev router registered outside frontend mode"
    return {
        "local_only": True,
        "header_name": FRONTEND_IDENTITY_HEADER,
        "profiles": profiles.list_profiles(),
        "note": (
            "The browser sends only the user-ID header; the backend resolves "
            "roles and bucket grants."
        ),
    }

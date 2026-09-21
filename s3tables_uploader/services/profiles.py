"""Hardcoded identity profiles used in LOCAL frontend and DEV modes."""

from __future__ import annotations

from ..core.exceptions import TableBucketForbidden
from ..protocols.auth import BucketGrant, UserContext


_LOCAL_IDENTITY_PROFILES: dict[str, dict[str, object]] = {
    "local-admin": {
        "is_admin": True,
        "can_view_upload_history": True,
        "can_rollback_uploads": True,
        "buckets": [],
    },
    "local-editor": {
        "is_admin": False,
        "can_view_upload_history": True,
        "can_rollback_uploads": True,
        "buckets": [
            {
                "table_bucket_arn": "arn:aws:s3tables:ap-southeast-1:964340114883:bucket/ah-soc-delta-pilot",
                "namespace": "pilot",
                "label": "AH SOC delta pilot",
            }
        ],
    },
    "local-unassigned": {
        "is_admin": False,
        "can_view_upload_history": False,
        "can_rollback_uploads": False,
        "buckets": [],
    },
}


class LocalIdentityProfileService:
    """Resolve profile keys into :class:`UserContext` instances.

    Only registered in LOCAL frontend and DEV modes; hardened modes have no
    profile database and rely on the calling application for permissions.
    """

    def __init__(self, profiles: dict[str, dict[str, object]] | None = None):
        self._profiles = profiles or _LOCAL_IDENTITY_PROFILES

    def resolve(self, user_id: str) -> UserContext:
        profile = self._profiles.get(user_id)
        if profile is None:
            raise TableBucketForbidden(
                "This user has no S3 Tables bucket assignment"
            )
        grants = [
            BucketGrant(
                table_bucket_arn=str(item["table_bucket_arn"]),
                namespace=str(item["namespace"]),
                label=str(item["label"]),
            )
            for item in profile.get("buckets", [])  # type: ignore[arg-type]
        ]
        return UserContext(
            user_id=user_id,
            is_admin=bool(profile["is_admin"]),
            can_view_upload_history=bool(profile["can_view_upload_history"]),
            can_rollback_uploads=bool(profile["can_rollback_uploads"]),
            visible_buckets=grants,
        )

    def list_profiles(self) -> list[dict[str, object]]:
        """Return the raw profile records for the ``/api/dev`` listing route."""
        return [
            {
                "user_id": user_id,
                **profile,
                "expected_access": bool(
                    profile["is_admin"] or profile["buckets"]  # type: ignore[operator]
                ),
            }
            for user_id, profile in self._profiles.items()
        ]

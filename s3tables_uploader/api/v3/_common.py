"""Shared route guards and helpers used across v3 routers."""

from __future__ import annotations

import json
from typing import Any

from ...core.exceptions import AdminRequired, TableBucketForbidden
from ...protocols.auth import UserContext
from ...services.tables import TableBucketService


def require_admin(user: UserContext) -> None:
    if not user.is_admin:
        raise AdminRequired("Only administrators may perform this operation")


def require_table_bucket_access(
    arn: str, user: UserContext, tables: TableBucketService
) -> None:
    """Confirm the caller can act on ``arn``.

    Shared by every router that accepts a ``table_bucket_arn`` parameter.
    """
    visible = {item["table_bucket_arn"] for item in tables.list_buckets()}
    if arn not in visible:
        raise TableBucketForbidden("TABLE_BUCKET_FORBIDDEN")
    if user.visible_buckets is not None and not user.is_admin:
        grant_arns = {grant.table_bucket_arn for grant in user.visible_buckets}
        if arn not in grant_arns:
            raise TableBucketForbidden("TABLE_BUCKET_FORBIDDEN")


def iceberg_row_count(s3: Any, metadata_uri: str | None) -> int | None:
    """Return the current Iceberg snapshot total without scanning table data."""
    if not metadata_uri or not metadata_uri.startswith("s3://"):
        return None
    try:
        bucket, key = metadata_uri.removeprefix("s3://").split("/", 1)
        metadata = json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
        current = metadata.get("current-snapshot-id")
        snapshot = next(
            (item for item in metadata.get("snapshots", []) if item.get("snapshot-id") == current),
            None,
        )
        total = (snapshot or {}).get("summary", {}).get("total-records")
        return int(total) if total is not None else None
    except Exception:
        return None

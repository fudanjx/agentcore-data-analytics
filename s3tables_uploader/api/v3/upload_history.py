"""Read-only upload history endpoint."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException

from ...app.dependencies import (
    AuditReaderDep,
    ContractServiceDep,
    TableBucketServiceDep,
    UserDep,
)
from ...core.constants import UPLOAD_HISTORY_TABLE
from ...utils.api_prefix import get_api_prefix
from ._common import require_table_bucket_access


router = APIRouter(prefix=get_api_prefix(Path(__file__), "api"))


@router.get("")
def upload_history(
    table_bucket_arn: str,
    namespace: str,
    table: str,
    user: UserDep,
    tables: TableBucketServiceDep,
    contracts: ContractServiceDep,
    audit: AuditReaderDep,
) -> dict[str, object]:
    require_table_bucket_access(table_bucket_arn, user, tables)
    if not user.can_view_upload_history:
        raise HTTPException(403, "This user cannot view upload history")
    if table == UPLOAD_HISTORY_TABLE:
        raise HTTPException(400, "The reserved uploader audit table is not a master-data destination")
    if not contracts.is_uploader_managed(table_bucket_arn, namespace, table):
        raise HTTPException(
            409, "This table is browse-only because it has no uploader history contract"
        )
    history = audit.read_entries(table_bucket_arn, namespace, table)
    successful = [
        item for item in history
        if item.get("status") == "SUCCESS" and item.get("previous_snapshot_id")
    ]
    latest = max(successful, key=lambda item: item.get("uploaded_at") or "", default=None)
    return {
        "table_bucket_arn": table_bucket_arn,
        "namespace": namespace,
        "table": table,
        "history": history,
        "latest_rollback_upload_id": latest.get("upload_id") if latest else None,
    }

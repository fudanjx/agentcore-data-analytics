"""Rollback and mutation status endpoints."""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ...app.dependencies import (
    AuditReaderDep,
    ContractServiceDep,
    MutationServiceDep,
    StoreDep,
    TableBucketServiceDep,
    UserDep,
    enforce_ownership,
)
from ...core.constants import UPLOAD_HISTORY_TABLE
from ...job_store import MissingRecord
from ...models import Destination, JobStatus, MutationCommand
from ...utils.api_prefix import get_api_prefix
from ...utils.time import now_iso
from ._common import require_table_bucket_access


router = APIRouter(prefix=get_api_prefix(Path(__file__), "api"))


class RollbackRequest(BaseModel):
    table: str = Field(pattern=r"^[a-z][a-z0-9_]{0,254}$")
    table_bucket_arn: str = Field(min_length=1)
    namespace: str = Field(pattern=r"^[a-z][a-z0-9_]{0,254}$")
    upload_id: str = Field(min_length=1, max_length=128)
    confirm: bool = False


@router.get("/{mutation_id}")
def mutation_status(
    mutation_id: str,
    user: UserDep,
    store: StoreDep,
) -> dict[str, object]:
    try:
        command = store.get_mutation_command(mutation_id)
        status = store.get_status(mutation_id).status
    except MissingRecord as error:
        raise HTTPException(404, "MUTATION_NOT_FOUND") from error
    enforce_ownership(command.owner_user_id, user)
    return {"mutation": command.model_dump(mode="json"), "status": status.model_dump(mode="json")}


# ---------------------------------------------------------------------------
# Rollbacks (own router; same file to keep the flow together)
# ---------------------------------------------------------------------------

rollbacks_router = APIRouter(prefix="/api/v3/rollbacks")


@rollbacks_router.post("", status_code=202)
def start_rollback(
    payload: RollbackRequest,
    user: UserDep,
    tables: TableBucketServiceDep,
    contracts: ContractServiceDep,
    audit: AuditReaderDep,
    store: StoreDep,
    mutations: MutationServiceDep,
) -> dict[str, object]:
    require_table_bucket_access(payload.table_bucket_arn, user, tables)
    if not user.can_rollback_uploads:
        raise HTTPException(403, "This user cannot roll back uploads")
    if not payload.confirm:
        raise HTTPException(400, "Explicit rollback confirmation is required")
    if payload.table == UPLOAD_HISTORY_TABLE:
        raise HTTPException(
            400, "The reserved uploader audit table cannot be rolled back through this UI"
        )
    if not contracts.is_uploader_managed(
        payload.table_bucket_arn, payload.namespace, payload.table
    ):
        raise HTTPException(
            409, "This table is browse-only because it has no uploader history contract"
        )
    history = audit.read_entries(payload.table_bucket_arn, payload.namespace, payload.table)
    selected = next((item for item in history if item.get("upload_id") == payload.upload_id), None)
    successful = [item for item in history if item.get("status") == "SUCCESS"]
    latest = max(successful, key=lambda item: item.get("uploaded_at") or "", default=None)
    if not selected or selected.get("status") != "SUCCESS":
        raise HTTPException(
            409,
            "Only a successful upload that has not already been rolled back can be restored",
        )
    if selected != latest:
        raise HTTPException(
            409, "Only the latest successful uploader-managed update may be rolled back"
        )
    snapshot_id = selected.get("previous_snapshot_id")
    if not snapshot_id:
        raise HTTPException(409, "The initial table load has no earlier snapshot to restore")

    # Rollback is a table mutation like create and append.  Its stable
    # request id makes an accidental repeat click reconnect to the same
    # durable command instead of starting a second Glue restore.
    request_id = hashlib.sha256(
        f"rollback\x1f{user.user_id}\x1f{payload.table_bucket_arn}\x1f{payload.namespace}\x1f{payload.table}\x1f{payload.upload_id}".encode()
    ).hexdigest()
    try:
        mutation_id = store.get_mutation_request(owner_user_id=user.user_id, request_id=request_id)
    except MissingRecord:
        proposed_id = str(uuid.uuid4())
        command = MutationCommand(
            mutation_id=proposed_id,
            request_id=request_id,
            owner_user_id=user.user_id,
            operation="rollback",
            destination=Destination(
                table_bucket_arn=payload.table_bucket_arn,
                namespace=payload.namespace,
                table=payload.table,
            ),
            upload_id=payload.upload_id,
            rollback_snapshot_id=str(snapshot_id),
            original_uploaded_by=selected.get("uploaded_by") or user.user_id,
            original_uploaded_at=selected.get("uploaded_at") or now_iso(),
            reporting_month=selected.get("reporting_month") or "not-applicable",
            filenames_json=selected.get("filenames") or "[]",
        )
        store.put_mutation_command(command)
        if store.put_mutation_request(
            owner_user_id=user.user_id, request_id=request_id, mutation_id=proposed_id
        ):
            store.put_status(
                JobStatus(
                    job_id=proposed_id,
                    phase="READY_FOR_MUTATION",
                    message="Rollback is queued for per-table FIFO Glue dispatch.",
                )
            )
            mutation_id = proposed_id
        else:
            mutation_id = store.get_mutation_request(
                owner_user_id=user.user_id, request_id=request_id
            )
    command = store.get_mutation_command(mutation_id)
    mutations.enqueue(mutation_id, command.destination)
    return {
        "mutation_id": mutation_id,
        "phase": store.get_status(mutation_id).status.phase,
        "status_url": f"/api/v3/mutations/{mutation_id}",
        "upload_id": payload.upload_id,
        "operation": "rollback",
    }

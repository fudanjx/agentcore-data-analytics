"""Worker lease lifecycle endpoints."""

from __future__ import annotations

import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field

from ...app.dependencies import (
    LeaseServiceDep,
    StoreDep,
    UserDep,
    enforce_ownership,
)
from ...core.constants import ACTIVE_LEASE_MINUTES, PREUPLOAD_LEASE_MINUTES
from ...job_store import MissingRecord, RecordStateConflict
from ...models import JobRequest, JobStatus
from ...utils.api_prefix import get_api_prefix
from ...utils.time import now_iso, utc_now
from ._compat import (
    delete_raw_source_versions as _delete_raw_source_versions,
    get_compat_session as _get_compat_session,
    save_compat_session as _save_compat_session,
)


router = APIRouter(prefix=get_api_prefix(Path(__file__), "api"))


class LeaseFile(BaseModel):
    name: str = Field(min_length=1, max_length=512)
    size_bytes: int = Field(gt=0)


class CreateWorkerLeaseRequest(BaseModel):
    files: list[LeaseFile] = Field(min_length=1)


_ACTIVE_LEASE_STATES = frozenset(
    {"STARTING", "AWAITING_UPLOAD", "PROFILING", "AWAITING_KEY", "ANALYSING_KEY", "AWAITING_CONFIRMATION"}
)
_CANCELLABLE_STATES = frozenset(
    {
        "STARTING",
        "AWAITING_UPLOAD",
        "PROFILING",
        "AWAITING_KEY",
        "ANALYSING_KEY",
        "AWAITING_CONFIRMATION",
        "FAILED",
    }
)
_CANCELLABLE_SESSION_PHASES = frozenset(
    {
        "RECEIVED",
        "PROFILING",
        "READY_FOR_REVIEW",
        "KEY_ANALYSING",
        "READY_FOR_ACKNOWLEDGEMENT",
        "FAILED",
        "DELETED",
    }
)


@router.post("", status_code=201)
def create_lease(
    payload: CreateWorkerLeaseRequest,
    user: UserDep,
    leases: LeaseServiceDep,
) -> dict[str, object]:
    lease = leases.new_lease([item.model_dump() for item in payload.files], user.user_id)
    return leases.as_response(lease)


@router.put("/{lease_id}")
def replace_idle_lease(
    lease_id: str,
    payload: CreateWorkerLeaseRequest,
    user: UserDep,
    leases: LeaseServiceDep,
    store: StoreDep,
) -> dict[str, object]:
    try:
        lease = store.get_lease(lease_id)
    except MissingRecord as error:
        raise HTTPException(404, "WORKER_LEASE_NOT_FOUND") from error
    enforce_ownership(lease.get("owner_user_id"), user)
    session_id = str(lease.get("session_id") or "")
    if lease.get("state") not in _ACTIVE_LEASE_STATES:
        raise HTTPException(409, "WORKER_LEASE_CANNOT_BE_REUSED")
    if session_id:
        try:
            session = store.get_compat_session(session_id)
        except MissingRecord as error:
            raise HTTPException(409, "WORKER_LEASE_CANNOT_BE_REUSED") from error
        if session.get("phase") not in {
            "RECEIVED",
            "PROFILING",
            "READY_FOR_REVIEW",
            "KEY_ANALYSING",
            "READY_FOR_ACKNOWLEDGEMENT",
        }:
            raise HTTPException(409, "WORKER_LEASE_CANNOT_BE_REUSED")
    files = [item.model_dump() for item in payload.files]
    route = leases._route_files(files)  # noqa: SLF001 — private for now, wrapper in Phase 6
    if route.worker_size == lease.get("worker_size"):
        lease = store.update_lease(
            lease_id,
            {
                "files": files,
                "routing_score": route.routing_score,
                "routing_reason": route.routing_reason,
                "session_id": None,
                "replaced_session_id": session_id or None,
                "state": "AWAITING_UPLOAD",
                "message": "File selection updated; reusing the existing worker.",
                "updated_at": now_iso(),
                "expires_at": (utc_now() + timedelta(minutes=PREUPLOAD_LEASE_MINUTES)).isoformat(),
            },
        )
        return {**leases.as_response(lease), "reused": True, "replaced": False}
    old_size, new_size = str(lease.get("worker_size")), route.worker_size
    lease = store.update_lease(
        lease_id,
        {
            "state": "CANCELLED",
            "message": f"File selection changed from {old_size} to {new_size}; replacing worker.",
            "updated_at": now_iso(),
        },
    )
    replacement = leases.new_lease(files, user.user_id)
    return {
        **leases.as_response(replacement),
        "reused": False,
        "replaced": True,
        "replaced_lease_id": lease_id,
    }


@router.delete("/{lease_id}", status_code=204)
def cancel_lease(
    lease_id: str,
    user: UserDep,
    store: StoreDep,
) -> Response:
    try:
        lease = store.get_lease(lease_id)
    except MissingRecord as error:
        raise HTTPException(404, "WORKER_LEASE_NOT_FOUND") from error
    enforce_ownership(lease.get("owner_user_id"), user)
    session: dict[str, Any] | None = None
    session_id = str(lease.get("session_id") or "")
    if session_id:
        try:
            session = store.get_compat_session(session_id)
        except MissingRecord as error:
            raise HTTPException(409, "CANCEL_AND_START_OVER_UNAVAILABLE") from error
        enforce_ownership(session.get("owner_user_id"), user)

    if lease.get("state") != "CANCELLED":
        if lease.get("cancellation_locked_at") or (session and session.get("ingestion")):
            raise HTTPException(409, "CANCEL_AND_START_OVER_UNAVAILABLE")
        if (
            lease.get("state") not in _CANCELLABLE_STATES
            or (session and session.get("phase") not in _CANCELLABLE_SESSION_PHASES)
        ):
            raise HTTPException(409, "CANCEL_AND_START_OVER_UNAVAILABLE")
        try:
            lease = store.update_lease(
                lease_id,
                {
                    "state": "CANCELLED",
                    "message": "Cancelled by the upload owner.",
                    "updated_at": now_iso(),
                },
                guard=lambda current: current.get("owner_user_id") == user.user_id
                and not current.get("cancellation_locked_at")
                and current.get("state") in _CANCELLABLE_STATES,
            )
        except RecordStateConflict as error:
            raise HTTPException(409, "CANCEL_AND_START_OVER_UNAVAILABLE") from error

    if session and session.get("phase") != "DELETED":
        try:
            session = store.update_compat_session(
                session_id,
                {
                    "phase": "DELETED",
                    "phase_started_at": now_iso(),
                    "updated_at": now_iso(),
                    "progress_message": "Upload cancelled before ETL acceptance.",
                    "error": None,
                    "cleanup_pending": True,
                },
                guard=lambda current: current.get("owner_user_id") == user.user_id
                and not current.get("ingestion")
                and current.get("phase") != "DELETED",
            )
        except RecordStateConflict as error:
            raise HTTPException(409, "CANCEL_AND_START_OVER_UNAVAILABLE") from error
    if session:
        _delete_raw_source_versions(store, session)
        store.update_compat_session(
            session_id, {"cleanup_pending": False, "updated_at": now_iso()}
        )
    return Response(status_code=204)


@router.post("/{lease_id}/retry-large", status_code=202)
def retry_large(
    lease_id: str,
    user: UserDep,
    leases: LeaseServiceDep,
    store: StoreDep,
) -> dict[str, object]:
    try:
        lease = store.get_lease(lease_id)
    except MissingRecord as error:
        raise HTTPException(404, "WORKER_LEASE_NOT_FOUND") from error
    enforce_ownership(lease.get("owner_user_id"), user)
    if (
        lease.get("worker_size") != "BASE"
        or lease.get("state") != "RESOURCE_LIMIT_EXCEEDED"
        or not lease.get("can_retry_large")
    ):
        raise HTTPException(409, "LARGE_RETRY_UNAVAILABLE")
    session = _get_compat_session(store, str(lease.get("session_id") or ""), user)
    resume_phase = str(lease.get("resume_phase") or "")
    if resume_phase not in {"RECEIVED", "KEY_ANALYSING", "QUEUED"}:
        raise HTTPException(409, "LARGE_RETRY_UNAVAILABLE")
    if resume_phase == "QUEUED":
        ingestion = session.get("ingestion") or {}
        job_id = str(ingestion.get("job_id") or "")
        try:
            old_status = store.get_status(job_id).status
        except MissingRecord as error:
            raise HTTPException(409, "LARGE_RETRY_UNAVAILABLE") from error
        if old_status.phase in {"STARTING_GLUE", "RUNNING_GLUE", "SUCCEEDED"}:
            raise HTTPException(409, "GLUE_SUBMISSION_MAY_HAVE_STARTED")
        old_request = store.get_request(job_id)
        new_job_id = str(uuid.uuid4())
        replacement = old_request.model_copy(update={"job_id": new_job_id})
        store.put_request(replacement)
        store.put_status(
            JobStatus(job_id=new_job_id, phase="QUEUED", message="Large worker retry queued.")
        )
        session["ingestion"] = {
            **ingestion,
            "job_id": new_job_id,
            "state": "QUEUED",
            "job_run_id": None,
        }
    _save_compat_session(
        store, session, phase=resume_phase, progress_message="Large worker retry is starting.", error=None
    )
    lease = store.update_lease(
        lease_id,
        {
            "worker_size": "LARGE",
            "state": "STARTING",
            "message": "Starting the requested large worker retry.",
            "can_retry_large": False,
            "attempt": int(lease.get("attempt", 1)) + 1,
            "updated_at": now_iso(),
            "expires_at": (utc_now() + timedelta(minutes=ACTIVE_LEASE_MINUTES)).isoformat(),
        },
    )
    leases.dispatch(lease)
    return leases.as_response(lease)



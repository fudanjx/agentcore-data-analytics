"""Upload session lifecycle endpoints.

Covers both the direct-S3 JSON protocol and the multipart-form compat
protocol used by the temporary frontend. ``POST /api/v3/upload-sessions``
dispatches to the appropriate typed handler based on the request's
``Content-Type`` — a single URL to preserve the browser contract.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from ...app.dependencies import (
    ContractServiceDep,
    LeaseServiceDep,
    MutationServiceDep,
    S3Dep,
    SettingsDep,
    StoreDep,
    TableBucketServiceDep,
    UserDep,
    enforce_ownership,
)
from ...core.constants import (
    S3_SSE,
    SUPPORTED_COMPAT_SUFFIXES,
    UPLOAD_HISTORY_TABLE,
    UPLOAD_PART_BYTES,
)
from ...job_store import MissingRecord, S3JobStore
from ...models import (
    Destination,
    JobRequest,
    JobSource,
    JobStatus,
    MutationCommand,
    UploadSession,
)
from ...protocols.auth import UserContext
from ...utils.api_prefix import get_api_prefix
from ...utils.time import now_iso
from ...utils.upload_ids import new_upload_id
from ._common import require_table_bucket_access
from ._compat import (
    delete_raw_source_versions,
    get_compat_session,
    safe_compat_session_response,
    save_compat_session,
)


router = APIRouter(prefix=get_api_prefix(Path(__file__), "api"))


# ---------------------------------------------------------------------------
# Request/response bodies
# ---------------------------------------------------------------------------

class CreateSessionRequest(BaseModel):
    file_name: str = Field(min_length=1, max_length=512)
    content_type: str = Field(min_length=1, max_length=255)
    source_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class CompleteSessionRequest(BaseModel):
    parts: list[dict[str, Any]] = Field(min_length=1)
    operation: Literal["create", "append"]
    destination: Destination


class PartUrlRequest(BaseModel):
    part_number: int = Field(ge=1, le=10_000)


class SessionIngestionRequest(BaseModel):
    request_id: str = Field(min_length=1, max_length=128)
    reporting_month: str = Field(default="", max_length=128)
    deduplication_mode: Literal["none", "keyed"] = "none"
    deduplication_columns: list[str] = Field(default_factory=list)
    key_analysis_token: str | None = None
    type_overrides: dict[str, str] = Field(default_factory=dict)
    manual_encryption_columns: list[str] = Field(default_factory=list)
    temporal_policy_acknowledgement_token: str | None = None


class SessionKeyImpactRequest(BaseModel):
    deduplication_columns: list[str] = Field(min_length=1)
    type_overrides: dict[str, str] = Field(default_factory=dict)


def _safe_upload_name(name: str) -> str:
    value = Path(name).name
    if not value or value in {".", ".."}:
        raise HTTPException(400, "Each uploaded file must have a filename")
    return value


# ---------------------------------------------------------------------------
# POST /api/v3/upload-sessions — dispatch between JSON and multipart
# ---------------------------------------------------------------------------

@router.post("", status_code=201)
async def create_session(
    request: Request,
    user: UserDep,
    settings: SettingsDep,
    store: StoreDep,
    s3: S3Dep,
    tables: TableBucketServiceDep,
    leases: LeaseServiceDep,
) -> dict[str, object]:
    """Accept direct-S3 JSON requests and immutable multipart form uploads."""
    content_type = request.headers.get("content-type", "").lower()
    if content_type.startswith("multipart/form-data"):
        return await _create_compat_session(request, user, settings, store, s3, tables, leases)
    try:
        payload = CreateSessionRequest.model_validate(await request.json())
    except Exception as error:
        raise HTTPException(
            422, "Expected the direct-S3 JSON protocol or a multipart upload form"
        ) from error
    session_id = str(uuid.uuid4())
    key = f"{settings.landing_prefix}/uploads/{session_id}/raw/{payload.file_name}"
    metadata = {"session-id": session_id, "owner-user-id": user.user_id}
    if payload.source_sha256:
        metadata["sha256"] = payload.source_sha256
    response = s3.create_multipart_upload(
        Bucket=settings.landing_bucket,
        Key=key,
        ContentType=payload.content_type,
        ServerSideEncryption=S3_SSE,
        Metadata=metadata,
    )
    session = UploadSession(
        session_id=session_id,
        owner_user_id=user.user_id,
        file_name=payload.file_name,
        content_type=payload.content_type,
        source_key=key,
        multipart_upload_id=response["UploadId"],
        expected_sha256=payload.source_sha256,
    )
    store.put_session(session)
    return {
        "session_id": session_id,
        "upload_id": session.multipart_upload_id,
        "source_key": session.source_key,
    }


# ---------------------------------------------------------------------------
# GET / DELETE by session id
# ---------------------------------------------------------------------------

@router.get("/{session_id}")
def get_session(
    session_id: str,
    user: UserDep,
    store: StoreDep,
    settings: SettingsDep,
    leases: LeaseServiceDep,
) -> dict[str, object]:
    session = get_compat_session(store, session_id, user)
    job_id = (session.get("ingestion") or {}).get("job_id")
    if job_id and session.get("phase") != "FAILED":
        try:
            # Mirror worker-owned durable progress into the session so the
            # browser can reconnect after an API replacement / page refresh.
            status = store.get_status(job_id).status
            phase_map = {
                "QUEUED": "QUEUED",
                "CLAIMED": "QUEUED",
                "PROFILING": "QUEUED",
                "PREPARING": "STARTING_GLUE",
                "READY_FOR_MUTATION": "QUEUED",
                "STARTING_GLUE": "STARTING_GLUE",
                "RUNNING_GLUE": "GLUE_RUNNING",
                "SUCCEEDED": "SUCCEEDED",
                "FAILED": "FAILED",
            }
            ingestion = {
                **(session.get("ingestion") or {}),
                "state": status.phase,
                "job_run_id": status.glue_run_id,
                "qc_uri": f"s3://{settings.landing_bucket}/{settings.landing_prefix}/qc/{job_id}.json",
            }
            changes: dict[str, Any] = {
                "phase": phase_map[status.phase],
                "progress_message": status.message,
                "ingestion": ingestion,
            }
            if status.phase == "FAILED":
                changes["error"] = {
                    "code": status.error_code or "WORKER_FAILED",
                    "message": status.message,
                }
            session = save_compat_session(store, session, **changes)
        except MissingRecord:
            pass
    return safe_compat_session_response(store, session, leases)


@router.delete("/{session_id}", status_code=204)
def delete_session(
    session_id: str,
    user: UserDep,
    store: StoreDep,
) -> Response:
    session = get_compat_session(store, session_id, user)
    save_compat_session(store, session, phase="DELETED", progress_message="Session deleted.")
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Multipart PART URL and COMPLETE (direct-S3 protocol)
# ---------------------------------------------------------------------------

@router.post("/{session_id}/parts")
def create_part_url(
    session_id: str,
    payload: PartUrlRequest,
    user: UserDep,
    store: StoreDep,
    s3: S3Dep,
    settings: SettingsDep,
) -> dict[str, str]:
    try:
        session = store.get_session(session_id)
    except MissingRecord as error:
        raise HTTPException(404, "UPLOAD_SESSION_NOT_FOUND") from error
    enforce_ownership(session.owner_user_id, user)
    url = s3.generate_presigned_url(
        "upload_part",
        Params={
            "Bucket": settings.landing_bucket,
            "Key": session.source_key,
            "UploadId": session.multipart_upload_id,
            "PartNumber": payload.part_number,
        },
        ExpiresIn=900,
        HttpMethod="PUT",
    )
    return {"url": url}


@router.post("/{session_id}/complete", status_code=202)
def complete_session(
    session_id: str,
    payload: CompleteSessionRequest,
    user: UserDep,
    store: StoreDep,
    s3: S3Dep,
    settings: SettingsDep,
    leases: LeaseServiceDep,
    mutations: MutationServiceDep,
) -> dict[str, str]:
    try:
        session = store.get_session(session_id)
    except MissingRecord as error:
        raise HTTPException(404, "UPLOAD_SESSION_NOT_FOUND") from error
    enforce_ownership(session.owner_user_id, user)
    s3.complete_multipart_upload(
        Bucket=settings.landing_bucket,
        Key=session.source_key,
        UploadId=session.multipart_upload_id,
        MultipartUpload={"Parts": payload.parts},
    )
    source = s3.head_object(Bucket=settings.landing_bucket, Key=session.source_key)
    if session.expected_sha256 and S3JobStore.object_sha256(source) != session.expected_sha256:
        raise HTTPException(409, "UPLOAD_CHECKSUM_MISMATCH")
    version_id = source.get("VersionId")
    if not version_id:
        raise HTTPException(500, "LANDING_BUCKET_VERSIONING_REQUIRED")
    job_id = str(uuid.uuid4())
    job = JobRequest(
        job_id=job_id,
        session_id=session_id,
        owner_user_id=user.user_id,
        operation=payload.operation,
        destination=payload.destination,
        source_key=session.source_key,
        source_version_id=version_id,
        source_sha256=session.expected_sha256,
        source_size_bytes=source["ContentLength"],
        upload_id=new_upload_id(),
    )
    store.put_request(job)
    store.put_mutation_command(
        MutationCommand(
            mutation_id=job_id,
            request_id=job_id,
            owner_user_id=user.user_id,
            operation=job.operation,
            destination=job.destination,
            upload_id=job.upload_id,
            source_job_id=job_id,
        )
    )
    store.put_status(
        JobStatus(
            job_id=job_id, phase="QUEUED", message="Upload completed; waiting for processing."
        )
    )
    mutations.enqueue(job_id, job.destination)
    leases.dispatch_direct_job(job_id, session.file_name, int(source["ContentLength"]))
    return {"job_id": job_id, "phase": "QUEUED"}


# ---------------------------------------------------------------------------
# Key-impact + ingestion for the compat session
# ---------------------------------------------------------------------------

@router.post("/{session_id}/key-impact", status_code=202)
def key_impact(
    session_id: str,
    payload: SessionKeyImpactRequest,
    user: UserDep,
    store: StoreDep,
) -> dict[str, object]:
    session = get_compat_session(store, session_id, user)
    if session.get("phase") not in {"READY_FOR_REVIEW", "READY_FOR_ACKNOWLEDGEMENT"}:
        raise HTTPException(
            409,
            f"Key-impact analysis is unavailable while session phase is {session.get('phase')}",
        )
    columns = payload.deduplication_columns
    known = {
        item["column"]
        for item in (session.get("preflight") or {}).get("deduplication_candidates", [])
    }
    unknown = sorted(set(columns) - known)
    if unknown:
        raise HTTPException(
            422, f"Unknown de-duplication columns: {', '.join(unknown)}"
        )
    save_compat_session(
        store,
        session,
        phase="KEY_ANALYSING",
        progress_message="Queued for isolated composite-key analysis.",
        key_impact=None,
        key_analysis_request={
            "deduplication_columns": columns,
            "type_overrides": payload.type_overrides,
        },
    )
    return {
        "session_id": session_id,
        "phase": "KEY_ANALYSING",
        "message": "Composite-key analysis has started in the isolated worker.",
    }


@router.post("/{session_id}/ingestions", status_code=202)
def start_ingestion(
    session_id: str,
    payload: SessionIngestionRequest,
    user: UserDep,
    store: StoreDep,
    s3: S3Dep,
    settings: SettingsDep,
    contracts: ContractServiceDep,
    leases: LeaseServiceDep,
    mutations: MutationServiceDep,
) -> dict[str, object]:
    import json

    session = get_compat_session(store, session_id, user)
    if session.get("phase") not in {"READY_FOR_REVIEW", "READY_FOR_ACKNOWLEDGEMENT"}:
        raise HTTPException(
            409, f"Upload is unavailable while session phase is {session.get('phase')}"
        )
    if not (session.get("preflight") or {}).get("accepted"):
        raise HTTPException(
            422, "The uploaded files did not pass the completed preflight validation"
        )
    if session["table"] == UPLOAD_HISTORY_TABLE:
        raise HTTPException(
            400, "The reserved uploader audit table is not a master-data destination"
        )
    effective_deduplication_mode = payload.deduplication_mode
    effective_deduplication_columns = list(payload.deduplication_columns)
    late_key_activation = False
    contract: dict[str, Any] | None = None
    configured: list[str] = []
    if session["mode"] == "append":
        contract = contracts.load(
            session["table_bucket_arn"], session["namespace"], session["table"]
        )
        configured = contract["deduplication_columns"]
        if configured:
            # The table's first selected key remains immutable, but a later
            # source may expose only a subset. The worker profiled this
            # source; never trust a browser-supplied key.
            effective_deduplication_columns = list(
                (session.get("preflight") or {}).get("deduplication_columns") or []
            )
            effective_deduplication_mode = (
                "keyed" if effective_deduplication_columns else "none"
            )
        elif effective_deduplication_mode == "keyed":
            late_key_activation = True

    if effective_deduplication_mode == "keyed" and not (
        session["mode"] == "append" and configured
    ):
        impact = session.get("key_impact") or {}
        if (
            payload.key_analysis_token != impact.get("token")
            or effective_deduplication_columns != impact.get("deduplication_columns")
        ):
            raise HTTPException(
                422, "Run and acknowledge composite-key analysis before keyed ingestion"
            )
        expires_at = impact.get("expires_at")
        if not expires_at or datetime.fromisoformat(expires_at) <= datetime.now(timezone.utc):
            raise HTTPException(
                422, "The composite-key analysis acknowledgement has expired; run it again"
            )
    allowed_manual = {
        item["column"]
        for item in (session.get("preflight") or {})
        .get("sanitization_review", {})
        .get("manual_encryption_candidates", [])
    }
    invalid_manual = sorted(set(payload.manual_encryption_columns) - allowed_manual)
    if invalid_manual:
        raise HTTPException(
            422, f"Manual encryption is not available for: {', '.join(invalid_manual)}"
        )
    sources: list[JobSource] = []
    for source in session["files"]:
        if not source.get("source_version_id"):
            head = s3.head_object(Bucket=settings.landing_bucket, Key=source["source_key"])
            source["source_version_id"] = head.get("VersionId")
        if not source.get("source_version_id"):
            raise HTTPException(500, "LANDING_BUCKET_VERSIONING_REQUIRED")
        sources.append(
            JobSource(
                name=source["name"],
                source_key=source["source_key"],
                source_version_id=source["source_version_id"],
                source_sha256=source["sha256"],
                source_size_bytes=source["size_bytes"],
            )
        )
    first_source = sources[0]
    # Irreversible browser-reset boundary. Acquired after validation but
    # before any contract mutation, durable job record, queue message, or
    # Glue side effect.
    leases.lock_for_ingestion(session, user.user_id)
    if late_key_activation and contract is not None:
        contracts.activate_late_deduplication(
            table_bucket_arn=session["table_bucket_arn"],
            namespace=session["namespace"],
            table=session["table"],
            contract=contract,
            columns=effective_deduplication_columns,
            user_id=user.user_id,
        )
    job_id = str(uuid.uuid4())
    upload_id = new_upload_id()
    job = JobRequest(
        job_id=job_id,
        session_id=session_id,
        owner_user_id=user.user_id,
        operation=session["mode"],
        destination=Destination(
            table_bucket_arn=session["table_bucket_arn"],
            namespace=session["namespace"],
            table=session["table"],
        ),
        source_key=first_source.source_key,
        source_version_id=first_source.source_version_id,
        source_sha256=first_source.source_sha256,
        source_size_bytes=first_source.source_size_bytes,
        source_files=sources,
        upload_id=upload_id,
        reporting_month=payload.reporting_month,
        deduplication_mode=effective_deduplication_mode,
        deduplication_columns=effective_deduplication_columns,
        manual_encryption_columns=payload.manual_encryption_columns,
    )
    store.put_request(job)
    store.put_mutation_command(
        MutationCommand(
            mutation_id=job_id,
            request_id=payload.request_id,
            owner_user_id=user.user_id,
            operation=job.operation,
            destination=job.destination,
            upload_id=job.upload_id,
            source_job_id=job_id,
            reporting_month=job.reporting_month,
            filenames_json=json.dumps([item.name for item in job.source_files]),
        )
    )
    store.put_status(
        JobStatus(
            job_id=job_id,
            phase="QUEUED",
            message="Queued for preparation and per-table FIFO Glue dispatch.",
        )
    )
    mutations.enqueue(job_id, job.destination)
    ingestion = {
        "request_id": payload.request_id,
        "job_id": job_id,
        "upload_id": upload_id,
        "operation": "ingestion",
        "state": "QUEUED",
        "job_run_id": None,
        "qc_uri": f"s3://{settings.landing_bucket}/{settings.landing_prefix}/qc/{job_id}.json",
    }
    save_compat_session(
        store,
        session,
        phase="QUEUED",
        progress_message="Queued for preparation and per-table FIFO Glue dispatch.",
        ingestion=ingestion,
    )
    return {"session_id": session_id, "job_id": job_id, "phase": "QUEUED"}


# ---------------------------------------------------------------------------
# Compat multipart-form upload dispatcher
# (implementation lives in _compat_upload.py to keep this file readable)
# ---------------------------------------------------------------------------

from ._compat_upload import create_compat_session as _create_compat_session  # noqa: E402

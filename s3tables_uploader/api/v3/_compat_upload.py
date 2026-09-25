"""Multipart-form upload handler for the compat upload-session route.

Lives in its own module so ``upload_sessions.py`` stays focused on the
route surface. Called from ``POST /api/v3/upload-sessions`` when the
request's ``Content-Type`` is ``multipart/form-data``. Streams each file
to S3 in bounded 8 MiB chunks; the data never sits in application memory
long enough to fill the container.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request
from fastapi.concurrency import run_in_threadpool

from ...core.constants import S3_SSE, SUPPORTED_COMPAT_SUFFIXES, UPLOAD_PART_BYTES
from ...core.exceptions import UploaderError
from ...job_store import MissingRecord, S3JobStore
from ...protocols.auth import UserContext
from ...services.leases import LeaseService
from ...services.tables import TableBucketService
from ...utils.time import now_iso
from ._common import require_table_bucket_access
from ._compat import delete_raw_source_versions, safe_compat_session_response


def _safe_upload_name(name: str) -> str:
    value = Path(name).name
    if not value or value in {".", ".."}:
        raise HTTPException(400, "Each uploaded file must have a filename")
    return value


async def create_compat_session(
    request: Request,
    user: UserContext,
    settings,
    store: S3JobStore,
    s3: Any,
    tables: TableBucketService,
    leases: LeaseService,
) -> dict[str, Any]:
    """Persist a v1 form upload directly to S3 with bounded 8 MiB chunks.

    FastAPI may spool the request body to Fargate ephemeral storage, but
    this function never materialises a data file in application memory and
    never leaves its durable copy on the task filesystem.
    """
    form = await request.form()
    mode = str(form.get("mode", ""))
    table_bucket_arn = str(form.get("table_bucket_arn", ""))
    namespace = str(form.get("namespace", ""))
    table = str(form.get("table", ""))
    lease_id = str(form.get("worker_lease_id", "")).strip() or None
    if mode not in {"create", "append"}:
        raise HTTPException(422, "mode must be create or append")
    require_table_bucket_access(table_bucket_arn, user, tables)
    if not namespace or not table:
        raise HTTPException(422, "namespace and table are required")
    uploads = [
        item for item in form.getlist("files")
        if hasattr(item, "filename") and hasattr(item, "file")
    ]
    if not uploads:
        raise HTTPException(400, "Choose at least one Parquet file")
    invalid = [
        str(upload.filename or "<unnamed>")
        for upload in uploads
        if not (upload.filename or "").lower().endswith(SUPPORTED_COMPAT_SUFFIXES)
    ]
    if invalid:
        raise HTTPException(400, "Supported files are Parquet, Parquet GZIP, XLSX, XLS, CSV, and TSV")

    session_id = uuid.uuid4().hex
    received_at = now_iso()
    files: list[dict[str, Any]] = []
    session_recorded = False
    try:
        for number, upload in enumerate(uploads):
            name = _safe_upload_name(upload.filename or "")
            key = f"{settings.landing_prefix}/uploads/{session_id}/raw/{number:02d}-{name}"
            multipart = await run_in_threadpool(
                s3.create_multipart_upload,
                Bucket=settings.landing_bucket,
                Key=key,
                ContentType=upload.content_type or "application/octet-stream",
                ServerSideEncryption=S3_SSE,
                Metadata={"session-id": session_id, "owner-user-id": user.user_id},
            )
            digest = hashlib.sha256()
            parts: list[dict[str, Any]] = []
            size = 0
            part_number = 1
            try:
                while chunk := await run_in_threadpool(upload.file.read, UPLOAD_PART_BYTES):
                    digest.update(chunk)
                    size += len(chunk)
                    part = await run_in_threadpool(
                        s3.upload_part,
                        Bucket=settings.landing_bucket,
                        Key=key,
                        UploadId=multipart["UploadId"],
                        PartNumber=part_number,
                        Body=chunk,
                    )
                    parts.append({"ETag": part["ETag"], "PartNumber": part_number})
                    part_number += 1
                if not parts:
                    raise HTTPException(400, f"{name} is empty")
                completed = await run_in_threadpool(
                    s3.complete_multipart_upload,
                    Bucket=settings.landing_bucket,
                    Key=key,
                    UploadId=multipart["UploadId"],
                    MultipartUpload={"Parts": parts},
                )
            except (Exception, asyncio.CancelledError):
                await run_in_threadpool(
                    s3.abort_multipart_upload,
                    Bucket=settings.landing_bucket,
                    Key=key,
                    UploadId=multipart["UploadId"],
                )
                raise
            files.append(
                {
                    "name": name,
                    "sha256": digest.hexdigest(),
                    "size_bytes": size,
                    "source_key": key,
                    "source_version_id": completed.get("VersionId"),
                }
            )
        session = {
            "schema_version": 1,
            "session_id": session_id,
            "owner_user_id": user.user_id,
            "mode": mode,
            "table_bucket_arn": table_bucket_arn,
            "namespace": namespace,
            "table": table,
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=60)).isoformat(),
            "files": files,
            "phase": "RECEIVED",
            "progress_message": "Files are stored in S3; waiting for isolated worker profiling.",
            "error": None,
            "preflight": None,
            "key_impact": None,
            "ingestion": None,
            "phase_timings_ms": {},
            "created_at": received_at,
            "updated_at": now_iso(),
            "phase_started_at": now_iso(),
        }
        if lease_id is None:
            lease = leases.new_lease(files, user.user_id)
        else:
            try:
                lease = store.get_lease(lease_id)
            except MissingRecord:
                lease = leases.new_lease(files, user.user_id)
            if lease.get("owner_user_id") != user.user_id:
                # Browser identity may have changed while a previous
                # file-selection lease remains in memory. Never bind
                # another user's lease; start a new owner-scoped one.
                lease = leases.new_lease(files, user.user_id)
            expected = [(item["name"], int(item["size_bytes"])) for item in lease.get("files", [])]
            received = [(item["name"], int(item["size_bytes"])) for item in files]
            if lease.get("state") == "CANCELLED":
                raise HTTPException(409, "WORKER_LEASE_UNAVAILABLE")
            if (
                lease.get("state") in {"EXPIRED", "COMPLETED", "RESOURCE_LIMIT_EXCEEDED"}
                or expected != received
            ):
                lease = leases.new_lease(files, user.user_id)
        session["worker_lease_id"] = lease["lease_id"]
        store.put_compat_session(session, create_only=True)
        session_recorded = True
        # Lease is already starting; bind it to the persisted session.
        try:
            lease = leases.bind(str(session["worker_lease_id"]), user.user_id, session)
        except UploaderError as error:
            if error.error_code != "WORKER_LEASE_UNAVAILABLE":
                raise
            lease = leases.new_lease(files, user.user_id)
            session["worker_lease_id"] = lease["lease_id"]
            store.put_compat_session(session)
            lease = leases.bind(str(session["worker_lease_id"]), user.user_id, session)
        return safe_compat_session_response(store, session, leases)
    except BaseException as error:
        if files:
            if session_recorded:
                try:
                    store.update_compat_session(
                        session_id,
                        {
                            "phase": "DELETED",
                            "phase_started_at": now_iso(),
                            "updated_at": now_iso(),
                            "progress_message": "Upload receipt cancelled before worker attachment.",
                            "cleanup_pending": True,
                        },
                    )
                except Exception:
                    pass
            try:
                await run_in_threadpool(delete_raw_source_versions, store, {"files": files})
            except Exception:
                pass
        if isinstance(error, HTTPException):
            raise
        raise HTTPException(422, f"Unable to read the uploaded Parquet file: {error}") from error
    finally:
        for upload in uploads:
            await upload.close()

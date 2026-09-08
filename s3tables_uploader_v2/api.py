"""Lightweight Fargate control plane; all large-file work belongs to workers."""

from __future__ import annotations

import hashlib
import uuid
from typing import Any, Literal

import boto3
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from .auth import COOKIE_NAME, login_cookie, require_user, valid_password
from .config import Settings
from .job_store import MissingRecord, S3JobStore
from .models import Destination, JobRequest, JobStatus, UploadSession


class LoginRequest(BaseModel):
    password: str


class CreateSessionRequest(BaseModel):
    file_name: str = Field(min_length=1, max_length=512)
    content_type: str = Field(min_length=1, max_length=255)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class CompleteSessionRequest(BaseModel):
    parts: list[dict[str, Any]] = Field(min_length=1)
    operation: Literal["create", "append"]
    destination: Destination


class PartUrlRequest(BaseModel):
    part_number: int = Field(ge=1, le=10_000)


def create_app(settings: Settings, s3_client: Any | None = None, sqs_client: Any | None = None) -> FastAPI:
    s3 = s3_client or boto3.client("s3", region_name=settings.region)
    sqs = sqs_client or boto3.client("sqs", region_name=settings.region)
    store = S3JobStore(s3, settings.landing_bucket, settings.landing_prefix)
    app = FastAPI(title="S3 Uploader v2", docs_url=None, redoc_url=None)

    def current_user(request: Request) -> str:
        return require_user(request, settings)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    def landing() -> str:
        return """<!doctype html><html><head><title>S3 Uploader v2</title></head>
<body><h1>S3 Uploader v2</h1><p>Service is ready.</p>
<p>The upload UI will be integrated here; the authenticated API is available under <code>/api/v2</code>.</p></body></html>"""

    @app.post("/login")
    def login(payload: LoginRequest, response: Response) -> dict[str, bool]:
        if not valid_password(payload.password, settings):
            raise HTTPException(401, "LOGIN_FAILED")
        response.set_cookie(
            COOKIE_NAME,
            login_cookie(settings),
            httponly=True,
            secure=settings.cookie_secure,
            samesite="strict",
            max_age=settings.session_ttl_seconds,
            path="/",
        )
        return {"authenticated": True}

    @app.post("/logout")
    def logout(response: Response) -> dict[str, bool]:
        response.delete_cookie(COOKIE_NAME, path="/")
        return {"authenticated": False}

    @app.post("/api/v2/upload-sessions", status_code=201)
    def create_session(payload: CreateSessionRequest, user_id: str = Depends(current_user)) -> dict[str, str]:
        session_id = str(uuid.uuid4())
        key = f"{settings.landing_prefix}/uploads/{session_id}/raw/{payload.file_name}"
        response = s3.create_multipart_upload(
            Bucket=settings.landing_bucket,
            Key=key,
            ContentType=payload.content_type,
            ServerSideEncryption="aws:kms",
            Metadata={"session-id": session_id, "owner-user-id": user_id, "sha256": payload.source_sha256},
        )
        session = UploadSession(
            session_id=session_id,
            owner_user_id=user_id,
            file_name=payload.file_name,
            content_type=payload.content_type,
            source_key=key,
            multipart_upload_id=response["UploadId"],
            expected_sha256=payload.source_sha256,
        )
        store.put_session(session)
        return {"session_id": session_id, "upload_id": session.multipart_upload_id, "source_key": session.source_key}

    @app.post("/api/v2/upload-sessions/{session_id}/parts")
    def create_part_url(session_id: str, payload: PartUrlRequest, user_id: str = Depends(current_user)) -> dict[str, str]:
        try:
            session = store.get_session(session_id)
        except MissingRecord as error:
            raise HTTPException(404, "UPLOAD_SESSION_NOT_FOUND") from error
        if session.owner_user_id != user_id:
            raise HTTPException(403, "UPLOAD_SESSION_FORBIDDEN")
        url = s3.generate_presigned_url(
            "upload_part",
            Params={"Bucket": settings.landing_bucket, "Key": session.source_key, "UploadId": session.multipart_upload_id, "PartNumber": payload.part_number},
            ExpiresIn=900,
            HttpMethod="PUT",
        )
        return {"url": url}

    @app.post("/api/v2/upload-sessions/{session_id}/complete", status_code=202)
    def complete_session(session_id: str, payload: CompleteSessionRequest, user_id: str = Depends(current_user)) -> dict[str, str]:
        try:
            session = store.get_session(session_id)
        except MissingRecord as error:
            raise HTTPException(404, "UPLOAD_SESSION_NOT_FOUND") from error
        if session.owner_user_id != user_id:
            raise HTTPException(403, "UPLOAD_SESSION_FORBIDDEN")
        s3.complete_multipart_upload(
            Bucket=settings.landing_bucket,
            Key=session.source_key,
            UploadId=session.multipart_upload_id,
            MultipartUpload={"Parts": payload.parts},
        )
        source = s3.head_object(Bucket=settings.landing_bucket, Key=session.source_key)
        if S3JobStore.object_sha256(source) != session.expected_sha256:
            raise HTTPException(409, "UPLOAD_CHECKSUM_MISMATCH")
        version_id = source.get("VersionId")
        if not version_id:
            raise HTTPException(500, "LANDING_BUCKET_VERSIONING_REQUIRED")
        job_id = str(uuid.uuid4())
        job = JobRequest(
            job_id=job_id,
            session_id=session_id,
            owner_user_id=user_id,
            operation=payload.operation,
            destination=payload.destination,
            source_key=session.source_key,
            source_version_id=version_id,
            source_sha256=session.expected_sha256,
            source_size_bytes=source["ContentLength"],
        )
        store.put_request(job)
        store.put_status(JobStatus(job_id=job_id, phase="QUEUED", message="Upload completed; waiting for processing."))
        group = hashlib.sha256(
            f"{job.destination.table_bucket_arn}\x1f{job.destination.namespace}\x1f{job.destination.table}".encode("utf-8")
        ).hexdigest()
        sqs.send_message(QueueUrl=settings.queue_url, MessageBody=job_id, MessageDeduplicationId=job_id, MessageGroupId=group)
        return {"job_id": job_id, "phase": "QUEUED"}

    @app.get("/api/v2/jobs/{job_id}")
    def get_job(job_id: str, user_id: str = Depends(current_user)) -> dict[str, Any]:
        try:
            request = store.get_request(job_id)
            status = store.get_status(job_id).status
        except MissingRecord as error:
            raise HTTPException(404, "JOB_NOT_FOUND") from error
        if request.owner_user_id != user_id:
            raise HTTPException(403, "JOB_FORBIDDEN")
        return {"request": request.model_dump(mode="json"), "status": status.model_dump(mode="json")}

    return app

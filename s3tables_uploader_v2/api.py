"""Lightweight Fargate control plane; all large-file work belongs to workers."""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from typing import Any, Literal

import boto3
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from .auth import COOKIE_NAME, login_cookie, require_user, valid_password
from .config import Settings
from .job_store import MissingRecord, S3JobStore
from .models import Destination, JobRequest, JobStatus, UploadSession
from . import skill_bundle


ALLOWED_TABLE_BUCKET_ARNS = {
    "arn:aws:s3tables:ap-southeast-1:964340114883:bucket/ah-analytics",
    "arn:aws:s3tables:ap-southeast-1:964340114883:bucket/ah-soc-delta-pilot",
    "arn:aws:s3tables:ap-southeast-1:964340114883:bucket/nuh-analytics",
}


class LoginRequest(BaseModel):
    password: str


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


def create_app(settings: Settings, s3_client: Any | None = None, sqs_client: Any | None = None) -> FastAPI:
    s3 = s3_client or boto3.client("s3", region_name=settings.region)
    sqs = sqs_client or boto3.client("sqs", region_name=settings.region)
    s3tables = boto3.client("s3tables", region_name=settings.region)
    store = S3JobStore(s3, settings.landing_bucket, settings.landing_prefix)
    app = FastAPI(title="S3 Uploader v2", docs_url=None, redoc_url=None)
    static_root = Path(__file__).parent / "static"

    def current_user(request: Request) -> str:
        return require_user(request, settings)

    @app.middleware("http")
    async def browser_login_gate(request: Request, call_next: Any) -> Response:
        if request.url.path in {"/login", "/healthz"}:
            return await call_next(request)
        try:
            require_user(request, settings)
        except HTTPException:
            if request.url.path.startswith("/api/"):
                return JSONResponse(status_code=401, content={"code": "LOGIN_REQUIRED", "detail": "Log in before using the uploader API."})
            return RedirectResponse(url="/login", status_code=303)
        return await call_next(request)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/dev/identity-profiles")
    def identity_profiles() -> dict[str, Any]:
        return {"local_only": True, "header_name": "X-Pilot-User-Id", "profiles": [{"user_id": "shared-operator", "is_admin": True, "can_view_upload_history": True, "can_rollback_uploads": True, "buckets": [], "expected_access": True}]}

    @app.get("/api/identity")
    def identity(user_id: str = Depends(current_user)) -> dict[str, Any]:
        return {"user_id": user_id, "is_admin": True, "can_view_upload_history": True, "can_rollback_uploads": True, "scope_mode": "three-approved-table-buckets", "buckets": [{"table_bucket_arn": arn, "namespace": "*", "label": arn.rsplit("/", 1)[-1]} for arn in sorted(ALLOWED_TABLE_BUCKET_ARNS)], "request_context": {"header_name": "X-Pilot-User-Id", "header_value": user_id, "roles_and_grants_sent_by_browser": False}}

    @app.get("/api/buckets")
    def buckets(user_id: str = Depends(current_user)) -> dict[str, Any]:
        result = s3tables.list_table_buckets()
        approved = [{"table_bucket_arn": item["arn"], "label": item["name"]} for item in result.get("tableBuckets", []) if item["arn"] in ALLOWED_TABLE_BUCKET_ARNS]
        return {"user_id": user_id, "is_admin": True, "can_view_upload_history": True, "can_rollback_uploads": True, "buckets": approved}

    @app.get("/api/namespaces")
    def namespaces(table_bucket_arn: str, _: str = Depends(current_user)) -> dict[str, Any]:
        if table_bucket_arn not in ALLOWED_TABLE_BUCKET_ARNS:
            raise HTTPException(403, "TABLE_BUCKET_FORBIDDEN")
        return {"table_bucket_arn": table_bucket_arn, "namespaces": [item["namespace"][0] for item in s3tables.list_namespaces(tableBucketARN=table_bucket_arn).get("namespaces", [])]}

    @app.get("/api/tables")
    def tables(table_bucket_arn: str, namespace: str, _: str = Depends(current_user)) -> dict[str, Any]:
        if table_bucket_arn not in ALLOWED_TABLE_BUCKET_ARNS:
            raise HTTPException(403, "TABLE_BUCKET_FORBIDDEN")
        rows = [{"name": item["name"], "created_at": str(item.get("createdAt")), "modified_at": str(item.get("modifiedAt")), "row_count": None, "uploader_managed": True} for item in s3tables.list_tables(tableBucketARN=table_bucket_arn, namespace=namespace).get("tables", [])]
        return {"table_bucket": table_bucket_arn, "namespace": namespace, "is_admin": True, "tables": sorted(rows, key=lambda item: item["name"])}

    @app.get("/api/skills/files")
    def skill_files(table_bucket_arn: str, _: str = Depends(current_user)) -> dict[str, Any]:
        if table_bucket_arn not in ALLOWED_TABLE_BUCKET_ARNS:
            raise HTTPException(403, "TABLE_BUCKET_FORBIDDEN")
        try:
            return skill_bundle.list_skill_files(table_bucket_arn)
        except skill_bundle.SkillBundleError as error:
            raise HTTPException(error.status_code, str(error)) from error

    @app.get("/")
    def landing() -> FileResponse:
        return FileResponse(static_root / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/static/{asset}")
    def static_asset(asset: Literal["app.js", "style.css"]) -> FileResponse:
        return FileResponse(static_root / asset, headers={"Cache-Control": "no-store"})

    @app.get("/login")
    def login_form() -> HTMLResponse:
        return HTMLResponse("<!doctype html><title>S3 Uploader v2 login</title><form method='post'><label>Password <input name='password' type='password' autofocus></label><button>Sign in</button></form>")

    @app.post("/login")
    async def login(request: Request) -> Response:
        is_json = request.headers.get("content-type", "").startswith("application/json")
        payload = LoginRequest.model_validate(await request.json()) if is_json else LoginRequest(password=str((await request.form()).get("password", "")))
        if not valid_password(payload.password, settings):
            if is_json:
                raise HTTPException(401, "LOGIN_FAILED")
            return HTMLResponse("<!doctype html><p>Invalid password.</p><a href='/login'>Try again</a>", status_code=401)
        response: Response = JSONResponse({"authenticated": True}) if is_json else RedirectResponse(url="/", status_code=303)
        response.set_cookie(
            COOKIE_NAME,
            login_cookie(settings),
            httponly=True,
            secure=settings.cookie_secure,
            samesite="strict",
            max_age=settings.session_ttl_seconds,
            path="/",
        )
        return response

    @app.post("/logout")
    def logout(request: Request, response: Response) -> Response:
        response = JSONResponse({"authenticated": False}) if request.headers.get("accept", "").startswith("application/json") else RedirectResponse(url="/login", status_code=303)
        response.delete_cookie(COOKIE_NAME, path="/")
        return response

    @app.post("/api/v2/upload-sessions", status_code=201)
    def create_session(payload: CreateSessionRequest, user_id: str = Depends(current_user)) -> dict[str, str]:
        session_id = str(uuid.uuid4())
        key = f"{settings.landing_prefix}/uploads/{session_id}/raw/{payload.file_name}"
        response = s3.create_multipart_upload(
            Bucket=settings.landing_bucket,
            Key=key,
            ContentType=payload.content_type,
            ServerSideEncryption="aws:kms",
            Metadata={"session-id": session_id, "owner-user-id": user_id, **({"sha256": payload.source_sha256} if payload.source_sha256 else {})},
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
        if session.expected_sha256 and S3JobStore.object_sha256(source) != session.expected_sha256:
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

"""Lightweight Fargate control plane; all large-file work belongs to workers."""

from __future__ import annotations

import asyncio
import hashlib
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

import boto3
import pyarrow as pa
import pyarrow.parquet as pq
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from .auth import COOKIE_NAME, login_cookie, require_user, valid_password
from .config import Settings
from .job_store import MissingRecord, S3JobStore
from .models import Destination, JobRequest, JobStatus, UploadSession
from .sanitization import sanitised_schema
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


_SUPPORTED_COMPAT_SUFFIXES = (".parquet", ".parquet.gzip")
_UPLOAD_PART_BYTES = 8 * 1024 * 1024


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_upload_name(name: str) -> str:
    value = Path(name).name
    if not value or value in {".", ".."}:
        raise HTTPException(400, "Each uploaded file must have a filename")
    return value


def _schema_from_arrow(schema: pa.Schema) -> tuple[list[dict[str, str]], list[str]]:
    """Small dependency-free schema projection used by the API review path."""
    used: set[str] = set(); fields: list[dict[str, str]] = []; warnings: list[str] = []
    for field in schema:
        base = re.sub(r"_+", "_", re.sub(r"[ /()\\-]", "_", field.name)).strip("_").lower()
        if not base:
            raise ValueError(f"Column name normalises to an empty value: {field.name!r}")
        name = base; index = 1
        while name in used:
            name = f"{base}_{index:02d}"; index += 1
        used.add(name)
        if pa.types.is_boolean(field.type): data_type = "BOOLEAN"
        elif pa.types.is_integer(field.type): data_type = "BIGINT"
        elif pa.types.is_floating(field.type) or pa.types.is_decimal(field.type): data_type = "DOUBLE"
        elif pa.types.is_timestamp(field.type): data_type = "TIMESTAMP"
        elif pa.types.is_date(field.type): data_type = "DATE"
        else:
            data_type = "STRING"
            if not (pa.types.is_string(field.type) or pa.types.is_large_string(field.type) or pa.types.is_binary(field.type) or pa.types.is_large_binary(field.type)):
                warnings.append(f"{field.name} ({field.type}) will be stored as STRING")
        fields.append({"name": name, "type": data_type, "source_name": field.name})
    return fields, warnings


def _compat_preflight(*, files: list[dict[str, Any]], mode: str, table_bucket_arn: str, namespace: str, table: str) -> dict[str, Any]:
    """Return the v1 browser's review shape without reading full Parquet data.

    This is deliberately metadata-only.  The Fargate worker remains the sole
    place that reads batches, sanitises values, and starts Glue.
    """
    target_schema: list[dict[str, str]] = []
    file_results: list[dict[str, Any]] = []
    reasons: list[str] = []
    for item in files:
        schema = item["schema"]
        source_fields, warnings = _schema_from_arrow(schema)
        sanitized_schema, plan = sanitised_schema(schema)
        output_fields, output_warnings = _schema_from_arrow(sanitized_schema)
        if not target_schema:
            target_schema = output_fields
        source_names = {field["name"] for field in source_fields}
        target_names = {field["name"] for field in target_schema}
        matching = len(source_names & target_names)
        percentage = 100.0 if not target_names else matching * 100.0 / len(target_names)
        accepted = mode == "create" or percentage >= 50.0
        file_reasons: list[str] = []
        if not accepted:
            file_reasons.append(f"{item['name']}: only {percentage:.1f}% of the proposed target columns match; at least 50.0% is required for an append.")
            reasons.extend(file_reasons)
        file_results.append({
            "filename": item["name"],
            "target_column_count": len(target_schema),
            "source_column_count": len(source_fields),
            "matching_column_count": matching,
            "matching_percentage": percentage,
            "source_schema": source_fields,
            "warnings": warnings + output_warnings,
            "sanitization": {
                "dropped_columns": list(plan.drop_columns),
                "encrypted_columns": list(plan.identifier_columns),
                "postal_columns": list(plan.postal_columns),
                "age_banded_columns": list(plan.age_columns),
            },
            "nric_detection": {}, "nric_detected_columns": [],
            "sanitized_columns": sorted(set(plan.drop_columns + plan.identifier_columns + plan.postal_columns + plan.age_columns)),
            "sanitized_column_count": len(set(plan.drop_columns + plan.identifier_columns + plan.postal_columns + plan.age_columns)),
            "unsafe_casts": [], "temporal_coercions": [], "accepted": accepted,
            "rejection_reasons": file_reasons,
        })
    candidates = [{"column": field["name"], "target_type": field["type"], "source_type": field["type"], "sample_values": [], "samples_masked": True, "non_null_count": 0, "deduplication_eligible": True} for field in target_schema]
    return {
        "mode": mode, "table_bucket_arn": table_bucket_arn, "namespace": namespace, "table": table,
        "target_schema": target_schema, "creation_warnings": [], "initial_table_column_count": len(target_schema),
        "minimum_append_schema_match_percent": 50.0, "files": file_results, "type_selections": [],
        "deduplication_candidates": candidates, "deduplication_columns": [], "deduplication_policy": "none",
        "contract_fingerprint": None, "temporal_policy_adoption": None, "phase_timings_ms": {},
        "incompatible_sensitive_columns": [], "accepted": not reasons, "rejection_reasons": reasons,
        "sensitive_column_scan": "Sanitization is enforced in the isolated worker before S3 staging.",
        "sanitization_review": {"automatic_encrypted_columns": [], "manual_encryption_candidates": [], "nric_detection_policy": {"sample_size": 5, "match_threshold": 3, "kind": "worker-validated"}},
    }


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
    async def create_session(request: Request, user_id: str = Depends(current_user)) -> dict[str, Any]:
        """Accept both the v2 direct-S3 JSON protocol and immutable v1 multipart UI."""
        if request.headers.get("content-type", "").lower().startswith("multipart/form-data"):
            return await create_compat_session(request, user_id)
        try:
            payload = CreateSessionRequest.model_validate(await request.json())
        except Exception as error:
            raise HTTPException(422, "Expected the v2 JSON upload protocol or a v1 multipart upload form") from error
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

    async def create_compat_session(request: Request, user_id: str) -> dict[str, Any]:
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
        if mode not in {"create", "append"}:
            raise HTTPException(422, "mode must be create or append")
        if table_bucket_arn not in ALLOWED_TABLE_BUCKET_ARNS:
            raise HTTPException(403, "TABLE_BUCKET_FORBIDDEN")
        if not namespace or not table:
            raise HTTPException(422, "namespace and table are required")
        uploads = [item for item in form.getlist("files") if hasattr(item, "filename") and hasattr(item, "file")]
        if not uploads:
            raise HTTPException(400, "Choose at least one Parquet file")
        invalid = [str(upload.filename or "<unnamed>") for upload in uploads if not (upload.filename or "").lower().endswith(_SUPPORTED_COMPAT_SUFFIXES)]
        if invalid:
            raise HTTPException(400, "The Fargate compatibility path currently accepts Parquet and Parquet GZIP files only")

        session_id = uuid.uuid4().hex
        received_at = _now()
        files: list[dict[str, Any]] = []
        profile_inputs: list[dict[str, Any]] = []
        try:
            for number, upload in enumerate(uploads):
                name = _safe_upload_name(upload.filename or "")
                # Parquet footer/schema is read from the spooled upload only;
                # it does not scan data pages or put file contents in memory.
                schema = await asyncio.to_thread(lambda source=upload.file: pq.ParquetFile(source).schema_arrow)
                await asyncio.to_thread(upload.file.seek, 0)
                key = f"{settings.landing_prefix}/uploads/{session_id}/raw/{number:02d}-{name}"
                multipart = await asyncio.to_thread(
                    s3.create_multipart_upload, Bucket=settings.landing_bucket, Key=key,
                    ContentType=upload.content_type or "application/octet-stream", ServerSideEncryption="aws:kms",
                    Metadata={"session-id": session_id, "owner-user-id": user_id},
                )
                digest = hashlib.sha256(); parts: list[dict[str, Any]] = []; size = 0; part_number = 1
                try:
                    while chunk := await asyncio.to_thread(upload.file.read, _UPLOAD_PART_BYTES):
                        digest.update(chunk); size += len(chunk)
                        part = await asyncio.to_thread(s3.upload_part, Bucket=settings.landing_bucket, Key=key, UploadId=multipart["UploadId"], PartNumber=part_number, Body=chunk)
                        parts.append({"ETag": part["ETag"], "PartNumber": part_number}); part_number += 1
                    if not parts:
                        raise HTTPException(400, f"{name} is empty")
                    completed = await asyncio.to_thread(s3.complete_multipart_upload, Bucket=settings.landing_bucket, Key=key, UploadId=multipart["UploadId"], MultipartUpload={"Parts": parts})
                except Exception:
                    await asyncio.to_thread(s3.abort_multipart_upload, Bucket=settings.landing_bucket, Key=key, UploadId=multipart["UploadId"])
                    raise
                files.append({"name": name, "sha256": digest.hexdigest(), "size_bytes": size, "source_key": key, "source_version_id": completed.get("VersionId")})
                profile_inputs.append({"name": name, "schema": schema})
            preflight = _compat_preflight(files=profile_inputs, mode=mode, table_bucket_arn=table_bucket_arn, namespace=namespace, table=table)
            session = {
                "schema_version": 1, "session_id": session_id, "owner_user_id": user_id, "mode": mode,
                "table_bucket_arn": table_bucket_arn, "namespace": namespace, "table": table,
                "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=60)).isoformat(), "files": files,
                "phase": "READY_FOR_REVIEW", "progress_message": "Schema review is complete.", "error": None,
                "preflight": preflight, "key_impact": None, "ingestion": None, "phase_timings_ms": {},
                "created_at": received_at, "updated_at": _now(), "phase_started_at": _now(),
            }
            store.put_compat_session(session, create_only=True)
            return _safe_compat_session(session)
        except HTTPException:
            raise
        except Exception as error:
            raise HTTPException(422, f"Unable to read the uploaded Parquet file: {error}") from error
        finally:
            for upload in uploads:
                await upload.close()

    def _safe_compat_session(session: dict[str, Any]) -> dict[str, Any]:
        value = {**session}
        value["files"] = [{key: item[key] for key in ("name", "sha256", "size_bytes")} for item in session.get("files", [])]
        return value

    def _get_compat_session(session_id: str, user_id: str) -> dict[str, Any]:
        try:
            session = store.get_compat_session(session_id)
        except MissingRecord as error:
            raise HTTPException(404, "The upload session does not exist, belongs to another user, or has expired") from error
        if session.get("owner_user_id") != user_id:
            raise HTTPException(403, "UPLOAD_SESSION_FORBIDDEN")
        if datetime.fromisoformat(session["expires_at"]) <= datetime.now(timezone.utc):
            raise HTTPException(404, "The upload session has expired")
        return session

    def _save_compat_session(session: dict[str, Any], **changes: Any) -> dict[str, Any]:
        now = _now()
        if "phase" in changes and changes["phase"] != session.get("phase"):
            changes["phase_started_at"] = now
        session.update(changes); session["updated_at"] = now
        store.put_compat_session(session)
        return session

    @app.get("/api/v2/upload-sessions/{session_id}")
    def get_compat_session(session_id: str, user_id: str = Depends(current_user)) -> dict[str, Any]:
        session = _get_compat_session(session_id, user_id)
        # A worker owns durable job progress.  Mirror its state into the v1
        # session response so the unchanged browser can reconnect after an API
        # replacement or page refresh.
        job_id = (session.get("ingestion") or {}).get("job_id")
        if job_id:
            try:
                status = store.get_status(job_id).status
                phase_map = {"QUEUED": "QUEUED", "CLAIMED": "QUEUED", "PROFILING": "QUEUED", "PREPARING": "STARTING_GLUE", "STARTING_GLUE": "STARTING_GLUE", "RUNNING_GLUE": "GLUE_RUNNING", "SUCCEEDED": "SUCCEEDED", "FAILED": "FAILED"}
                ingestion = {**(session.get("ingestion") or {}), "state": status.phase, "job_run_id": status.glue_run_id, "qc_uri": f"s3://{settings.landing_bucket}/{settings.landing_prefix}/qc/{job_id}.json"}
                changes: dict[str, Any] = {"phase": phase_map[status.phase], "progress_message": status.message, "ingestion": ingestion}
                if status.phase == "FAILED":
                    changes["error"] = {"code": status.error_code or "WORKER_FAILED", "message": status.message}
                session = _save_compat_session(session, **changes)
            except MissingRecord:
                pass
        return _safe_compat_session(session)

    @app.delete("/api/v2/upload-sessions/{session_id}", status_code=204)
    def delete_compat_session(session_id: str, user_id: str = Depends(current_user)) -> Response:
        session = _get_compat_session(session_id, user_id)
        _save_compat_session(session, phase="DELETED", progress_message="Session deleted.")
        return Response(status_code=204)

    @app.post("/api/v2/upload-sessions/{session_id}/key-impact", status_code=202)
    def key_impact(session_id: str, payload: SessionKeyImpactRequest, user_id: str = Depends(current_user)) -> dict[str, Any]:
        session = _get_compat_session(session_id, user_id)
        if session.get("phase") not in {"READY_FOR_REVIEW", "READY_FOR_ACKNOWLEDGEMENT"}:
            raise HTTPException(409, f"Key-impact analysis is unavailable while session phase is {session.get('phase')}")
        columns = payload.deduplication_columns
        known = {item["column"] for item in (session.get("preflight") or {}).get("deduplication_candidates", [])}
        unknown = sorted(set(columns) - known)
        if unknown:
            raise HTTPException(422, f"Unknown de-duplication columns: {', '.join(unknown)}")
        # Exact duplicate metrics require a bounded worker scan.  We never
        # report invented figures: this explicit review token records the
        # immutable user selection, and the worker enforces it before Glue.
        token = uuid.uuid4().hex
        impact = {"token": token, "acknowledgement_token": token, "deduplication_columns": columns,
                  "metrics": {"incoming_rows": 0, "unique_composite_keys": 0, "exact_duplicate_rows": 0, "conflicting_composite_keys": 0, "expected_retained_rows": 0, "expected_skipped_rows": 0},
                  "message": "Key selection recorded; exact row metrics will be calculated by the isolated worker before ingestion."}
        _save_compat_session(session, phase="READY_FOR_ACKNOWLEDGEMENT", progress_message="Composite-key choice recorded; acknowledge it before upload.", key_impact=impact)
        return {"session_id": session_id, "phase": "READY_FOR_ACKNOWLEDGEMENT", "key_impact": impact}

    @app.post("/api/v2/upload-sessions/{session_id}/ingestions", status_code=202)
    def start_compat_ingestion(session_id: str, payload: SessionIngestionRequest, user_id: str = Depends(current_user)) -> dict[str, Any]:
        session = _get_compat_session(session_id, user_id)
        if session.get("phase") not in {"READY_FOR_REVIEW", "READY_FOR_ACKNOWLEDGEMENT"}:
            raise HTTPException(409, f"Upload is unavailable while session phase is {session.get('phase')}")
        if not (session.get("preflight") or {}).get("accepted"):
            raise HTTPException(422, "The uploaded files did not pass the completed preflight validation")
        if len(session.get("files", [])) != 1:
            raise HTTPException(422, "Multiple-file ingestion will be rejected until all files share one worker manifest; submit one file per session for now.")
        if payload.deduplication_mode == "keyed":
            impact = session.get("key_impact") or {}
            if payload.key_analysis_token != impact.get("token") or payload.deduplication_columns != impact.get("deduplication_columns"):
                raise HTTPException(422, "Run and acknowledge composite-key analysis before keyed ingestion")
        source = session["files"][0]
        if not source.get("source_version_id"):
            head = s3.head_object(Bucket=settings.landing_bucket, Key=source["source_key"])
            source["source_version_id"] = head.get("VersionId")
        if not source.get("source_version_id"):
            raise HTTPException(500, "LANDING_BUCKET_VERSIONING_REQUIRED")
        job_id = str(uuid.uuid4())
        job = JobRequest(job_id=job_id, session_id=session_id, owner_user_id=user_id, operation=session["mode"],
                         destination=Destination(table_bucket_arn=session["table_bucket_arn"], namespace=session["namespace"], table=session["table"]),
                         source_key=source["source_key"], source_version_id=source["source_version_id"],
                         source_sha256=source["sha256"], source_size_bytes=source["size_bytes"])
        store.put_request(job); store.put_status(JobStatus(job_id=job_id, phase="QUEUED", message="Upload queued for the isolated Fargate worker."))
        group = hashlib.sha256(f"{job.destination.table_bucket_arn}\x1f{job.destination.namespace}\x1f{job.destination.table}".encode()).hexdigest()
        sqs.send_message(QueueUrl=settings.queue_url, MessageBody=job_id, MessageDeduplicationId=job_id, MessageGroupId=group)
        ingestion = {"request_id": payload.request_id, "job_id": job_id, "operation": "ingestion", "state": "QUEUED", "job_run_id": None,
                     "qc_uri": f"s3://{settings.landing_bucket}/{settings.landing_prefix}/qc/{job_id}.json"}
        _save_compat_session(session, phase="QUEUED", progress_message="Upload queued for isolated processing.", ingestion=ingestion)
        return {"session_id": session_id, "job_id": job_id, "phase": "QUEUED"}

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

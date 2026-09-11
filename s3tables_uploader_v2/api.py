"""Lightweight Fargate control plane; all large-file work belongs to workers."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Literal
from urllib.parse import quote

import boto3
from botocore.exceptions import ClientError
import pyarrow as pa
import pyarrow.parquet as pq
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field

from .auth import COOKIE_NAME, login_cookie, require_user, valid_password
from .config import Settings
from .job_store import MissingRecord, S3JobStore
from .models import Destination, JobRequest, JobSource, JobStatus, UploadSession
from .sanitization import sanitised_schema
from .table_lock import S3TableLockManager, S3TableMutationQueue, TableLockedError
from .worker_routing import RoutingError, SelectedFile, route_files
from . import skill_bundle


_CONTRACT_BUCKET = "ah-data-analytics"
_CONTRACT_PREFIX = "temp_s3_update/web_ingest/table_contracts"
_HISTORY_BUCKET = "ah-data-analytics"
_HISTORY_PREFIX = "temp_s3_update/web_ingest/upload_history"
_UPLOAD_HISTORY_TABLE = "uploader_upload_history"
_IDENTITY_EMULATION_HEADER = "X-Pilot-User-Id"
_LOCAL_IDENTITY_PROFILES = {
    "local-admin": {"is_admin": True, "can_view_upload_history": True, "can_rollback_uploads": True, "buckets": []},
    "local-editor": {
        "is_admin": False, "can_view_upload_history": True, "can_rollback_uploads": True,
        "buckets": [{"table_bucket_arn": "arn:aws:s3tables:ap-southeast-1:964340114883:bucket/ah-soc-delta-pilot", "namespace": "pilot", "label": "AH SOC delta pilot"}],
    },
    "local-unassigned": {"is_admin": False, "can_view_upload_history": False, "can_rollback_uploads": False, "buckets": []},
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


class LeaseFile(BaseModel):
    name: str = Field(min_length=1, max_length=512)
    size_bytes: int = Field(gt=0)


class CreateWorkerLeaseRequest(BaseModel):
    files: list[LeaseFile] = Field(min_length=1)


class CreateTableBucketRequest(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9-]{3,63}$")


class CreateNamespaceRequest(BaseModel):
    table_bucket_arn: str = Field(min_length=1)
    namespace: str = Field(pattern=r"^[a-z][a-z0-9_]{0,254}$")


class DeleteTableRequest(BaseModel):
    table_bucket_arn: str = Field(min_length=1)
    namespace: str = Field(pattern=r"^[a-z][a-z0-9_]{0,254}$")
    table: str = Field(pattern=r"^[a-z][a-z0-9_]{0,254}$")


class RollbackRequest(BaseModel):
    table: str = Field(pattern=r"^[a-z][a-z0-9_]{0,254}$")
    table_bucket_arn: str = Field(min_length=1)
    namespace: str = Field(pattern=r"^[a-z][a-z0-9_]{0,254}$")
    upload_id: str = Field(min_length=1, max_length=128)
    confirm: bool = False


class DeleteSkillFileRequest(BaseModel):
    table_bucket_arn: str = Field(min_length=1)
    path: str = Field(min_length=1, max_length=1024)
    confirm: bool = False


_SUPPORTED_COMPAT_SUFFIXES = (".parquet", ".parquet.gzip", ".xlsx", ".xls", ".csv", ".tsv")
_UPLOAD_PART_BYTES = 8 * 1024 * 1024
_PREUPLOAD_LEASE_MINUTES = 10
_ACTIVE_LEASE_MINUTES = 30


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_upload_name(name: str) -> str:
    value = Path(name).name
    if not value or value in {".", ".."}:
        raise HTTPException(400, "Each uploaded file must have a filename")
    return value


def _history_prefix(table_bucket_arn: str, namespace: str, table: str) -> str:
    scope = hashlib.sha256(f"{table_bucket_arn}|{namespace}".encode()).hexdigest()[:16]
    return f"{_HISTORY_PREFIX}/{scope}/{table}/"


def _contract_key(table_bucket_arn: str, namespace: str, table: str) -> str:
    scope = hashlib.sha256(f"{table_bucket_arn}|{namespace}".encode()).hexdigest()[:16]
    return f"{_CONTRACT_PREFIX}/{scope}/{table}.json"


def _upload_id() -> str:
    return f"UPLOAD-{uuid.uuid4().hex[:12].upper()}"


def _iceberg_row_count(s3: Any, metadata_uri: str | None) -> int | None:
    """Return the current Iceberg snapshot total without scanning table data."""
    if not metadata_uri or not metadata_uri.startswith("s3://"):
        return None
    try:
        bucket, key = metadata_uri.removeprefix("s3://").split("/", 1)
        metadata = json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
        current = metadata.get("current-snapshot-id")
        snapshot = next((item for item in metadata.get("snapshots", []) if item.get("snapshot-id") == current), None)
        total = (snapshot or {}).get("summary", {}).get("total-records")
        return int(total) if total is not None else None
    except Exception:
        return None


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


def create_app(
    settings: Settings,
    s3_client: Any | None = None,
    sqs_client: Any | None = None,
    s3tables_client: Any | None = None,
    glue_client: Any | None = None,
) -> FastAPI:
    s3 = s3_client or boto3.client("s3", region_name=settings.region)
    sqs = sqs_client or boto3.client("sqs", region_name=settings.region)
    glue = glue_client or boto3.client("glue", region_name=settings.region)
    s3tables = s3tables_client or boto3.client("s3tables", region_name=settings.region)
    store = S3JobStore(s3, settings.landing_bucket, settings.landing_prefix)
    app = FastAPI(title="S3 Uploader v2", docs_url=None, redoc_url=None)
    static_root = Path(__file__).parent / "static"

    def _profile(user_id: str) -> dict[str, Any]:
        profile = _LOCAL_IDENTITY_PROFILES.get(user_id)
        if profile is None:
            raise HTTPException(403, "This user has no S3 Tables bucket assignment")
        return profile

    def _is_admin(user_id: str) -> bool:
        return bool(_profile(user_id)["is_admin"])

    def _require_admin(user_id: str) -> None:
        if not _is_admin(user_id):
            raise HTTPException(403, "Only administrators may create S3 Tables buckets and namespaces")

    def current_user(request: Request) -> str:
        require_user(request, settings)
        user_id = request.headers.get(_IDENTITY_EMULATION_HEADER, "local-admin")
        _profile(user_id)
        return user_id

    def _control_plane_error(error: ClientError, resource: str) -> HTTPException:
        code = error.response.get("Error", {}).get("Code", "S3TablesError")
        message = error.response.get("Error", {}).get("Message", "S3 Tables control-plane request failed")
        if code in {"AccessDenied", "AccessDeniedException"}:
            return HTTPException(403, f"{resource}: {message}")
        if code in {"ResourceNotFoundException", "NotFoundException"}:
            return HTTPException(404, f"{resource}: {message}")
        if code in {"ConflictException", "AlreadyExistsException"}:
            return HTTPException(409, f"{resource}: {message}")
        return HTTPException(400, f"{resource}: {message}")

    def _table_buckets() -> list[dict[str, str]]:
        buckets: list[dict[str, str]] = []
        request: dict[str, str] = {}
        try:
            while True:
                response = s3tables.list_table_buckets(**request)
                buckets.extend(
                    {"table_bucket_arn": item["arn"], "label": item.get("name", item["arn"].rsplit("/", 1)[-1])}
                    for item in response.get("tableBuckets", [])
                    if item.get("type", "customer") == "customer"
                )
                token = response.get("continuationToken")
                if not token:
                    break
                request = {"continuationToken": token}
        except ClientError as error:
            raise _control_plane_error(error, "S3 Tables buckets") from error
        return sorted(buckets, key=lambda item: item["label"])

    def _require_table_bucket(table_bucket_arn: str, user_id: str) -> None:
        if table_bucket_arn not in {item["table_bucket_arn"] for item in _table_buckets()}:
            raise HTTPException(403, "TABLE_BUCKET_FORBIDDEN")
        if not _is_admin(user_id) and table_bucket_arn not in {item["table_bucket_arn"] for item in _profile(user_id)["buckets"]}:
            raise HTTPException(403, "TABLE_BUCKET_FORBIDDEN")

    def _is_uploader_managed_table(table_bucket_arn: str, namespace: str, table: str) -> bool:
        try:
            s3.head_object(Bucket=_CONTRACT_BUCKET, Key=_contract_key(table_bucket_arn, namespace, table))
            return True
        except Exception:
            return False

    def _load_contract_record(table_bucket_arn: str, namespace: str, table: str) -> dict[str, Any]:
        """Load the durable schema/sanitisation contract and its locked key universe."""
        try:
            record = json.loads(s3.get_object(
                Bucket=_CONTRACT_BUCKET, Key=_contract_key(table_bucket_arn, namespace, table)
            )["Body"].read())
        except Exception as error:
            raise HTTPException(409, f"No uploader schema contract is available for table {table!r}") from error
        columns = record.get("deduplication_columns") or []
        if not isinstance(columns, list) or any(not isinstance(column, str) or not column for column in columns):
            raise HTTPException(500, f"The stored de-duplication contract for {table!r} is invalid")
        if len(columns) != len(set(columns)):
            raise HTTPException(500, f"The stored de-duplication contract for {table!r} contains duplicate columns")
        record["deduplication_columns"] = columns
        return record

    def _activate_late_deduplication_contract(
        table_bucket_arn: str, namespace: str, table: str, contract: dict[str, Any], columns: list[str], user_id: str,
    ) -> dict[str, Any]:
        """Save the one user-selected key for a previously keyless table."""
        if contract["deduplication_columns"]:
            return contract
        updated = {
            **contract,
            "contract_version": max(int(contract.get("contract_version", 1)), 3),
            "deduplication_columns": columns,
            "deduplication_mode": "keyed",
            "deduplication_policy": "derived-locked-key-v3",
            "deduplication_activated_by": user_id,
            "deduplication_activated_at": _now(),
        }
        key = _contract_key(table_bucket_arn, namespace, table)
        try:
            etag = s3.head_object(Bucket=_CONTRACT_BUCKET, Key=key).get("ETag", "").strip('"')
            s3.put_object(
                Bucket=_CONTRACT_BUCKET, Key=key, Body=json.dumps(updated, sort_keys=True).encode(),
                ContentType="application/json", ServerSideEncryption="AES256", IfMatch=etag,
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") in {"PreconditionFailed", "ConditionalRequestConflict", "412"}:
                raise HTTPException(409, "The table contract changed while assigning its first composite key; review and try again") from error
            raise HTTPException(503, "Unable to save the table's composite de-duplication key") from error
        return updated

    def _history_entries(table_bucket_arn: str, namespace: str, table: str) -> list[dict[str, Any]]:
        """Read V1's per-table audit projection, with a V2 migration fallback.

        The fallback is read-only and only exposes records whose embedded
        scope matches exactly; every new V3 write uses the canonical V1 path.
        """
        entries: list[dict[str, Any]] = []

        def load(bucket: str, prefix: str) -> None:
            paginator = s3.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
                for item in page.get("Contents", []):
                    try:
                        body = s3.get_object(Bucket=bucket, Key=item["Key"])["Body"].read()
                        entry = json.loads(body)
                    except Exception:
                        continue
                    if (
                        entry.get("table_bucket_arn") == table_bucket_arn
                        and entry.get("namespace") == namespace
                        and entry.get("target_table") == table
                    ):
                        entries.append(entry)

        load(_HISTORY_BUCKET, _history_prefix(table_bucket_arn, namespace, table))
        load(settings.landing_bucket, f"{settings.landing_prefix}/audit/")
        latest_by_upload = {str(item.get("upload_id")): item for item in sorted(entries, key=lambda item: item.get("uploaded_at") or "")}
        return sorted(latest_by_upload.values(), key=lambda item: item.get("uploaded_at") or "", reverse=True)

    def _lease_response(lease: dict[str, Any]) -> dict[str, Any]:
        return {
            "lease_id": lease["lease_id"], "worker_state": lease["state"], "worker_size": lease["worker_size"],
            "routing_score": lease["routing_score"], "routing_reason": lease["routing_reason"],
            "expires_at": lease["expires_at"], "can_retry_large": bool(lease.get("can_retry_large")),
        }

    def _lease_queue(worker_size: str) -> str:
        return settings.base_worker_queue_url if worker_size == "BASE" else settings.large_worker_queue_url

    def _dispatch_lease(lease: dict[str, Any]) -> None:
        sqs.send_message(
            QueueUrl=_lease_queue(str(lease["worker_size"])), MessageBody=f"lease:{lease['lease_id']}",
            MessageDeduplicationId=f"lease:{lease['lease_id']}:{lease.get('attempt', 1)}",
            MessageGroupId=str(lease["lease_id"]),
        )

    def _new_lease(files: list[dict[str, Any]], user_id: str) -> dict[str, Any]:
        try:
            route = route_files([SelectedFile(name=str(item["name"]), size_bytes=int(item["size_bytes"])) for item in files])
        except RoutingError as error:
            raise HTTPException(422, str(error)) from error
        now = datetime.now(timezone.utc)
        lease = {
            "schema_version": 1, "lease_id": uuid.uuid4().hex, "owner_user_id": user_id,
            "files": [{"name": str(item["name"]), "size_bytes": int(item["size_bytes"])} for item in files],
            "worker_size": route.worker_size, "routing_score": route.routing_score, "routing_reason": route.routing_reason,
            "state": "STARTING", "message": "Starting a leased worker for the selected file.", "session_id": None,
            "attempt": 1, "can_retry_large": False, "created_at": now.isoformat(), "updated_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=_PREUPLOAD_LEASE_MINUTES)).isoformat(),
        }
        store.put_lease(lease, create_only=True)
        _dispatch_lease(lease)
        return lease

    def _bind_lease(lease_id: str, user_id: str, session: dict[str, Any]) -> dict[str, Any]:
        try:
            lease = store.get_lease(lease_id)
        except MissingRecord as error:
            raise HTTPException(404, "WORKER_LEASE_NOT_FOUND") from error
        if lease.get("owner_user_id") != user_id:
            raise HTTPException(403, "WORKER_LEASE_FORBIDDEN")
        if lease.get("state") in {"CANCELLED", "EXPIRED", "COMPLETED", "RESOURCE_LIMIT_EXCEEDED"}:
            raise HTTPException(409, "WORKER_LEASE_UNAVAILABLE")
        expected = [(item["name"], int(item["size_bytes"])) for item in lease.get("files", [])]
        received = [(item["name"], int(item["size_bytes"])) for item in session.get("files", [])]
        if expected != received:
            raise HTTPException(409, "WORKER_LEASE_FILES_CHANGED")
        return store.update_lease(lease_id, {
            "session_id": session["session_id"], "state": "AWAITING_UPLOAD", "message": "Waiting for the upload to become available.",
            "updated_at": _now(), "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=_ACTIVE_LEASE_MINUTES)).isoformat(),
        })

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
        profiles = [
            {"user_id": user_id, **profile, "expected_access": bool(profile["is_admin"] or profile["buckets"])}
            for user_id, profile in _LOCAL_IDENTITY_PROFILES.items()
        ]
        return {"local_only": True, "header_name": _IDENTITY_EMULATION_HEADER, "profiles": profiles,
                "note": "The browser sends only the user-ID header; the backend resolves roles and bucket grants."}

    @app.get("/api/identity")
    def identity(user_id: str = Depends(current_user)) -> dict[str, Any]:
        profile = _profile(user_id)
        buckets = [{**item, "namespace": "*"} for item in _table_buckets()] if profile["is_admin"] else profile["buckets"]
        return {"user_id": user_id, "is_admin": profile["is_admin"], "can_view_upload_history": profile["can_view_upload_history"], "can_rollback_uploads": profile["can_rollback_uploads"], "scope_mode": "all-discoverable-buckets-and-namespaces" if profile["is_admin"] else "configured-bucket-and-namespace-scopes", "buckets": buckets, "request_context": {"header_name": _IDENTITY_EMULATION_HEADER, "header_value": user_id, "roles_and_grants_sent_by_browser": False}}

    @app.get("/api/buckets")
    def buckets(user_id: str = Depends(current_user)) -> dict[str, Any]:
        profile = _profile(user_id)
        if not profile["is_admin"] and not profile["buckets"]:
            raise HTTPException(403, "This user has no S3 Tables bucket assignment")
        visible = _table_buckets() if profile["is_admin"] else profile["buckets"]
        return {"user_id": user_id, "is_admin": profile["is_admin"], "can_view_upload_history": profile["can_view_upload_history"], "can_rollback_uploads": profile["can_rollback_uploads"], "buckets": visible}

    @app.post("/api/buckets", status_code=201)
    def create_table_bucket(payload: CreateTableBucketRequest, user_id: str = Depends(current_user)) -> dict[str, str]:
        _require_admin(user_id)
        try:
            result = s3tables.create_table_bucket(name=payload.name)
        except ClientError as error:
            raise _control_plane_error(error, "S3 Tables bucket") from error
        return {"table_bucket_arn": result["arn"], "label": payload.name}

    @app.get("/api/namespaces")
    def namespaces(table_bucket_arn: str, user_id: str = Depends(current_user)) -> dict[str, Any]:
        _require_table_bucket(table_bucket_arn, user_id)
        namespaces: list[str] = []
        request: dict[str, str] = {"tableBucketARN": table_bucket_arn}
        try:
            while True:
                response = s3tables.list_namespaces(**request)
                namespaces.extend(item["namespace"][0] for item in response.get("namespaces", []) if len(item.get("namespace", [])) == 1)
                token = response.get("continuationToken")
                if not token:
                    break
                request = {"tableBucketARN": table_bucket_arn, "continuationToken": token}
        except ClientError as error:
            raise _control_plane_error(error, "namespace") from error
        return {"table_bucket_arn": table_bucket_arn, "namespaces": sorted(namespaces)}

    @app.post("/api/namespaces", status_code=201)
    def create_namespace(payload: CreateNamespaceRequest, user_id: str = Depends(current_user)) -> dict[str, str]:
        _require_admin(user_id)
        _require_table_bucket(payload.table_bucket_arn, user_id)
        try:
            result = s3tables.create_namespace(tableBucketARN=payload.table_bucket_arn, namespace=[payload.namespace])
        except ClientError as error:
            raise _control_plane_error(error, "namespace") from error
        namespace = result.get("namespace", [payload.namespace])
        return {"table_bucket_arn": result.get("tableBucketARN", payload.table_bucket_arn), "namespace": namespace[0]}

    @app.get("/api/tables")
    def tables(table_bucket_arn: str, namespace: str, user_id: str = Depends(current_user)) -> dict[str, Any]:
        _require_table_bucket(table_bucket_arn, user_id)
        rows: list[dict[str, Any]] = []
        request: dict[str, str] = {"tableBucketARN": table_bucket_arn, "namespace": namespace}
        try:
            while True:
                response = s3tables.list_tables(**request)
                for item in response.get("tables", []):
                    if item["name"] == _UPLOAD_HISTORY_TABLE:
                        continue
                    details = s3tables.get_table(tableBucketARN=table_bucket_arn, namespace=namespace, name=item["name"])
                    uploader_managed = _is_uploader_managed_table(table_bucket_arn, namespace, item["name"])
                    contract = _load_contract_record(table_bucket_arn, namespace, item["name"]) if uploader_managed else {}
                    rows.append({
                        "name": item["name"],
                        "created_at": str(item.get("createdAt")),
                        "modified_at": str(item.get("modifiedAt")),
                        "row_count": _iceberg_row_count(s3, details.get("metadataLocation")),
                        "uploader_managed": uploader_managed,
                        "deduplication_columns": contract.get("deduplication_columns", []),
                    })
                token = response.get("continuationToken")
                if not token:
                    break
                request = {"tableBucketARN": table_bucket_arn, "namespace": namespace, "continuationToken": token}
        except ClientError as error:
            raise _control_plane_error(error, "S3 Tables") from error
        return {"table_bucket": table_bucket_arn, "namespace": namespace, "is_admin": _is_admin(user_id), "tables": sorted(rows, key=lambda item: item["name"])}

    @app.delete("/api/tables")
    def delete_table(payload: DeleteTableRequest, user_id: str = Depends(current_user)) -> dict[str, str]:
        _require_admin(user_id)
        _require_table_bucket(payload.table_bucket_arn, user_id)
        if payload.table == _UPLOAD_HISTORY_TABLE:
            raise HTTPException(400, "The reserved uploader audit table cannot be deleted through this UI")
        if not _is_uploader_managed_table(payload.table_bucket_arn, payload.namespace, payload.table):
            raise HTTPException(409, "This table is browse-only because it was not created by this uploader")
        try:
            s3tables.delete_table(tableBucketARN=payload.table_bucket_arn, namespace=payload.namespace, name=payload.table)
        except ClientError as error:
            raise _control_plane_error(error, f"S3 Table {payload.namespace}.{payload.table}") from error
        return {"deleted": payload.table, "table_bucket_arn": payload.table_bucket_arn, "namespace": payload.namespace}

    @app.get("/api/upload-history")
    def upload_history(table_bucket_arn: str, namespace: str, table: str, user_id: str = Depends(current_user)) -> dict[str, Any]:
        _require_table_bucket(table_bucket_arn, user_id)
        if not _profile(user_id)["can_view_upload_history"]:
            raise HTTPException(403, "This user cannot view upload history")
        if table == _UPLOAD_HISTORY_TABLE:
            raise HTTPException(400, "The reserved uploader audit table is not a master-data destination")
        if not _is_uploader_managed_table(table_bucket_arn, namespace, table):
            raise HTTPException(409, "This table is browse-only because it has no uploader history contract")
        history = _history_entries(table_bucket_arn, namespace, table)
        successful = [item for item in history if item.get("status") == "SUCCESS" and item.get("previous_snapshot_id")]
        latest = max(successful, key=lambda item: item.get("uploaded_at") or "", default=None)
        return {
            "table_bucket_arn": table_bucket_arn,
            "namespace": namespace,
            "table": table,
            "history": history,
            "latest_rollback_upload_id": latest.get("upload_id") if latest else None,
        }

    @app.post("/api/rollbacks")
    def start_rollback(payload: RollbackRequest, user_id: str = Depends(current_user)) -> dict[str, Any]:
        _require_table_bucket(payload.table_bucket_arn, user_id)
        if not _profile(user_id)["can_rollback_uploads"]:
            raise HTTPException(403, "This user cannot roll back uploads")
        if not payload.confirm:
            raise HTTPException(400, "Explicit rollback confirmation is required")
        if payload.table == _UPLOAD_HISTORY_TABLE:
            raise HTTPException(400, "The reserved uploader audit table cannot be rolled back through this UI")
        if not _is_uploader_managed_table(payload.table_bucket_arn, payload.namespace, payload.table):
            raise HTTPException(409, "This table is browse-only because it has no uploader history contract")
        history = _history_entries(payload.table_bucket_arn, payload.namespace, payload.table)
        selected = next((item for item in history if item.get("upload_id") == payload.upload_id), None)
        successful = [item for item in history if item.get("status") == "SUCCESS"]
        latest = max(successful, key=lambda item: item.get("uploaded_at") or "", default=None)
        if not selected or selected.get("status") != "SUCCESS":
            raise HTTPException(409, "Only a successful upload that has not already been rolled back can be restored")
        if selected != latest:
            raise HTTPException(409, "Only the latest successful uploader-managed update may be rolled back")
        snapshot_id = selected.get("previous_snapshot_id")
        if not snapshot_id:
            raise HTTPException(409, "The initial table load has no earlier snapshot to restore")
        run_id = str(uuid.uuid4())
        lock_manager = S3TableLockManager(s3, settings.landing_bucket, f"{settings.landing_prefix}/table-locks")
        try:
            table_lock = lock_manager.acquire(
                table_bucket_arn=payload.table_bucket_arn, namespace=payload.namespace, table=payload.table,
                owner_token=run_id, user_id=user_id, request_id=run_id, session_id=None,
                operation="rollback", phase="STARTING_GLUE",
            )
        except TableLockedError as error:
            raise HTTPException(409, "TABLE_MUTATION_IN_PROGRESS") from error
        try:
            response = glue.start_job_run(
                JobName=settings.glue_job_name,
                JobRunQueuingEnabled=True,
                Arguments={
                    "--MODE": "rollback",
                    "--MANIFEST_URI": "s3://ah-data-analytics/temp_s3_update/web_ingest/uploads/not-used-for-rollback.json",
                    "--TABLE_BUCKET_ARN": payload.table_bucket_arn,
                    "--NAMESPACE": payload.namespace,
                    "--TABLE": payload.table,
                    "--QC_PREFIX": "s3://ah-data-analytics/temp_s3_update/qc",
                    "--RUN_ID": run_id,
                    "--UPLOAD_ID": payload.upload_id,
                    "--UPLOADED_BY": user_id,
                    "--ORIGINAL_UPLOADED_BY": selected.get("uploaded_by") or user_id,
                    "--ORIGINAL_UPLOADED_AT": selected.get("uploaded_at") or _now(),
                    "--REPORTING_MONTH": selected.get("reporting_month") or "not-applicable",
                    "--FILENAMES_JSON": selected.get("filenames") or "[]",
                    "--AUDIT_PREFIX": f"s3://{_HISTORY_BUCKET}/{_history_prefix(payload.table_bucket_arn, payload.namespace, payload.table)}",
                    "--ROLLBACK_SNAPSHOT_ID": str(snapshot_id),
                    "--LOCK_BUCKET": settings.landing_bucket, "--LOCK_KEY": table_lock.key, "--LOCK_ETAG": table_lock.etag,
                    "--QUEUE_BUCKET": "", "--QUEUE_KEY": "", "--QUEUE_ETAG": "",
                },
            )
        except ClientError as error:
            lock_manager.release(table_lock)
            raise HTTPException(502, "AWS Glue could not start the rollback") from error
        return {
            "job_run_id": response["JobRunId"],
            "qc_uri": f"s3://ah-data-analytics/temp_s3_update/qc/web/{run_id}/report.json",
            "upload_id": payload.upload_id,
            "operation": "rollback",
        }

    @app.get("/api/skills/files")
    def skill_files(table_bucket_arn: str, user_id: str = Depends(current_user)) -> dict[str, Any]:
        _require_table_bucket(table_bucket_arn, user_id)
        try:
            return skill_bundle.list_skill_files(table_bucket_arn)
        except skill_bundle.SkillBundleError as error:
            raise HTTPException(error.status_code, str(error)) from error

    @app.post("/api/skills/files")
    async def upload_skill_files(
        table_bucket_arn: str = Form(), paths_json: str = Form(), files: list[UploadFile] = File(),
        user_id: str = Depends(current_user),
    ) -> dict[str, Any]:
        """Incrementally add or overwrite supplied safe skill files."""
        _require_table_bucket(table_bucket_arn, user_id)
        try:
            paths = skill_bundle.parse_paths_json(paths_json)
            if len(paths) != len(files):
                raise skill_bundle.SkillBundleError("Each uploaded skill file must have one matching relative path")
            payload = [(path, await upload.read(skill_bundle.MAX_FILE_BYTES + 1)) for path, upload in zip(paths, files, strict=True)]
            return skill_bundle.publish_files(table_bucket_arn, user_id, payload)
        except skill_bundle.SkillBundleError as error:
            raise HTTPException(error.status_code, str(error)) from error
        finally:
            for upload in files:
                await upload.close()

    def _stream_skill_file(body: Any) -> Iterator[bytes]:
        try:
            while chunk := body.read(1024 * 1024):
                yield chunk
        finally:
            body.close()

    @app.get("/api/skills/files/download")
    def download_skill_file(table_bucket_arn: str, path: str, user_id: str = Depends(current_user)) -> StreamingResponse:
        _require_table_bucket(table_bucket_arn, user_id)
        try:
            destination_bucket, key, safe_path = skill_bundle.skill_file_location(table_bucket_arn, path)
            result = skill_bundle.s3.get_object(Bucket=destination_bucket, Key=key)
        except skill_bundle.SkillBundleError as error:
            raise HTTPException(error.status_code, str(error)) from error
        except ClientError as error:
            if error.response.get("Error", {}).get("Code", "") in {"404", "NoSuchKey", "NotFound"}:
                raise HTTPException(404, "The requested skill file no longer exists") from error
            raise HTTPException(502, "Unable to download the requested skill file from S3") from error
        headers = {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(safe_path.rsplit('/', 1)[-1])}"}
        if result.get("ContentLength") is not None:
            headers["Content-Length"] = str(result["ContentLength"])
        return StreamingResponse(
            _stream_skill_file(result["Body"]), media_type=result.get("ContentType") or "application/octet-stream", headers=headers,
        )

    @app.delete("/api/skills/files")
    def delete_skill_file(payload: DeleteSkillFileRequest, user_id: str = Depends(current_user)) -> dict[str, str]:
        _require_table_bucket(payload.table_bucket_arn, user_id)
        if not payload.confirm:
            raise HTTPException(422, "Confirm deletion before removing a skill file")
        try:
            destination_bucket, key, safe_path = skill_bundle.skill_file_location(payload.table_bucket_arn, payload.path)
            skill_bundle.s3.head_object(Bucket=destination_bucket, Key=key)
            skill_bundle.s3.delete_object(Bucket=destination_bucket, Key=key)
        except skill_bundle.SkillBundleError as error:
            raise HTTPException(error.status_code, str(error)) from error
        except ClientError as error:
            if error.response.get("Error", {}).get("Code", "") in {"404", "NoSuchKey", "NotFound"}:
                raise HTTPException(404, "The requested skill file no longer exists") from error
            raise HTTPException(502, "Unable to delete the requested skill file from S3") from error
        return {"deleted_path": safe_path}

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

    @app.post("/api/v3/worker-leases", status_code=201)
    def create_worker_lease(payload: CreateWorkerLeaseRequest, user_id: str = Depends(current_user)) -> dict[str, Any]:
        if not settings.leases_enabled:
            raise HTTPException(409, "WORKER_LEASES_DISABLED")
        lease = _new_lease([item.model_dump() for item in payload.files], user_id)
        return _lease_response(lease)

    @app.put("/api/v3/worker-leases/{lease_id}")
    def replace_idle_worker_lease(lease_id: str, payload: CreateWorkerLeaseRequest, user_id: str = Depends(current_user)) -> dict[str, Any]:
        """Reuse an idle worker unless the new deterministic size differs."""
        try:
            lease = store.get_lease(lease_id)
        except MissingRecord as error:
            raise HTTPException(404, "WORKER_LEASE_NOT_FOUND") from error
        if lease.get("owner_user_id") != user_id:
            raise HTTPException(403, "WORKER_LEASE_FORBIDDEN")
        session_id = str(lease.get("session_id") or "")
        if lease.get("state") not in {"STARTING", "AWAITING_UPLOAD", "PROFILING", "AWAITING_KEY", "ANALYSING_KEY", "AWAITING_CONFIRMATION"}:
            raise HTTPException(409, "WORKER_LEASE_CANNOT_BE_REUSED")
        if session_id:
            try:
                session = store.get_compat_session(session_id)
            except MissingRecord as error:
                raise HTTPException(409, "WORKER_LEASE_CANNOT_BE_REUSED") from error
            # Profiling and key analysis have no Glue or table side effects, so
            # an abandoned review may safely yield the worker to a replacement
            # selection.  Preparation and Glue submission are deliberately
            # excluded: their lease must remain immutable.
            if session.get("phase") not in {"RECEIVED", "PROFILING", "READY_FOR_REVIEW", "KEY_ANALYSING", "READY_FOR_ACKNOWLEDGEMENT"}:
                raise HTTPException(409, "WORKER_LEASE_CANNOT_BE_REUSED")
        files = [item.model_dump() for item in payload.files]
        try:
            route = route_files([SelectedFile(name=item["name"], size_bytes=item["size_bytes"]) for item in files])
        except RoutingError as error:
            raise HTTPException(422, str(error)) from error
        if route.worker_size == lease.get("worker_size"):
            lease = store.update_lease(lease_id, {
                "files": files, "routing_score": route.routing_score, "routing_reason": route.routing_reason,
                "session_id": None, "replaced_session_id": session_id or None,
                "state": "AWAITING_UPLOAD", "message": "File selection updated; reusing the existing worker.", "updated_at": _now(),
                "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=_PREUPLOAD_LEASE_MINUTES)).isoformat(),
            })
            return {**_lease_response(lease), "reused": True, "replaced": False}
        old_size, new_size = str(lease.get("worker_size")), route.worker_size
        lease = store.update_lease(lease_id, {"state": "CANCELLED", "message": f"File selection changed from {old_size} to {new_size}; replacing worker.", "updated_at": _now()})
        replacement = _new_lease(files, user_id)
        return {**_lease_response(replacement), "reused": False, "replaced": True, "replaced_lease_id": lease_id}

    @app.delete("/api/v3/worker-leases/{lease_id}", status_code=204)
    def cancel_worker_lease(lease_id: str, user_id: str = Depends(current_user)) -> Response:
        try:
            lease = store.get_lease(lease_id)
        except MissingRecord as error:
            raise HTTPException(404, "WORKER_LEASE_NOT_FOUND") from error
        if lease.get("owner_user_id") != user_id:
            raise HTTPException(403, "WORKER_LEASE_FORBIDDEN")
        if lease.get("session_id"):
            raise HTTPException(409, "WORKER_LEASE_ALREADY_ATTACHED")
        store.update_lease(lease_id, {"state": "CANCELLED", "message": "File selection changed.", "updated_at": _now()})
        return Response(status_code=204)

    @app.post("/api/v3/worker-leases/{lease_id}/retry-large", status_code=202)
    def retry_large_worker(lease_id: str, user_id: str = Depends(current_user)) -> dict[str, Any]:
        try:
            lease = store.get_lease(lease_id)
        except MissingRecord as error:
            raise HTTPException(404, "WORKER_LEASE_NOT_FOUND") from error
        if lease.get("owner_user_id") != user_id:
            raise HTTPException(403, "WORKER_LEASE_FORBIDDEN")
        if lease.get("worker_size") != "BASE" or lease.get("state") != "RESOURCE_LIMIT_EXCEEDED" or not lease.get("can_retry_large"):
            raise HTTPException(409, "LARGE_RETRY_UNAVAILABLE")
        session = _get_compat_session(str(lease.get("session_id") or ""), user_id)
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
            store.put_status(JobStatus(job_id=new_job_id, phase="QUEUED", message="Large worker retry queued."))
            session["ingestion"] = {**ingestion, "job_id": new_job_id, "state": "QUEUED", "job_run_id": None}
        _save_compat_session(session, phase=resume_phase, progress_message="Large worker retry is starting.", error=None)
        lease = store.update_lease(lease_id, {
            "worker_size": "LARGE", "state": "STARTING", "message": "Starting the requested large worker retry.",
            "can_retry_large": False, "attempt": int(lease.get("attempt", 1)) + 1, "updated_at": _now(),
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=_ACTIVE_LEASE_MINUTES)).isoformat(),
        })
        _dispatch_lease(lease)
        return _lease_response(lease)

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
        lease_id = str(form.get("worker_lease_id", "")).strip() or None
        if mode not in {"create", "append"}:
            raise HTTPException(422, "mode must be create or append")
        _require_table_bucket(table_bucket_arn, user_id)
        if not namespace or not table:
            raise HTTPException(422, "namespace and table are required")
        uploads = [item for item in form.getlist("files") if hasattr(item, "filename") and hasattr(item, "file")]
        if not uploads:
            raise HTTPException(400, "Choose at least one Parquet file")
        invalid = [str(upload.filename or "<unnamed>") for upload in uploads if not (upload.filename or "").lower().endswith(_SUPPORTED_COMPAT_SUFFIXES)]
        if invalid:
            raise HTTPException(400, "Supported files are Parquet, Parquet GZIP, XLSX, XLS, CSV, and TSV")

        session_id = uuid.uuid4().hex
        received_at = _now()
        files: list[dict[str, Any]] = []
        try:
            for number, upload in enumerate(uploads):
                name = _safe_upload_name(upload.filename or "")
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
            session = {
                "schema_version": 1, "session_id": session_id, "owner_user_id": user_id, "mode": mode,
                "table_bucket_arn": table_bucket_arn, "namespace": namespace, "table": table,
                "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=60)).isoformat(), "files": files,
                "phase": "RECEIVED", "progress_message": "Files are stored in S3; waiting for isolated worker profiling.", "error": None,
                "preflight": None, "key_impact": None, "ingestion": None, "phase_timings_ms": {},
                "created_at": received_at, "updated_at": _now(), "phase_started_at": _now(),
            }
            if settings.leases_enabled:
                if lease_id is None:
                    lease = _new_lease(files, user_id)
                else:
                    try:
                        lease = store.get_lease(lease_id)
                    except MissingRecord as error:
                        lease = _new_lease(files, user_id)
                    if lease.get("owner_user_id") != user_id:
                        # A browser identity can change while its previous
                        # file-selection lease remains in memory. Never bind
                        # another user's lease; start a new owner-scoped one.
                        lease = _new_lease(files, user_id)
                    expected = [(item["name"], int(item["size_bytes"])) for item in lease.get("files", [])]
                    received = [(item["name"], int(item["size_bytes"])) for item in files]
                    if lease.get("state") in {"CANCELLED", "EXPIRED", "COMPLETED", "RESOURCE_LIMIT_EXCEEDED"} or expected != received:
                        lease = _new_lease(files, user_id)
                session["worker_lease_id"] = lease["lease_id"]
            store.put_compat_session(session, create_only=True)
            if settings.leases_enabled:
                # The leased task is already starting, or was just started as
                # the backwards-compatible fallback for a missed warm-up.
                try:
                    lease = _bind_lease(str(session["worker_lease_id"]), user_id, session)
                except HTTPException as error:
                    if error.status_code != 409:
                        raise
                    lease = _new_lease(files, user_id)
                    session["worker_lease_id"] = lease["lease_id"]
                    store.put_compat_session(session)
                    lease = _bind_lease(str(session["worker_lease_id"]), user_id, session)
            else:
                _dispatch_compat_work("profile", session)
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
        lease_id = session.get("worker_lease_id")
        if lease_id:
            try:
                value["worker_lease"] = _lease_response(store.get_lease(str(lease_id)))
            except MissingRecord:
                value["worker_lease"] = {"lease_id": lease_id, "worker_state": "FAILED", "can_retry_large": False}
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
        updated = store.update_compat_session(str(session["session_id"]), {**changes, "updated_at": now})
        session.clear(); session.update(updated)
        return session

    def _dispatch_compat_work(action: Literal["profile", "key"], session: dict[str, Any]) -> None:
        work_id = f"{action}:{session['session_id']}"
        group = hashlib.sha256(f"{session['table_bucket_arn']}\x1f{session['namespace']}\x1f{session['table']}".encode()).hexdigest()
        sqs.send_message(QueueUrl=settings.queue_url, MessageBody=work_id, MessageDeduplicationId=f"{work_id}:{uuid.uuid4()}", MessageGroupId=group)

    def _glue_run(job_run_id: str) -> dict[str, Any]:
        try:
            run = glue.get_job_run(JobName=settings.glue_job_name, RunId=job_run_id, PredecessorsIncluded=False)["JobRun"]
        except Exception as error:
            raise HTTPException(503, "Glue status is temporarily unavailable") from error
        state = str(run.get("JobRunState", "UNKNOWN"))
        message = str(run.get("ErrorMessage") or run.get("StateDetail") or f"Glue job is {state.lower()}.")
        return {"state": state, "message": message, "raw": run}

    def _reconcile_glue_job(job_id: str, status: JobStatus) -> JobStatus:
        if status.phase != "RUNNING_GLUE" or not status.glue_run_id:
            return status
        result = _glue_run(status.glue_run_id)
        state = result["state"]
        if state == "SUCCEEDED":
            status = JobStatus(job_id=job_id, phase="SUCCEEDED", message="Glue ingestion succeeded.", glue_run_id=status.glue_run_id)
            store.put_status(status)
        elif state in {"FAILED", "ERROR", "TIMEOUT", "STOPPED"}:
            status = JobStatus(job_id=job_id, phase="FAILED", message=result["message"], error_code=f"GLUE_{state}", glue_run_id=status.glue_run_id)
            store.put_status(status)
        return status

    @app.get("/api/v2/upload-sessions/{session_id}")
    def get_compat_session(session_id: str, user_id: str = Depends(current_user)) -> dict[str, Any]:
        session = _get_compat_session(session_id, user_id)
        # A worker owns durable job progress.  Mirror its state into the v1
        # session response so the unchanged browser can reconnect after an API
        # replacement or page refresh.
        job_id = (session.get("ingestion") or {}).get("job_id")
        if job_id and session.get("phase") != "FAILED":
            try:
                status = _reconcile_glue_job(job_id, store.get_status(job_id).status)
                phase_map = {"QUEUED": "QUEUED", "CLAIMED": "QUEUED", "PROFILING": "QUEUED", "PREPARING": "STARTING_GLUE", "STARTING_GLUE": "STARTING_GLUE", "RUNNING_GLUE": "GLUE_RUNNING", "SUCCEEDED": "SUCCEEDED", "FAILED": "FAILED"}
                ingestion = {**(session.get("ingestion") or {}), "state": status.phase, "job_run_id": status.glue_run_id, "qc_uri": f"s3://{settings.landing_bucket}/{settings.landing_prefix}/qc/{job_id}.json"}
                changes: dict[str, Any] = {"phase": phase_map[status.phase], "progress_message": status.message, "ingestion": ingestion}
                if status.phase == "FAILED":
                    changes["error"] = {"code": status.error_code or "WORKER_FAILED", "message": status.message}
                session = _save_compat_session(session, **changes)
            except MissingRecord:
                pass
        return _safe_compat_session(session)

    @app.get("/api/ingestions/{job_run_id}")
    def ingestion_status(job_run_id: str, _: str = Depends(current_user)) -> dict[str, Any]:
        result = _glue_run(job_run_id)
        return {"job_run_id": job_run_id, "state": result["state"], "message": result["message"]}

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
        _save_compat_session(
            session, phase="KEY_ANALYSING", progress_message="Queued for isolated composite-key analysis.", key_impact=None,
            key_analysis_request={"deduplication_columns": columns, "type_overrides": payload.type_overrides},
        )
        if not (settings.leases_enabled and session.get("worker_lease_id")):
            _dispatch_compat_work("key", session)
        return {"session_id": session_id, "phase": "KEY_ANALYSING", "message": "Composite-key analysis has started in the isolated worker."}

    @app.post("/api/v2/upload-sessions/{session_id}/ingestions", status_code=202)
    def start_compat_ingestion(session_id: str, payload: SessionIngestionRequest, user_id: str = Depends(current_user)) -> dict[str, Any]:
        session = _get_compat_session(session_id, user_id)
        if session.get("phase") not in {"READY_FOR_REVIEW", "READY_FOR_ACKNOWLEDGEMENT"}:
            raise HTTPException(409, f"Upload is unavailable while session phase is {session.get('phase')}")
        if not (session.get("preflight") or {}).get("accepted"):
            raise HTTPException(422, "The uploaded files did not pass the completed preflight validation")
        if session["table"] == _UPLOAD_HISTORY_TABLE:
            raise HTTPException(400, "The reserved uploader audit table is not a master-data destination")
        effective_deduplication_mode = payload.deduplication_mode
        effective_deduplication_columns = list(payload.deduplication_columns)
        late_key_activation = False
        if session["mode"] == "append":
            contract = _load_contract_record(session["table_bucket_arn"], session["namespace"], session["table"])
            configured = contract["deduplication_columns"]
            if configured:
                # The table's first selected key remains immutable, but a
                # later source may expose only a subset (for example a SAP
                # extract without identifiers available in Epic). The worker
                # profiled this source; never trust a browser-supplied key.
                effective_deduplication_columns = list((session.get("preflight") or {}).get("deduplication_columns") or [])
                effective_deduplication_mode = "keyed" if effective_deduplication_columns else "none"
            elif effective_deduplication_mode == "keyed":
                late_key_activation = True

        if effective_deduplication_mode == "keyed" and not (session["mode"] == "append" and configured):
            impact = session.get("key_impact") or {}
            if payload.key_analysis_token != impact.get("token") or effective_deduplication_columns != impact.get("deduplication_columns"):
                raise HTTPException(422, "Run and acknowledge composite-key analysis before keyed ingestion")
            expires_at = impact.get("expires_at")
            if not expires_at or datetime.fromisoformat(expires_at) <= datetime.now(timezone.utc):
                raise HTTPException(422, "The composite-key analysis acknowledgement has expired; run it again")
        if late_key_activation:
            _activate_late_deduplication_contract(
                session["table_bucket_arn"], session["namespace"], session["table"], contract,
                effective_deduplication_columns, user_id,
            )
        allowed_manual = {
            item["column"] for item in (session.get("preflight") or {}).get("sanitization_review", {}).get("manual_encryption_candidates", [])
        }
        invalid_manual = sorted(set(payload.manual_encryption_columns) - allowed_manual)
        if invalid_manual:
            raise HTTPException(422, f"Manual encryption is not available for: {', '.join(invalid_manual)}")
        sources: list[JobSource] = []
        for source in session["files"]:
            if not source.get("source_version_id"):
                head = s3.head_object(Bucket=settings.landing_bucket, Key=source["source_key"])
                source["source_version_id"] = head.get("VersionId")
            if not source.get("source_version_id"):
                raise HTTPException(500, "LANDING_BUCKET_VERSIONING_REQUIRED")
            sources.append(JobSource(name=source["name"], source_key=source["source_key"], source_version_id=source["source_version_id"], source_sha256=source["sha256"], source_size_bytes=source["size_bytes"]))
        source = sources[0]
        job_id = str(uuid.uuid4())
        upload_id = _upload_id()
        job = JobRequest(job_id=job_id, session_id=session_id, owner_user_id=user_id, operation=session["mode"],
                         destination=Destination(table_bucket_arn=session["table_bucket_arn"], namespace=session["namespace"], table=session["table"]),
                         source_key=source.source_key, source_version_id=source.source_version_id,
                         source_sha256=source.source_sha256, source_size_bytes=source.source_size_bytes,
                         source_files=sources,
                         upload_id=upload_id,
                         reporting_month=payload.reporting_month, deduplication_mode=effective_deduplication_mode,
                         deduplication_columns=effective_deduplication_columns,
                         manual_encryption_columns=payload.manual_encryption_columns)
        store.put_request(job)
        S3TableMutationQueue(s3, settings.landing_bucket, f"{settings.landing_prefix}/table-queues").enqueue(
            table_bucket_arn=job.destination.table_bucket_arn, namespace=job.destination.namespace,
            table=job.destination.table, job_id=job.job_id, user_id=user_id,
            session_id=session_id, operation=job.operation, created_at=job.created_at.isoformat(),
        )
        store.put_status(JobStatus(job_id=job_id, phase="QUEUED", message="Upload queued for the isolated Fargate worker."))
        group = hashlib.sha256(f"{job.destination.table_bucket_arn}\x1f{job.destination.namespace}\x1f{job.destination.table}".encode()).hexdigest()
        if not (settings.leases_enabled and session.get("worker_lease_id")):
            sqs.send_message(QueueUrl=settings.queue_url, MessageBody=job_id, MessageDeduplicationId=job_id, MessageGroupId=group)
        ingestion = {"request_id": payload.request_id, "job_id": job_id, "upload_id": upload_id, "operation": "ingestion", "state": "QUEUED", "job_run_id": None,
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
            upload_id=_upload_id(),
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

"""Local-only web UI for the isolated S3 Tables pilot namespace."""

from __future__ import annotations

import base64
import asyncio
import csv
import hashlib
import hmac
import json
import logging
import os
import random
import shutil
import tempfile
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal

import boto3
import pandas as pd
import polars as pl
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
from botocore.exceptions import ClientError
from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator

from .contract import TARGET_COLUMNS, TIMESTAMP_TARGET_COLUMNS
from .ingest_contract import (
    compare_schema,
    manual_confirmation_columns,
    normalise_names,
    parse_documented_date,
    parse_documented_timestamp,
    schema_from_arrow,
    schema_from_table,
)
from .pilot import NAMESPACE, QC_PREFIX, REGION, ROLE_NAME, SOURCE_BUCKET, SOURCE_PREFIX, TABLE_BUCKET_ARN
from .sanitization import detect_nric_columns, encryption_key, sanitise_table, sanitised_schema
from . import skill_bundle
from .observability import configure_logging, request_id_var, safe_error, user_id_var
from .table_lock import S3TableLockManager, TableLease, TableLockedError, TableLockError
from .upload_sessions import UploadSessionStore

WEB_JOB_NAME = "ah-soc-delta-pilot-web-ingest"
WEB_SCRIPT_KEY = f"{SOURCE_PREFIX}/_pilot_assets/generic_glue_job.py"
WEB_CONTRACT_PREFIX = f"{SOURCE_PREFIX}/web_ingest/table_contracts"
WEB_UPLOAD_PREFIX = f"{SOURCE_PREFIX}/web_ingest/uploads"
WEB_HISTORY_PREFIX = f"{SOURCE_PREFIX}/web_ingest/upload_history"
UPLOAD_HISTORY_TABLE = "uploader_upload_history"
SNAPSHOT_RETENTION = {"minSnapshotsToKeep": 12, "maxSnapshotAgeHours": 365 * 24}
UPLOAD_ARCHIVE_RETENTION_DAYS = 30
UPLOAD_ARCHIVE_LIFECYCLE_RULE_ID = "agentcore-s3tables-upload-archive-30-days"
TABLE_LOCK_PREFIX = f"{SOURCE_PREFIX}/web_ingest/table_locks"
PILOT_GLUE_MAX_CONCURRENT_RUNS = int(os.environ.get("PILOT_GLUE_MAX_CONCURRENT_RUNS", "5"))
PILOT_LOCAL_PROCESSING_CONCURRENCY = int(os.environ.get("PILOT_LOCAL_PROCESSING_CONCURRENCY", "2"))
ROOT = Path(__file__).parent
s3 = boto3.client("s3", region_name=REGION)
s3tables = boto3.client("s3tables", region_name=REGION)
glue = boto3.client("glue", region_name=REGION)
iam = boto3.client("iam")
app = FastAPI(title="AH S3 Tables Pilot", docs_url=None, redoc_url=None)
logger = configure_logging()
table_locks = S3TableLockManager(s3, SOURCE_BUCKET, TABLE_LOCK_PREFIX)
upload_sessions = UploadSessionStore()
active_table_leases: dict[str, TableLease] = {}
active_mutations_by_request: dict[str, dict] = {}
pending_table_leases: dict[str, TableLease] = {}
# The v2 session bridge registers a short-lived callback for each request while
# the existing ingestion launcher prepares its artifacts.  This keeps the
# browser informed without placing mutable progress state in S3 or Glue.
ingestion_progress_hooks: dict[str, Callable[[str], None]] = {}
local_processing_slots = threading.BoundedSemaphore(PILOT_LOCAL_PROCESSING_CONCURRENCY)
maintenance_task: asyncio.Task | None = None
SUPPORTED_UPLOAD_SUFFIXES = (".parquet", ".parquet.gzip", ".xlsx", ".xls", ".csv", ".tsv")
MIN_APPEND_SCHEMA_MATCH_PERCENT = 50.0
SELECTABLE_ICEBERG_TYPES = ("STRING", "BIGINT", "DOUBLE", "DATE", "TIMESTAMP", "BOOLEAN")
IDENTITY_EMULATION_HEADER = "X-Pilot-User-Id"
LOCAL_TEST_USER_IDS = ("local-admin", "local-editor", "local-unassigned")


@app.on_event("startup")
async def cleanup_abandoned_upload_sessions() -> None:
    """Remove expired node-local uploads before accepting new pilot traffic."""
    removed = upload_sessions.cleanup_expired()
    logger.info("upload_session_startup_cleanup", extra={"removed_sessions": removed, "phase": "CLEANUP"})
    await asyncio.to_thread(_reconcile_table_locks_on_startup)
    global maintenance_task
    maintenance_task = asyncio.create_task(_maintenance_loop())


@app.on_event("shutdown")
async def stop_maintenance_loop() -> None:
    if maintenance_task:
        maintenance_task.cancel()


async def _maintenance_loop() -> None:
    """Renew active table leases and clean expired private session files."""
    while True:
        await asyncio.sleep(5 * 60)
        removed = upload_sessions.cleanup_expired()
        if removed:
            logger.info("upload_session_periodic_cleanup", extra={"removed_sessions": removed, "phase": "CLEANUP"})
        for job_run_id, lease in list(active_table_leases.items()):
            try:
                active_table_leases[job_run_id] = await asyncio.to_thread(
                    table_locks.renew, lease, "GLUE_RUNNING", job_run_id,
                )
                logger.info("table_lock_renewed", extra={"job_run_id": job_run_id, "phase": "GLUE_RUNNING"})
            except TableLockError:
                # The current 120-minute lease remains valid; record the safe
                # failure so an operator can correct S3 access before expiry.
                safe_error(logger, "table_lock_renewal_failed", job_run_id=job_run_id, phase="GLUE_RUNNING")


def _reconcile_table_locks_on_startup() -> None:
    """Recover the bounded S3 lease set after a local FastAPI restart."""
    terminal = {"SUCCEEDED", "FAILED", "TIMEOUT", "STOPPED", "ERROR"}
    try:
        leases = table_locks.list_leases()
    except TableLockError:
        safe_error(logger, "table_lock_startup_reconciliation_failed", phase="STARTUP")
        return
    for lease in leases:
        job_run_id = lease.payload.get("glue_job_run_id")
        try:
            if job_run_id:
                state = glue.get_job_run(JobName=WEB_JOB_NAME, RunId=job_run_id, PredecessorsIncluded=False)["JobRun"]["JobRunState"]
                if state in terminal:
                    table_locks.release(lease)
                else:
                    active_table_leases[job_run_id] = lease
            elif S3TableLockManager._expired(lease.payload):
                table_locks.release(lease)
        except Exception:
            safe_error(logger, "table_lock_startup_reconciliation_item_failed", phase="STARTUP", job_run_id=job_run_id)


@app.middleware("http")
async def request_logging(request: Request, call_next):
    """Emit one safe, correlated record per request without inspecting bodies."""
    request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
    user_id = request.headers.get(IDENTITY_EMULATION_HEADER)
    request_token = request_id_var.set(request_id)
    user_token = user_id_var.set(user_id)
    started = time.perf_counter()
    try:
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        logger.info(
            "http_request",
            extra={
                "method": request.method,
                "route": request.scope.get("route").path if request.scope.get("route") else request.url.path,
                "status_code": response.status_code,
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                "response_size": response.headers.get("content-length"),
            },
        )
        return response
    finally:
        request_id_var.reset(request_token)
        user_id_var.reset(user_token)


@app.exception_handler(Exception)
async def unhandled_exception(request: Request, error: Exception):
    """Keep tracebacks in local/container logs, not browser responses."""
    error_id = safe_error(logger, "unhandled_exception", method=request.method, route=request.url.path)
    request_id = request.headers.get("X-Request-ID") or request_id_var.get()
    return JSONResponse(
        status_code=500,
        content={
            "detail": "The uploader encountered an unexpected server error.",
            "code": "INTERNAL_ERROR",
            "request_id": request_id,
            "error_id": error_id,
        },
        headers={"X-Request-ID": request_id or ""},
    )


@dataclass(frozen=True)
class BucketScope:
    table_bucket_arn: str
    namespace: str
    label: str


@dataclass(frozen=True)
class PilotUser:
    user_id: str
    is_admin: bool
    can_view_upload_history: bool
    can_rollback_uploads: bool
    buckets: tuple[BucketScope, ...]


def _configured_users() -> dict:
    """Placeholder for the future frontend's authenticated user/bucket relationship.

    Set PILOT_USER_ACCESS_JSON to a mapping of user IDs to ``is_admin`` and a
    ``buckets`` list. A local administrator is dynamically authorized for every
    account-visible S3 Table bucket and namespace; non-admin users are limited
    to their explicit scopes.
    """
    default = {
        "local-admin": {
            "is_admin": True,
            "can_view_upload_history": True,
            "can_rollback_uploads": True,
            "buckets": [],
        },
        "local-editor": {
            "is_admin": False,
            # Deliberately limited recovery access for local integration tests:
            # own audit history only, and only the globally latest update.
            "can_view_upload_history": True,
            "can_rollback_uploads": True,
            "buckets": [{"table_bucket_arn": TABLE_BUCKET_ARN, "namespace": NAMESPACE, "label": "AH SOC delta pilot"}],
        },
        # Deliberately has no bucket assignment so the UI can demonstrate the
        # same authorization failure a real unassigned user receives.
        "local-unassigned": {"is_admin": False, "can_view_upload_history": False, "can_rollback_uploads": False, "buckets": []},
    }
    raw = os.environ.get("PILOT_USER_ACCESS_JSON")
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError as error:
        raise RuntimeError("PILOT_USER_ACCESS_JSON must contain valid JSON") from error


def _current_user(x_pilot_user_id: str | None = Header(default=None, alias="X-Pilot-User-Id")) -> PilotUser:
    # TRUST PLACEHOLDER: replace this lookup with verified frontend identity claims.
    user_id = x_pilot_user_id or os.environ.get("PILOT_LOCAL_USER_ID", "local-admin")
    definition = _configured_users().get(user_id)
    if not definition:
        raise HTTPException(403, "This user has no S3 Tables bucket assignment")
    buckets = tuple(
        BucketScope(
            table_bucket_arn=item["table_bucket_arn"],
            namespace=item.get("namespace", NAMESPACE),
            label=item.get("label", item["table_bucket_arn"].rsplit("/", 1)[-1]),
        )
        for item in definition.get("buckets", [])
    )
    if not buckets and not bool(definition.get("is_admin", False)):
        raise HTTPException(403, "This user has no S3 Tables bucket assignment")
    is_admin = bool(definition.get("is_admin", False))
    return PilotUser(
        user_id=user_id,
        is_admin=is_admin,
        can_view_upload_history=is_admin or bool(definition.get("can_view_upload_history", False)),
        can_rollback_uploads=is_admin or bool(definition.get("can_rollback_uploads", False)),
        buckets=buckets,
    )


def _require_scope(user: PilotUser, table_bucket_arn: str, namespace: str) -> BucketScope:
    if user.is_admin:
        bucket = _require_bucket_access(user, table_bucket_arn)
        try:
            s3tables.get_namespace(tableBucketARN=table_bucket_arn, namespace=namespace)
        except ClientError as error:
            raise HTTPException(
                403, "The selected S3 Tables bucket or namespace is not visible to this administrator"
            ) from error
        return BucketScope(table_bucket_arn=table_bucket_arn, namespace=namespace, label=bucket["label"])
    scope = next((item for item in user.buckets if item.table_bucket_arn == table_bucket_arn and item.namespace == namespace), None)
    if not scope:
        raise HTTPException(403, "The selected S3 Tables bucket or namespace is not assigned to this user")
    return scope


def _discover_table_buckets() -> list[dict[str, str]]:
    """Return all account-visible customer table buckets for administrator navigation."""
    buckets: list[dict[str, str]] = []
    request: dict[str, str] = {}
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
    return sorted(buckets, key=lambda item: item["label"])


def _discover_namespaces(table_bucket_arn: str) -> list[str]:
    """Return one-level namespace names supported by this uploader's API contract."""
    namespaces: list[str] = []
    request: dict[str, str] = {"tableBucketARN": table_bucket_arn}
    while True:
        response = s3tables.list_namespaces(**request)
        for item in response.get("namespaces", []):
            value = item.get("namespace", [])
            if isinstance(value, list) and len(value) == 1 and value[0]:
                namespaces.append(value[0])
        token = response.get("continuationToken")
        if not token:
            break
        request = {"tableBucketARN": table_bucket_arn, "continuationToken": token}
    return sorted(set(namespaces))


def _require_bucket_access(user: PilotUser, table_bucket_arn: str) -> dict[str, str]:
    if user.is_admin:
        try:
            bucket = s3tables.get_table_bucket(tableBucketARN=table_bucket_arn)
        except ClientError as error:
            raise HTTPException(403, "The selected S3 Tables bucket is not visible to this administrator") from error
        return {
            "table_bucket_arn": bucket.get("arn", table_bucket_arn),
            "label": bucket.get("name", table_bucket_arn.rsplit("/", 1)[-1]),
        }
    else:
        scope = next((item for item in user.buckets if item.table_bucket_arn == table_bucket_arn), None)
        if scope:
            return {"table_bucket_arn": scope.table_bucket_arn, "label": scope.label}
        raise HTTPException(403, "The selected S3 Tables bucket is not assigned to this user")


def _canonical_table_name(value: str) -> str:
    """Translate UI-friendly hyphens to the S3 Tables identifier convention."""
    return value.strip().replace("-", "_")


class IngestionRequest(BaseModel):
    mode: Literal["create", "append"]
    table: str = Field(pattern=r"^[a-z][a-z0-9_]{0,254}$")
    table_bucket_arn: str = Field(min_length=1)
    namespace: str = Field(pattern=r"^[a-z][a-z0-9_]{0,254}$")
    request_id: str
    # Backward-compatible audit field name.  It is now a free-form user tag,
    # not a calendar month.
    reporting_month: str = Field(min_length=1, max_length=256)
    type_overrides: dict[str, str] = Field(default_factory=dict)
    deduplication_columns: list[str] = Field(default_factory=list)
    deduplication_mode: Literal["none", "keyed", "legacy-full-row"] = "legacy-full-row"
    key_analysis_token: str | None = Field(default=None, max_length=8192)
    manual_encryption_columns: list[str] = Field(default_factory=list)
    @field_validator("table", mode="before")
    @classmethod
    def canonicalise_table(cls, value: str) -> str:
        return _canonical_table_name(value)


class KeyAnalysisRequest(BaseModel):
    table_bucket_arn: str = Field(min_length=1)
    namespace: str = Field(pattern=r"^[a-z][a-z0-9_]{0,254}$")
    table: str = Field(pattern=r"^[a-z][a-z0-9_]{0,254}$")
    type_overrides: dict[str, str] = Field(default_factory=dict)
    deduplication_columns: list[str] = Field(default_factory=list)
    @field_validator("table", mode="before")
    @classmethod
    def canonicalise_table(cls, value: str) -> str:
        return _canonical_table_name(value)


class SessionKeyImpactRequest(BaseModel):
    type_overrides: dict[str, str] = Field(default_factory=dict)
    deduplication_columns: list[str] = Field(default_factory=list)


class SessionIngestionRequest(BaseModel):
    request_id: str = Field(min_length=1, max_length=128)
    reporting_month: str = Field(min_length=1, max_length=256)
    type_overrides: dict[str, str] = Field(default_factory=dict)
    deduplication_mode: Literal["none", "keyed"] = "keyed"
    deduplication_columns: list[str] = Field(default_factory=list)
    key_analysis_token: str | None = Field(default=None, max_length=8192)
    manual_encryption_columns: list[str] = Field(default_factory=list)


class DeleteTableRequest(BaseModel):
    table: str = Field(pattern=r"^[a-z][a-z0-9_]{0,254}$")
    table_bucket_arn: str = Field(min_length=1)
    namespace: str = Field(pattern=r"^[a-z][a-z0-9_]{0,254}$")


class RollbackRequest(BaseModel):
    table: str = Field(pattern=r"^[a-z][a-z0-9_]{0,254}$")
    table_bucket_arn: str = Field(min_length=1)
    namespace: str = Field(pattern=r"^[a-z][a-z0-9_]{0,254}$")
    upload_id: str = Field(min_length=1, max_length=128)
    confirm: bool = False


class CreateTableBucketRequest(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9-]{3,63}$")


class CreateNamespaceRequest(BaseModel):
    table_bucket_arn: str = Field(min_length=1)
    namespace: str = Field(pattern=r"^[a-z][a-z0-9_]{0,254}$")


def _admin_only(user: PilotUser) -> None:
    if not user.is_admin:
        raise HTTPException(403, "Only administrators may create S3 Tables buckets and namespaces")


def _control_plane_http_error(error: ClientError, resource: str) -> HTTPException:
    """Convert expected AWS control-plane failures into useful API responses."""
    details = error.response.get("Error", {})
    code = details.get("Code", "")
    message = details.get("Message") or f"AWS could not create the {resource}"
    if code in {"ConflictException", "AlreadyExistsException"}:
        status = 409
    elif code in {"AccessDenied", "AccessDeniedException", "UnauthorizedException"}:
        status = 403
    elif code in {"BadRequestException", "ValidationException"}:
        status = 400
    else:
        status = 502
    return HTTPException(status, message)


def _table_lock_http_error(error: TableLockedError) -> HTTPException:
    details = error.details
    return HTTPException(
        409,
        detail={
            "code": "TABLE_LOCKED",
            "message": "The selected table is busy with another uploader operation.",
            "operation": details.get("operation"), "phase": details.get("phase"),
            "owner": details.get("user_id"), "acquired_at": details.get("acquired_at"),
            "lease_expires_at": details.get("lease_expires_at"),
        },
        headers={"Retry-After": "300"},
    )


def _acquire_table_mutation_lock(*, table_bucket_arn: str, namespace: str, table: str,
                                 user: PilotUser, request_id: str, operation: str,
                                 session_id: str | None = None) -> TableLease:
    try:
        return table_locks.acquire(
            table_bucket_arn=table_bucket_arn, namespace=namespace, table=table,
            owner_token=uuid.uuid4().hex, user_id=user.user_id, request_id=request_id,
            session_id=session_id, operation=operation, phase="STARTING",
        )
    except TableLockedError as error:
        raise _table_lock_http_error(error) from error
    except TableLockError as error:
        raise HTTPException(503, "The uploader could not obtain its required table mutation lock") from error


def _release_table_mutation_lock(job_run_id: str) -> None:
    lease = active_table_leases.pop(job_run_id, None)
    if not lease:
        return
    try:
        table_locks.release(lease)
        logger.info("table_lock_released", extra={"job_run_id": job_run_id, "phase": "TERMINAL"})
    except TableLockError:
        safe_error(logger, "table_lock_release_failed", job_run_id=job_run_id, phase="TERMINAL")


def _soc_schema() -> list[dict[str, str]]:
    return [{"name": column, "type": "TIMESTAMP" if column in TIMESTAMP_TARGET_COLUMNS else "BIGINT" if column == "cnt" else "STRING"} for column in TARGET_COLUMNS]


def _contract_key(table_bucket_arn: str, namespace: str, table: str) -> str:
    scope = hashlib.sha256(f"{table_bucket_arn}|{namespace}".encode()).hexdigest()[:16]
    return f"{WEB_CONTRACT_PREFIX}/{scope}/{table}.json"


def _history_prefix(table_bucket_arn: str, namespace: str, table: str) -> str:
    """Return the request-scoped, value-free upload-history projection prefix."""
    scope = hashlib.sha256(f"{table_bucket_arn}|{namespace}".encode()).hexdigest()[:16]
    return f"{WEB_HISTORY_PREFIX}/{scope}/{table}/"


def _history_entries(table_bucket_arn: str, namespace: str, table: str) -> list[dict]:
    """Load the latest immutable audit projection for each upload; admin only."""
    entries: list[dict] = []
    paginator = s3.get_paginator("list_objects_v2")
    prefix = _history_prefix(table_bucket_arn, namespace, table)
    for page in paginator.paginate(Bucket=SOURCE_BUCKET, Prefix=prefix):
        for item in page.get("Contents", []):
            try:
                entry = json.loads(s3.get_object(Bucket=SOURCE_BUCKET, Key=item["Key"])["Body"].read())
            except Exception:
                continue
            if (
                entry.get("table_bucket_arn") == table_bucket_arn
                and entry.get("namespace") == namespace
                and entry.get("target_table") == table
            ):
                entries.append(entry)
    return sorted(entries, key=lambda item: item.get("uploaded_at") or "", reverse=True)


def _upload_id() -> str:
    """Technical identifier deliberately independent of free-form user tags."""
    return f"UPLOAD-{uuid.uuid4().hex[:12].upper()}"


def _configure_snapshot_retention(table_bucket_arn: str, namespace: str, table: str) -> None:
    """Configure S3 Tables retention from the local service's current boto3.

    Glue 5's bundled boto3 may not yet include the S3 Tables service model, so
    this control-plane operation must not run inside the Glue data-plane job.
    """
    s3tables.put_table_maintenance_configuration(
        tableBucketARN=table_bucket_arn,
        namespace=namespace,
        name=table,
        type="icebergSnapshotManagement",
        value={"status": "enabled", "settings": {"icebergSnapshotManagement": SNAPSHOT_RETENTION}},
    )


def _load_contract_record(table_bucket_arn: str, namespace: str, table: str) -> dict:
    """Read a table contract and lazily project legacy fields into v2."""
    if table == "soc":
        return {"contract_version": 1, "schema": _soc_schema(), "deduplication_columns": [], "deduplication_mode": "legacy-full-row", "deduplication_policy": "legacy-full-row-v1"}
    try:
        response = s3.get_object(Bucket=SOURCE_BUCKET, Key=_contract_key(table_bucket_arn, namespace, table))
    except s3.exceptions.NoSuchKey as error:
        # Backward-compatible lookup for contracts created by the first local UI.
        if table_bucket_arn == TABLE_BUCKET_ARN and namespace == NAMESPACE:
            try:
                response = s3.get_object(Bucket=SOURCE_BUCKET, Key=f"{WEB_CONTRACT_PREFIX}/{table}.json")
            except s3.exceptions.NoSuchKey:
                raise HTTPException(400, f"No local pilot schema contract is available for table {table!r}") from error
        else:
            raise HTTPException(400, f"No local pilot schema contract is available for table {table!r}") from error
    record = json.loads(response["Body"].read())
    if not record.get("schema"):
        raise HTTPException(400, f"The stored table contract for {table!r} has no schema")
    # Empty legacy keys are deliberately key-unconfigured. They never become
    # an implicit full-row contract for new v2 requests.
    record.setdefault("deduplication_columns", [])
    record.setdefault("contract_version", 1)
    record.setdefault("deduplication_mode", "keyed" if record["deduplication_columns"] else "legacy-full-row")
    record.setdefault("deduplication_policy", "skip-existing-key-report-conflict-v1" if record["deduplication_columns"] else "legacy-full-row-v1")
    record.setdefault("manual_encryption_columns", [])
    record.setdefault("automatic_sanitization_columns", [])
    return record


def _load_contract(table_bucket_arn: str, namespace: str, table: str) -> list[dict[str, str]]:
    return _load_contract_record(table_bucket_arn, namespace, table)["schema"]


def _activate_late_deduplication_contract(
    table_bucket_arn: str, namespace: str, table: str, contract: dict,
    columns: list[str], user_id: str,
) -> None:
    """Prospectively assign the first immutable key to an older no-key table."""
    current = list(contract.get("deduplication_columns") or [])
    if current:
        if current != columns:
            raise HTTPException(409, "This table already has an immutable composite de-duplication key")
        return
    updated = {
        **contract,
        "contract_version": 2,
        "deduplication_columns": columns,
        "deduplication_mode": "keyed",
        "deduplication_policy": "skip-existing-key-report-conflict-v2",
        "deduplication_activated_by": user_id,
        "deduplication_activated_at": datetime.now(timezone.utc).isoformat(),
    }
    key = _contract_key(table_bucket_arn, namespace, table)
    # The uploader lease serializes its own writers; this ETag condition also
    # prevents a concurrent administrative or future service writer from
    # silently overwriting the first immutable-key decision.
    try:
        etag = s3.head_object(Bucket=SOURCE_BUCKET, Key=key).get("ETag", "").strip('"')
        s3.put_object(
            Bucket=SOURCE_BUCKET, Key=key, Body=json.dumps(updated, indent=2).encode(),
            ContentType="application/json", ServerSideEncryption="AES256", IfMatch=etag,
        )
    except ClientError as error:
        code = error.response.get("Error", {}).get("Code", "")
        if code in {"PreconditionFailed", "ConditionalRequestConflict", "412"}:
            raise HTTPException(409, "The table contract changed while assigning its first composite key; refresh and try again") from error
        raise HTTPException(503, "Unable to update the immutable table de-duplication contract") from error


def _is_uploader_managed_table(table_bucket_arn: str, namespace: str, table: str) -> bool:
    """Whether this table has the uploader's schema and recovery contract."""
    if table_bucket_arn == TABLE_BUCKET_ARN and namespace == NAMESPACE and table == "soc":
        return True
    try:
        s3.head_object(Bucket=SOURCE_BUCKET, Key=_contract_key(table_bucket_arn, namespace, table))
        return True
    except Exception:
        return False


def _temporary_suffix(filename: str) -> str:
    return ".parquet" if filename.lower().endswith((".parquet", ".parquet.gzip")) else Path(filename).suffix


@contextmanager
def _temporary_upload_path(upload: UploadFile) -> Iterator[Path]:
    """Copy an upload to a closed local file before opening or deleting it.

    Windows does not allow another reader or ``unlink`` to operate reliably on
    a still-open ``NamedTemporaryFile``. Keep creation/writing in the inner
    context, then yield only after that handle has closed.
    """
    temp = tempfile.NamedTemporaryFile(
        suffix=_temporary_suffix(upload.filename or "upload"), delete=False
    )
    path = Path(temp.name)
    try:
        with temp:
            shutil.copyfileobj(upload.file, temp)
            temp.flush()
        yield path
    finally:
        path.unlink(missing_ok=True)
        upload.file.seek(0)


def _ensure_upload_archive_lifecycle() -> None:
    """Retain the sanitized Glue staging archive for 30 days, by prefix only."""
    try:
        current = s3.get_bucket_lifecycle_configuration(Bucket=SOURCE_BUCKET)
        rules = list(current.get("Rules", []))
    except s3.exceptions.NoSuchLifecycleConfiguration:
        rules = []
    desired = {
        "ID": UPLOAD_ARCHIVE_LIFECYCLE_RULE_ID,
        "Status": "Enabled",
        "Filter": {"Prefix": f"{WEB_UPLOAD_PREFIX}/"},
        "Expiration": {"Days": UPLOAD_ARCHIVE_RETENTION_DAYS},
    }
    # Also remove the superseded short-lived duplicate-backup rule. Other
    # bucket lifecycle rules remain untouched.
    replacement = [rule for rule in rules if rule.get("ID") not in {
        UPLOAD_ARCHIVE_LIFECYCLE_RULE_ID,
        "agentcore-s3tables-upload-archive-30-years",
        "agentcore-s3tables-sanitized-upload-backups-30-days",
    }]
    replacement.append(desired)
    if rules != replacement:
        s3.put_bucket_lifecycle_configuration(
            Bucket=SOURCE_BUCKET, LifecycleConfiguration={"Rules": replacement}
        )


def _read_upload_table(path: Path, filename: str) -> pa.Table:
    lower = filename.lower()
    if lower.endswith((".parquet", ".parquet.gzip")):
        return pq.read_table(path)
    if lower.endswith(".csv"):
        frame = pd.read_csv(path)
    elif lower.endswith(".tsv"):
        frame = pd.read_csv(path, sep="\t")
    elif lower.endswith((".xlsx", ".xls")):
        frame = pd.read_excel(path, engine="xlrd" if lower.endswith(".xls") else "openpyxl")
    else:
        raise ValueError(f"Unsupported file type: {filename}")
    # Excel and CSV object columns can legitimately contain a mix such as
    # 671 and "672A". PyArrow otherwise chooses int64 from the first value and
    # rejects the later text value. Keep native types where Arrow can infer
    # them, but use a string column whenever the source is genuinely mixed.
    for name in frame.columns:
        series = frame[name]
        if not pd.api.types.is_object_dtype(series.dtype):
            continue
        try:
            pa.array(series, from_pandas=True)
        except (pa.ArrowInvalid, pa.ArrowTypeError):
            frame[name] = series.map(lambda value: None if pd.isna(value) else str(value)).astype("string")
    return pa.Table.from_pandas(frame, preserve_index=False)


def _sanitization_details(schema: pa.Schema) -> tuple[pa.Schema, dict]:
    sanitized, plan = sanitised_schema(schema)
    return sanitized, {
        "dropped_columns": list(plan.drop_columns),
        "encrypted_columns": list(plan.identifier_columns),
        "postal_columns": list(plan.postal_columns),
        "age_banded_columns": list(plan.age_columns),
    }


def _nric_sanitization_review(upload: UploadFile) -> dict:
    """Return value-free automatic NRIC detection for the preflight UI."""
    with _temporary_upload_path(upload) as path:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(8 * 1024 * 1024):
                digest.update(chunk)
        table = _read_upload_table(path, upload.filename or "upload")
        columns, details = detect_nric_columns(table, digest.hexdigest())
    return {"nric_detected_columns": list(columns), "nric_detection": details}


def _read_schemas(files: list[UploadFile]) -> tuple[list[pa.Schema], list[dict]]:
    schemas, sanitization = [], []
    for upload in files:
        if not upload.filename or not upload.filename.lower().endswith(SUPPORTED_UPLOAD_SUFFIXES):
            raise HTTPException(400, f"Supported files are Parquet, XLSX, XLS, CSV, and TSV: {upload.filename or '<unnamed>'}")
        try:
            with _temporary_upload_path(upload) as path:
                schema, details = _sanitization_details(_read_upload_table(path, upload.filename).schema)
                schemas.append(schema)
                sanitization.append(details)
        except Exception as error:
            raise HTTPException(400, f"Cannot read {upload.filename} as Parquet: {error}") from error
    return schemas, sanitization


def _first_upload_contract(upload: UploadFile) -> tuple[list[dict[str, str]], list[str], set[str]]:
    """Profile every populated value of the first upload for table creation."""
    with _temporary_upload_path(upload) as path:
        table = _read_upload_table(path, upload.filename or "upload")
        sanitized_schema, plan = sanitised_schema(table.schema)
        forced_strings = set(plan.identifier_columns) | set(plan.postal_columns) | set(plan.age_columns)
        schema, warnings = schema_from_table(table, sanitized_schema, forced_strings)
        return schema, warnings, manual_confirmation_columns(table, sanitized_schema, forced_strings)


def _create_type_selections(
    comparisons: list[dict], target: list[dict[str, str]], manual_columns: set[str],
) -> list[dict[str, str | list[str] | bool]]:
    """Expose first-file conversion choices which define a new table contract.

    The first file alone defines a new table.  Later files in that same
    request must conform to the final selected contract, but must not create
    duplicate or competing type choices in the browser.
    """
    selections = []
    target_by_name = {field["name"]: field for field in target}
    first_comparison = comparisons[0] if comparisons else {"type_conversions": []}
    conversion_by_name = {item["column"]: item for item in first_comparison["type_conversions"]}
    for name in sorted(manual_columns):
        conversion = conversion_by_name.get(name, {"source_type": "STRING"})
        selections.append({
            "column": name,
            "source_type": conversion["source_type"],
            "suggested_target_type": target_by_name[name]["type"],
            "allowed_target_types": list(SELECTABLE_ICEBERG_TYPES),
            "locked": False,
        })
    return selections


def _create_deduplication_candidates(upload: UploadFile, target: list[dict[str, str]]) -> list[dict]:
    """Return every stored first-upload column with ephemeral safe examples."""
    with _temporary_upload_path(upload) as path:
        source = _read_upload_table(path, upload.filename or "upload")
        _, plan = sanitised_schema(source.schema)
        sensitive_sources = set(plan.drop_columns) | set(plan.identifier_columns) | set(plan.postal_columns) | set(plan.age_columns)
        source_by_canonical = dict(zip(normalise_names(source.schema.names), source.schema.names))
        candidates = []
        for field in target:
            source_name = source_by_canonical.get(field["name"])
            masked = source_name in sensitive_sources
            values = []
            non_null_count = 0
            if source_name and not masked:
                column = source[source_name]
                # Keep preflight bounded even for large healthcare files:
                # Arrow calculates counts natively, while sampling reads a
                # small random set of scalars rather than materialising an
                # entire column as a Python list.
                non_null_count = int(pc.count(column).as_py())
                indexes = random.SystemRandom().sample(range(len(column)), min(1024, len(column)))
                for index in indexes:
                    value = column[index].as_py()
                    if value is not None and str(value).strip().lower() not in {"", "nan", "none", "nat"}:
                        values.append(str(value)[:160])
                        if len(values) == 5:
                            break
            candidates.append({
                "column": field["name"], "target_type": field["type"],
                "source_type": str(source.schema.field(source_name).type) if source_name else "MISSING",
                "sample_values": values, "samples_masked": bool(masked),
                # The uploader normalises then encrypts identifiers using
                # the configured stable legacy-compatible representation.
                # They are therefore valid case-level keys, but values
                # remain masked in the browser.
                "deduplication_eligible": True,
                "deduplication_ineligible_reason": None,
                # Quality is metadata only. Sensitive fields deliberately
                # do not expose their value distribution to the browser.
                "non_null_count": non_null_count if not masked else None,
                # Distinct cardinality is intentionally deferred until the
                # user asks for composite-key analysis.  Doing it for every
                # candidate column makes first-upload review unnecessarily
                # expensive on multi-million-row files.
                "distinct_non_null_count": None,
            })
        return candidates


def _create_type_selection_samples(upload: UploadFile, selections: list[dict]) -> list[dict]:
    """Attach a few privacy-safe, non-persistent examples to type choices.

    Samples exist only in the HTTP preflight response.  They are deliberately
    excluded from the staging manifest, Glue arguments, QC reports, and upload
    history.  Any column covered by healthcare sanitisation is represented by a
    masked notice rather than a source value.
    """
    if not selections:
        return selections
    samples = {item["column"]: item for item in _create_deduplication_candidates(upload, [
        {"name": item["column"], "type": item.get("suggested_target_type", item.get("source_type", "STRING"))}
        for item in selections
    ])}
    with _temporary_upload_path(upload) as path:
        source = _read_upload_table(path, upload.filename or "upload")
        source_by_canonical = dict(zip(normalise_names(source.schema.names), source.schema.names))
        for choice in selections:
            source_name = source_by_canonical.get(choice["column"])
            impacts: dict[str, dict[str, int | str]] = {}
            if source_name:
                series = source[source_name].to_pandas()
                non_null = series.notna()
                for target_type, parser in (("DATE", parse_documented_date), ("TIMESTAMP", parse_documented_timestamp)):
                    converted = series.map(parser)
                    invalid_count = int((non_null & converted.isna()).sum())
                    if invalid_count:
                        impacts[target_type] = {
                            "invalid_value_count": invalid_count,
                            "behaviour": "invalid_values_become_null",
                        }
            choice["lossy_target_types"] = impacts
    for choice in selections:
        choice.update({key: samples[choice["column"]][key] for key in ("sample_values", "samples_masked")})
    return selections


def _validate_create_deduplication_columns(preview: dict, columns: list[str]) -> list[str]:
    """Require a non-empty, known, canonical key selection for new tables."""
    if not columns:
        raise HTTPException(422, "Choose at least one de-duplication column for a new table")
    if len(columns) != len(set(columns)):
        raise HTTPException(422, "A de-duplication column may be selected only once")
    candidates = {item["column"]: item for item in preview.get("deduplication_candidates", [])}
    available = set(candidates)
    invalid = sorted(set(columns) - available)
    if invalid:
        raise HTTPException(422, f"Unknown de-duplication column: {', '.join(invalid)}")
    ineligible = [column for column in columns if not candidates[column].get("deduplication_eligible", True)]
    if ineligible:
        raise HTTPException(422, f"These columns cannot be used for de-duplication: {', '.join(ineligible)}")
    return columns


def _v2_deduplication_columns(preview: dict, contract: dict, mode: str, columns: list[str]) -> list[str]:
    """Resolve upload-scoped de-duplication without silently changing a key."""
    if mode in {"none", "legacy-full-row"}:
        return []
    configured = list(contract.get("deduplication_columns") or [])
    if configured:
        if columns and columns != configured:
            raise HTTPException(422, "This table already has an immutable composite de-duplication key")
        return configured
    return _validate_create_deduplication_columns(preview, columns)


def _validate_key_analysis_columns(columns: list[str]) -> list[str]:
    """Validate only the client-selected raw key names for the fast review."""
    if not columns:
        raise HTTPException(422, "Choose at least one de-duplication column for key analysis")
    if len(columns) != len(set(columns)):
        raise HTTPException(422, "A de-duplication column may be selected only once")
    return columns


def _apply_create_type_overrides(preview: dict, overrides: dict[str, str]) -> list[dict[str, str]]:
    """Validate user choices and return the immutable initial table contract."""
    choices = {item["column"]: item for item in preview.get("type_selections", [])}
    invalid = sorted(set(overrides) - set(choices))
    if invalid:
        raise HTTPException(422, f"Type selection is not available for: {', '.join(invalid)}")
    result = []
    for field in preview["target_schema"]:
        selected = overrides.get(field["name"], field["type"])
        choice = choices.get(field["name"])
        if choice and (choice["locked"] or selected not in SELECTABLE_ICEBERG_TYPES):
            raise HTTPException(422, f"Invalid target type selection for {field['name']}")
        result.append({**field, "type": selected})
    return result


def _unsafe_cast_issues(upload: UploadFile, target: list[dict[str, str]]) -> list[dict[str, int | str]]:
    """Return value-level casts Spark would turn into NULL, without values.

    This keeps a bad append out of temporary S3 and Glue.  It deliberately
    returns only column names and counts: raw healthcare values never enter the
    preflight response or logs.
    """
    with _temporary_upload_path(upload) as path:
        table = _read_upload_table(path, upload.filename or "upload")
        sanitized_schema, plan = sanitised_schema(table.schema)
        normalized = normalise_names([field.name for field in sanitized_schema])
        source_columns = dict(zip(normalized, sanitized_schema.names))
        sensitive = set(plan.identifier_columns) | set(plan.postal_columns) | set(plan.age_columns)
        target_by_name = {field["name"]: field["type"] for field in target}
        issues = []
        for name, source_name in source_columns.items():
            target_type = target_by_name.get(name)
            if not target_type or source_name in sensitive:
                # Sanitization converts these selected source fields to
                # STRING before Glue sees them.
                continue
            source = table[source_name]
            source_type = source.type
            if target_type == "STRING" or (
                target_type == "BIGINT" and pa.types.is_integer(source_type)
            ) or (
                target_type == "DOUBLE" and (pa.types.is_integer(source_type) or pa.types.is_floating(source_type))
            ) or (
                target_type == "TIMESTAMP" and pa.types.is_timestamp(source_type)
            ) or (
                target_type == "DATE" and pa.types.is_date(source_type)
            ):
                continue
            series = source.to_pandas()
            non_null = series.notna()
            if target_type in {"BIGINT", "DOUBLE"}:
                converted = pd.to_numeric(series, errors="coerce")
                if target_type == "BIGINT":
                    converted = converted.where((converted % 1) == 0)
            elif target_type == "DATE":
                converted = series.map(parse_documented_date)
            elif target_type == "TIMESTAMP":
                converted = series.map(parse_documented_timestamp)
            elif target_type == "BOOLEAN":
                converted = series.astype("string").str.strip().str.lower().isin({"true", "false", "0", "1"})
                invalid = non_null & ~converted
                count = int(invalid.sum())
                if count:
                    issues.append({"column": name, "source_type": str(source_type), "target_type": target_type, "unsafe_value_count": count})
                continue
            else:
                continue
            invalid = non_null & converted.isna()
            count = int(invalid.sum())
            if count:
                issues.append({"column": name, "source_type": str(source_type), "target_type": target_type, "unsafe_value_count": count})
        return issues


def _sign_key_analysis(payload: dict) -> str:
    """Create a short-lived, tamper-evident acknowledgement token."""
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    secret = os.environ.get("PILOT_KEY_ANALYSIS_SECRET", "local-pilot-key-analysis-secret").encode()
    signature = hmac.new(secret, encoded, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(encoded + b"." + signature).decode()


def _read_key_analysis_token(token: str) -> dict:
    try:
        raw = base64.urlsafe_b64decode(token.encode())
        # The HMAC is arbitrary bytes and can itself contain ``.``. Its length
        # is fixed for SHA-256, so split at the known delimiter position rather
        # than searching the signature payload.
        if len(raw) <= hashlib.sha256().digest_size or raw[-33:-32] != b".":
            raise ValueError("missing token delimiter")
        encoded, signature = raw[:-33], raw[-32:]
    except Exception as error:
        raise HTTPException(422, "The key-impact analysis acknowledgement is invalid; run it again") from error
    secret = os.environ.get("PILOT_KEY_ANALYSIS_SECRET", "local-pilot-key-analysis-secret").encode()
    expected = hmac.new(secret, encoded, hashlib.sha256).digest()
    if not hmac.compare_digest(signature, expected):
        raise HTTPException(422, "The key-impact analysis acknowledgement is invalid; run it again")
    try:
        value = json.loads(encoded)
    except json.JSONDecodeError as error:
        raise HTTPException(422, "The key-impact analysis acknowledgement is invalid; run it again") from error
    if int(value.get("expires_at", 0)) < int(datetime.now(timezone.utc).timestamp()):
        raise HTTPException(422, "The key-impact analysis has expired; run it again")
    return value


def _copy_upload_with_digest(upload: UploadFile) -> tuple[Path, str]:
    """Copy one browser file locally and return a digest without any S3 write."""
    with tempfile.NamedTemporaryFile(suffix=_temporary_suffix(upload.filename or "upload"), delete=False) as temp:
        path = Path(temp.name)
        digest = hashlib.sha256()
        while chunk := upload.file.read(8 * 1024 * 1024):
            digest.update(chunk)
            temp.write(chunk)
    upload.file.seek(0)
    return path, digest.hexdigest()


def _composite_key_metrics(frame: pd.DataFrame, key_columns: list[str]) -> dict[str, int]:
    """Calculate the exact policy outcome used by generic_glue_job, without values."""
    key_frame = frame[key_columns].astype("string").fillna("~").replace("", "~")
    key_hash = pd.util.hash_pandas_object(key_frame, index=False)
    row_hash = pd.util.hash_pandas_object(frame, index=False)
    grouped = pd.DataFrame({"key": key_hash, "row": row_hash}).groupby("key", sort=False).agg(
        rows=("row", "size"), variants=("row", "nunique")
    )
    exact = grouped[grouped["variants"] == 1]
    conflicts = grouped[grouped["variants"] > 1]
    total = int(len(frame))
    retained = int(len(exact))
    return {
        "incoming_rows": total,
        "unique_composite_keys": int(len(grouped)),
        "exact_duplicate_rows": int((exact["rows"] - 1).sum()),
        "conflicting_key_groups": int(len(conflicts)),
        "rows_in_conflicting_key_groups": int(conflicts["rows"].sum()),
        "expected_retained_rows": retained,
        "expected_skipped_rows": total - retained,
    }


def _raw_analysis_lazy_frame(path: Path, filename: str) -> pl.LazyFrame:
    """Read an upload locally for preliminary key analysis without sanitizing it.

    Parquet and delimited files retain Polars' lazy scan; spreadsheet formats
    necessarily use the existing reader once, then move into Polars. No raw
    bytes or values leave the local process in either case.
    """
    lower_name = filename.lower()
    if lower_name.endswith((".parquet", ".parquet.gzip")):
        return pl.scan_parquet(path)
    if lower_name.endswith((".csv", ".tsv")):
        # Key impact is deliberately an *untyped raw* comparison.  Letting
        # Polars infer a CSV schema can make the review fail when an early
        # numeric-looking value is followed by a valid mixed value such as
        # ``83%``.  Reading every delimited source field as text both preserves
        # the submitted values and avoids applying data-type policy before the
        # user has confirmed the key.
        separator = "\t" if lower_name.endswith(".tsv") else ","
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            header = next(csv.reader(stream, delimiter=separator), None)
        if not header:
            raise HTTPException(422, "The selected file has no header row for key analysis")
        return pl.scan_csv(
            path,
            separator=separator,
            schema_overrides={name: pl.String for name in header},
            infer_schema=False,
            try_parse_dates=False,
        )
    return pl.from_arrow(_read_upload_table(path, filename)).lazy()


def _raw_key_impact_metrics(paths: list[tuple[Path, str]], key_columns: list[str]) -> dict[str, int]:
    """Calculate incoming key impact using raw local values and Polars.

    This deliberately runs *before* sanitization, type projection, S3 staging,
    or Glue. Canonical column names and blank-key semantics match the uploader,
    but all full-row comparisons use source values only.
    """
    frames: list[tuple[pl.LazyFrame, dict[str, str]]] = []
    all_columns: set[str] = set()
    for path, filename in paths:
        frame = _raw_analysis_lazy_frame(path, filename)
        source_names = list(frame.collect_schema().names())
        canonical_names = normalise_names(source_names)
        lookup = dict(zip(canonical_names, source_names))
        frames.append((frame, lookup))
        all_columns.update(canonical_names)
    if not all_columns:
        raise HTTPException(422, "The selected file has no columns for key analysis")
    missing_keys = sorted(set(key_columns) - all_columns)
    if missing_keys:
        raise HTTPException(422, f"The selected key columns are not present in the upload: {', '.join(missing_keys)}")

    columns = sorted(all_columns)
    projected = []
    for frame, lookup in frames:
        projected.append(frame.select([
            pl.col(lookup[name]).cast(pl.String, strict=False).alias(name)
            if name in lookup else pl.lit(None, dtype=pl.String).alias(name)
            for name in columns
        ]))
    incoming = pl.concat(projected, how="vertical_relaxed")
    key_components = [
        pl.when(pl.col(name).is_null() | (pl.col(name).str.strip_chars() == ""))
        .then(pl.lit("~"))
        .otherwise(pl.col(name))
        .alias(name)
        for name in key_columns
    ]
    grouped = (
        incoming.with_columns(pl.struct(key_components).alias("__uploader_composite_key"))
        .group_by("__uploader_composite_key")
        .agg(
            pl.len().alias("rows"),
            pl.struct([pl.col(name) for name in columns]).n_unique().alias("variants"),
        )
    )
    summary = grouped.select(
        pl.col("rows").sum().alias("incoming_rows"),
        pl.len().alias("unique_composite_keys"),
        pl.when(pl.col("variants") == 1).then(pl.col("rows") - 1).otherwise(0).sum().alias("exact_duplicate_rows"),
        (pl.col("variants") > 1).sum().alias("conflicting_key_groups"),
        pl.when(pl.col("variants") > 1).then(pl.col("rows")).otherwise(0).sum().alias("rows_in_conflicting_key_groups"),
        (pl.col("variants") == 1).sum().alias("expected_retained_rows"),
    ).collect().row(0, named=True)
    total = int(summary["incoming_rows"] or 0)
    retained = int(summary["expected_retained_rows"] or 0)
    return {
        "incoming_rows": total,
        "unique_composite_keys": int(summary["unique_composite_keys"] or 0),
        "exact_duplicate_rows": int(summary["exact_duplicate_rows"] or 0),
        "conflicting_key_groups": int(summary["conflicting_key_groups"] or 0),
        "rows_in_conflicting_key_groups": int(summary["rows_in_conflicting_key_groups"] or 0),
        "expected_retained_rows": retained,
        "expected_skipped_rows": total - retained,
    }


def _analyse_selected_key(
    files: list[UploadFile], key_columns: list[str],
) -> tuple[dict[str, int], list[str]]:
    paths, file_digests = [], []
    for upload in files:
        path, digest = _copy_upload_with_digest(upload)
        paths.append((path, upload.filename or "upload"))
        file_digests.append(digest)
    try:
        return _raw_key_impact_metrics(paths, key_columns), file_digests
    finally:
        for path, _ in paths:
            path.unlink(missing_ok=True)


def _preflight(mode: str, table_bucket_arn: str, namespace: str, table: str, files: list[UploadFile]) -> dict:
    schemas, sanitization = _read_schemas(files)
    nric_reviews = [_nric_sanitization_review(upload) for upload in files]
    if not schemas:
        raise HTTPException(400, "Choose at least one Parquet file")
    if mode == "create":
        target, creation_warnings, manual_type_columns = _first_upload_contract(files[0])
        contract = {"schema": target, "deduplication_columns": [], "deduplication_policy": "skip-existing-key-report-conflict-v1"}
    else:
        contract = _load_contract_record(table_bucket_arn, namespace, table)
        target, creation_warnings, manual_type_columns = contract["schema"], [], set()
    comparisons = [compare_schema(schema, target) for schema in schemas]
    target_by_name = {field["name"]: field["type"] for field in target}
    incompatible_sensitive_columns = []
    for schema, details in zip(schemas, sanitization):
        source_fields, _ = schema_from_arrow(schema)
        source_by_original = {field["source_name"]: field["name"] for field in source_fields}
        for original in details["encrypted_columns"] + details["postal_columns"] + details["age_banded_columns"]:
            field_name = source_by_original.get(original)
            target_type = target_by_name.get(field_name)
            if target_type is not None and target_type != "STRING":
                incompatible_sensitive_columns.append({"column": field_name, "target_type": target_type})
    file_results = []
    rejection_reasons = []
    for upload, comparison, details, nric_review in zip(files, comparisons, sanitization, nric_reviews):
        file_rejection_reasons = []
        sanitized_columns = sorted(set(
            details["dropped_columns"]
            + details["encrypted_columns"]
            + details["postal_columns"]
            + details["age_banded_columns"]
            + nric_review["nric_detected_columns"]
        ))
        match_accepted = mode == "create" or comparison["matching_percentage"] >= MIN_APPEND_SCHEMA_MATCH_PERCENT
        if not match_accepted:
            reason = (
                f"{upload.filename}: only {comparison['matching_column_count']} of "
                f"{comparison['target_column_count']} initial-table columns match "
                f"({comparison['matching_percentage']:.1f}%). At least "
                f"{MIN_APPEND_SCHEMA_MATCH_PERCENT:.0f}% is required for an append."
            )
            rejection_reasons.append(reason)
            file_rejection_reasons.append(reason)
        unsafe_casts = _unsafe_cast_issues(upload, target) if mode == "append" else []
        if unsafe_casts:
            columns = ", ".join(
                f"{item['column']} ({item['unsafe_value_count']} invalid value{'s' if item['unsafe_value_count'] != 1 else ''})"
                for item in unsafe_casts
            )
            reason = (
                f"{upload.filename}: values cannot be safely converted to the existing table schema: {columns}."
            )
            rejection_reasons.append(reason)
            file_rejection_reasons.append(reason)
        file_results.append({
            "filename": upload.filename,
            **comparison,
            "sanitization": details,
            "nric_detection": nric_review["nric_detection"],
            "nric_detected_columns": nric_review["nric_detected_columns"],
            "sanitized_columns": sanitized_columns,
            "sanitized_column_count": len(sanitized_columns),
            "unsafe_casts": unsafe_casts,
            "accepted": match_accepted and not unsafe_casts,
            "rejection_reasons": file_rejection_reasons,
        })
    if incompatible_sensitive_columns:
        rejection_reasons.append(
            "The selected table has non-string sensitive columns and cannot accept encrypted or masked values."
        )
    type_selections = _create_type_selection_samples(
        files[0], _create_type_selections(comparisons, target, manual_type_columns)
    ) if mode == "create" else []
    automatic_encrypted = sorted(set(
        item for details, nric_review in zip(sanitization, nric_reviews)
        for item in details["encrypted_columns"] + nric_review["nric_detected_columns"]
    ))
    candidates = _create_deduplication_candidates(files[0], target) if (
        mode == "create" or not contract["deduplication_columns"]
    ) else []
    if mode == "append" and not contract["deduplication_columns"]:
        incoming_names = {field["name"] for field in schema_from_arrow(schemas[0])[0]}
        candidates = [candidate for candidate in candidates if candidate["column"] in incoming_names]
    automatic_or_transformed = {
        normalise_names([column])[0]
        for column in (
            sanitization[0]["dropped_columns"] + sanitization[0]["encrypted_columns"]
            + sanitization[0]["postal_columns"] + sanitization[0]["age_banded_columns"]
            + nric_reviews[0]["nric_detected_columns"]
        )
    }
    manual_candidates = [
        {"column": item["column"], "sample_values": item["sample_values"], "samples_masked": item["samples_masked"]}
        for item in candidates if item["column"] not in automatic_or_transformed
    ]
    return {
        "mode": mode, "table_bucket_arn": table_bucket_arn, "namespace": namespace, "table": table, "target_schema": target, "creation_warnings": creation_warnings,
        "initial_table_column_count": len(target),
        "minimum_append_schema_match_percent": MIN_APPEND_SCHEMA_MATCH_PERCENT,
        "files": file_results,
        "type_selections": type_selections,
        "deduplication_candidates": candidates,
        "deduplication_columns": contract["deduplication_columns"],
        "deduplication_policy": contract["deduplication_policy"],
        "incompatible_sensitive_columns": incompatible_sensitive_columns,
        "accepted": not rejection_reasons,
        "rejection_reasons": rejection_reasons,
        "sensitive_column_scan": "Sanitization is enforced before temporary S3 staging.",
        "sanitization_review": {
            "automatic_encrypted_columns": automatic_encrypted,
            "manual_encryption_candidates": manual_candidates,
            "nric_detection_policy": {"sample_size": 5, "match_threshold": 3, "kind": "sampled-heuristic-v1"},
        },
    }


def _normalise_temporal_column(
    column: pa.ChunkedArray, target_type: str, *, allow_invalid_values: bool = False,
) -> tuple[pa.Array, int]:
    """Apply the documented date/time rules before Spark sees the staged file."""
    series = column.to_pandas()
    if target_type == "DATE":
        parsed = series.map(parse_documented_date)
        invalid = series.notna() & parsed.isna()
        invalid_count = int(invalid.sum())
        if invalid_count and not allow_invalid_values:
            raise ValueError(f"DATE conversion would discard {int(invalid.sum())} value(s)")
        return pa.array(parsed.tolist(), type=pa.date32(), from_pandas=True), invalid_count
    if target_type == "TIMESTAMP":
        parsed = series.map(parse_documented_timestamp)
        invalid = series.notna() & parsed.isna()
        invalid_count = int(invalid.sum())
        if invalid_count and not allow_invalid_values:
            raise ValueError(f"TIMESTAMP conversion would discard {int(invalid.sum())} value(s)")
        return pa.array(parsed.tolist(), type=pa.timestamp("us"), from_pandas=True), invalid_count
    raise ValueError(f"Unsupported temporal target type: {target_type}")


def _make_glue_compatible_parquet(
    source: Path, filename: str, key=None, target_schema: list[dict[str, str]] | None = None,
    manual_encryption_columns: list[str] | None = None, nric_columns: list[str] | None = None,
    lossy_temporal_columns: set[str] | None = None,
) -> tuple[Path, bool, dict]:
    """Stage every supported file as Spark-safe Parquet for the Glue job."""
    table = _read_upload_table(source, filename)
    manual_encryption_columns = manual_encryption_columns or []
    nric_columns = nric_columns or []
    lossy_temporal_columns = lossy_temporal_columns or set()
    schema, plan = sanitised_schema(table.schema, set(manual_encryption_columns) | set(nric_columns))
    sanitization_required = bool(plan.drop_columns or plan.identifier_columns or plan.postal_columns or plan.age_columns or manual_encryption_columns or nric_columns)
    audit = {"dropped_columns": [], "encrypted_columns": [], "postal_columns": [], "age_banded_columns": [], "manual_encryption_columns": [], "nric_encrypted_columns": [], "newly_encrypted_values": 0, "already_encrypted_values": 0, "lossy_temporal_nulls": {}}
    if sanitization_required:
        table, audit = sanitise_table(table, key, manual_encryption_columns, nric_columns)
        # Sanitisation owns the main audit object; preserve the staging-only
        # conversion information added by this function.
        audit.setdefault("lossy_temporal_nulls", {})
    else:
        schema = table.schema
    names = normalise_names([field.name for field in schema])
    table = table.rename_columns(names)
    target_types = {field["name"]: field["type"] for field in (target_schema or [])}
    arrays = []
    for name in names:
        column = table[name]
        target_type = target_types.get(name)
        if target_type in {"DATE", "TIMESTAMP"}:
            parsed, invalid_count = _normalise_temporal_column(
                column, target_type, allow_invalid_values=name in lossy_temporal_columns,
            )
            if invalid_count:
                audit["lossy_temporal_nulls"][name] = invalid_count
            arrays.append(parsed)
        else:
            arrays.append(column)
    table = pa.table(arrays, names=names)
    has_nanosecond_timestamps = any(
        pa.types.is_timestamp(field.type) and field.type.unit == "ns" for field in schema
    )
    # Spark/Glue cannot read Parquet TIME(MICROS), which Arrow produces for
    # Excel cells containing only a time-of-day.  The table contract models
    # those fields as strings (for example, ``ATIME`` and ``SOPTIME``), so
    # serialise them as text before staging rather than emitting TIME(MICROS).
    has_time_of_day_values = any(pa.types.is_time(field.type) for field in schema)
    has_unsafe_names = names != list(schema.names)
    is_parquet = filename.lower().endswith((".parquet", ".parquet.gzip"))
    if is_parquet and not sanitization_required and not has_nanosecond_timestamps and not has_time_of_day_values and not has_unsafe_names:
        # Temporal fields need a rewritten Parquet payload even when the input
        # was already Parquet, because DATE/TIMESTAMP parsing is explicit.
        if not any(kind in {"DATE", "TIMESTAMP"} for kind in target_types.values()):
            return source, False, audit

    fields = [
        pa.field(
            name,
            pa.date32()
            if target_types.get(name) == "DATE"
            else pa.timestamp("us")
            if target_types.get(name) == "TIMESTAMP"
            else pa.timestamp("us", tz=field.type.tz)
            if pa.types.is_timestamp(field.type) and field.type.unit == "ns"
            else pa.string()
            if pa.types.is_time(field.type)
            else field.type,
            nullable=field.nullable,
            metadata=field.metadata,
        )
        for field, name in zip(schema, names)
    ]
    target_schema = pa.schema(fields, metadata=schema.metadata)
    table = table.cast(target_schema, safe=False)
    converted = source.with_name(f"{source.stem}-glue-compatible.parquet")
    pq.write_table(table, converted, compression="snappy")
    return converted, True, audit


def _ensure_web_job() -> None:
    script = ROOT / "generic_glue_job.py"
    s3.put_object(Bucket=SOURCE_BUCKET, Key=WEB_SCRIPT_KEY, Body=script.read_bytes(), ContentType="text/x-python", ServerSideEncryption="AES256")
    role_arn = iam.get_role(RoleName=ROLE_NAME)["Role"]["Arn"]
    conf = " ".join([
        "spark.sql.extensions=org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions",
        "--conf spark.sql.legacy.timeParserPolicy=CORRECTED",
        "--conf spark.sql.catalog.s3_rest_catalog=org.apache.iceberg.spark.SparkCatalog",
        "--conf spark.sql.catalog.s3_rest_catalog.type=rest",
        f"--conf spark.sql.catalog.s3_rest_catalog.uri=https://s3tables.{REGION}.amazonaws.com/iceberg",
        f"--conf spark.sql.catalog.s3_rest_catalog.warehouse={TABLE_BUCKET_ARN}",
        "--conf spark.sql.catalog.s3_rest_catalog.rest.sigv4-enabled=true",
        "--conf spark.sql.catalog.s3_rest_catalog.rest.signing-name=s3tables",
        f"--conf spark.sql.catalog.s3_rest_catalog.rest.signing-region={REGION}",
        "--conf spark.sql.catalog.s3_rest_catalog.io-impl=org.apache.iceberg.aws.s3.S3FileIO",
    ])
    definition = {
        "Role": role_arn, "Command": {"Name": "glueetl", "ScriptLocation": f"s3://{SOURCE_BUCKET}/{WEB_SCRIPT_KEY}", "PythonVersion": "3"},
        "GlueVersion": "5.0", "WorkerType": "G.1X", "NumberOfWorkers": 4, "Timeout": 60, "MaxRetries": 0,
        "ExecutionProperty": {"MaxConcurrentRuns": PILOT_GLUE_MAX_CONCURRENT_RUNS},
        "DefaultArguments": {"--job-language": "python", "--datalake-formats": "iceberg", "--enable-metrics": "true", "--enable-continuous-cloudwatch-log": "true", "--conf": conf},
        "Description": "Local web UI generic append-only S3 Tables pilot ingestion",
    }
    try:
        glue.get_job(JobName=WEB_JOB_NAME)
        glue.update_job(JobName=WEB_JOB_NAME, JobUpdate=definition)
    except glue.exceptions.EntityNotFoundException:
        glue.create_job(Name=WEB_JOB_NAME, **definition)


def _iceberg_row_count(metadata_uri: str | None) -> int | None:
    """Best-effort count from Iceberg metadata; listing must stay responsive."""
    if not metadata_uri or not metadata_uri.startswith("s3://"):
        return None
    try:
        bucket, key = metadata_uri.removeprefix("s3://").split("/", 1)
        metadata = json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
        current = metadata.get("current-snapshot-id")
        snapshot = next((item for item in metadata.get("snapshots", []) if item.get("snapshot-id") == current), None)
        count = (snapshot or {}).get("summary", {}).get("total-records")
        return int(count) if count is not None else None
    except Exception:
        return None


def _table_summary(table_bucket_arn: str, namespace: str, item: dict) -> dict:
    details = s3tables.get_table(tableBucketARN=table_bucket_arn, namespace=namespace, name=item["name"])
    return {
        "name": item["name"],
        "created_at": str(item.get("createdAt")),
        "modified_at": str(item.get("modifiedAt")),
        "row_count": _iceberg_row_count(details.get("metadataLocation")),
        "uploader_managed": _is_uploader_managed_table(table_bucket_arn, namespace, item["name"]),
    }


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/static/{asset}")
def static(asset: str):
    if asset not in {"app.js", "style.css"}:
        raise HTTPException(404)
    return FileResponse(ROOT / "static" / asset)


@app.get("/api/buckets")
def list_buckets(user: PilotUser = Depends(_current_user)):
    buckets = _discover_table_buckets() if user.is_admin else [
        {"table_bucket_arn": item.table_bucket_arn, "label": item.label}
        for item in user.buckets
    ]
    return {
        "user_id": user.user_id,
        "is_admin": user.is_admin,
        "can_view_upload_history": user.can_view_upload_history,
        "can_rollback_uploads": user.can_rollback_uploads,
        "buckets": list({item["table_bucket_arn"]: item for item in buckets}.values()),
    }


@app.post("/api/buckets", status_code=201)
def create_table_bucket(payload: CreateTableBucketRequest, user: PilotUser = Depends(_current_user)):
    _admin_only(user)
    try:
        result = s3tables.create_table_bucket(name=payload.name)
    except ClientError as error:
        raise _control_plane_http_error(error, "S3 Tables bucket") from error
    return {"table_bucket_arn": result["arn"], "label": payload.name}


@app.get("/api/namespaces")
def list_namespaces(table_bucket_arn: str, user: PilotUser = Depends(_current_user)):
    _require_bucket_access(user, table_bucket_arn)
    namespaces = _discover_namespaces(table_bucket_arn) if user.is_admin else sorted({
        item.namespace for item in user.buckets if item.table_bucket_arn == table_bucket_arn
    })
    return {"table_bucket_arn": table_bucket_arn, "namespaces": namespaces}


@app.post("/api/namespaces", status_code=201)
def create_namespace(payload: CreateNamespaceRequest, user: PilotUser = Depends(_current_user)):
    _admin_only(user)
    _require_bucket_access(user, payload.table_bucket_arn)
    try:
        result = s3tables.create_namespace(
            tableBucketARN=payload.table_bucket_arn,
            namespace=[payload.namespace],
        )
    except ClientError as error:
        raise _control_plane_http_error(error, "namespace") from error
    namespace = result.get("namespace", [payload.namespace])
    return {
        "table_bucket_arn": result.get("tableBucketARN", payload.table_bucket_arn),
        "namespace": namespace[0],
    }


@app.post("/api/skills/upload-bundle")
async def upload_skill_bundle(
    table_bucket_arn: str = Form(),
    paths_json: str = Form(),
    confirm_replace: bool = Form(),
    files: list[UploadFile] = File(),
    user: PilotUser = Depends(_current_user),
):
    """Replace an authorized bucket's skill bundle after local validation.

    The browser supplies the folder-relative paths separately because standard
    multipart filenames omit ``webkitRelativePath`` in some browsers.
    """
    _require_bucket_access(user, table_bucket_arn)
    if not confirm_replace:
        raise HTTPException(422, "Confirm replacement before publishing the skill bundle")
    try:
        paths = skill_bundle.parse_paths_json(paths_json)
        if len(paths) != len(files):
            raise skill_bundle.SkillBundleError("Each uploaded skill file must have one matching relative path")
        payload: list[tuple[str, bytes]] = []
        for path, upload in zip(paths, files, strict=True):
            payload.append((path, await upload.read(skill_bundle.MAX_FILE_BYTES + 1)))
        return skill_bundle.publish_bundle(table_bucket_arn, user.user_id, payload)
    except skill_bundle.SkillBundleError as error:
        raise HTTPException(error.status_code, str(error)) from error


@app.get("/api/dev/identity-profiles")
def local_identity_profiles():
    """Expose safe local-only test identities for the trusted-placeholder UI.

    This route exists only for the localhost pilot.  It describes configured
    access grants but never authenticates a user or accepts client-supplied
    roles.  Production must replace both this panel and ``_current_user`` with
    verified frontend identity claims.
    """
    definitions = _configured_users()
    profiles = []
    for user_id in LOCAL_TEST_USER_IDS:
        definition = definitions.get(user_id)
        if definition is None:
            continue
        profiles.append({
            "user_id": user_id,
            "is_admin": bool(definition.get("is_admin", False)),
            "can_view_upload_history": bool(definition.get("is_admin", False) or definition.get("can_view_upload_history", False)),
            "can_rollback_uploads": bool(definition.get("is_admin", False) or definition.get("can_rollback_uploads", False)),
            "buckets": definition.get("buckets", []),
            "expected_access": bool(definition.get("is_admin", False) or definition.get("buckets", [])),
        })
    return {
        "local_only": True,
        "header_name": IDENTITY_EMULATION_HEADER,
        "profiles": profiles,
        "note": "The browser sends only the user-ID header; the backend resolves roles and bucket grants.",
    }


@app.get("/api/identity")
def current_identity(
    x_pilot_user_id: str | None = Header(default=None, alias=IDENTITY_EMULATION_HEADER),
    user: PilotUser = Depends(_current_user),
):
    """Return the effective, backend-resolved identity for the local UI."""
    return {
        "user_id": user.user_id,
        "is_admin": user.is_admin,
        "can_view_upload_history": user.can_view_upload_history,
        "can_rollback_uploads": user.can_rollback_uploads,
        "scope_mode": "all-discoverable-buckets-and-namespaces" if user.is_admin else "configured-bucket-and-namespace-scopes",
        "buckets": [
            {"table_bucket_arn": item.table_bucket_arn, "namespace": item.namespace, "label": item.label}
            for item in user.buckets
        ],
        "request_context": {
            "header_name": IDENTITY_EMULATION_HEADER,
            "header_value": x_pilot_user_id or os.environ.get("PILOT_LOCAL_USER_ID", "local-admin"),
            "roles_and_grants_sent_by_browser": False,
        },
    }


@app.get("/api/tables")
def list_tables(table_bucket_arn: str, namespace: str, user: PilotUser = Depends(_current_user)):
    _require_scope(user, table_bucket_arn, namespace)
    result = s3tables.list_tables(tableBucketARN=table_bucket_arn, namespace=namespace)
    tables = [
        _table_summary(table_bucket_arn, namespace, item)
        for item in result.get("tables", [])
        if item["name"] != UPLOAD_HISTORY_TABLE
    ]
    return {
        "table_bucket": table_bucket_arn,
        "namespace": namespace,
        "is_admin": user.is_admin,
        "tables": sorted(tables, key=lambda item: item["name"]),
    }


@contextmanager
def _session_upload_files(session) -> Iterator[list[UploadFile]]:
    """Open the private session artifacts as UploadFile-compatible streams."""
    streams = []
    try:
        for file in session.files:
            stream = Path(file.path).open("rb")
            streams.append((stream, UploadFile(filename=file.name, file=stream)))
        yield [upload for _, upload in streams]
    finally:
        for stream, _ in streams:
            stream.close()


def _session_error(session_id: str, user_id: str, phase: str, message: str, error: Exception) -> None:
    error_id = safe_error(logger, "upload_session_failed", session_id=session_id, phase=phase)
    upload_sessions.update(
        session_id, user_id, phase="FAILED", progress_message=message,
        error={"code": "SESSION_PROCESSING_FAILED", "message": message, "error_id": error_id},
    )


@contextmanager
def _local_processing_slot(session_id: str, user_id: str, activity: str) -> Iterator[None]:
    """Bound CPU/memory-intensive local work and make any wait visible."""
    if not local_processing_slots.acquire(blocking=False):
        upload_sessions.update(
            session_id, user_id, phase="QUEUED",
            progress_message=f"Queued for local processing capacity before {activity}.",
        )
        local_processing_slots.acquire()
    try:
        yield
    finally:
        local_processing_slots.release()


def _report_ingestion_progress(request_id: str, message: str) -> None:
    """Publish a safe, human-readable preparation phase to a v2 session."""
    callback = ingestion_progress_hooks.get(request_id)
    if callback is None:
        return
    try:
        callback(message)
    except Exception:
        # Progress reporting must never break an otherwise valid upload.
        safe_error(logger, "upload_session_progress_update_failed", request_id=request_id)


def _profile_upload_session(session_id: str, user_id: str) -> None:
    """Profile a previously copied upload once per session, in the background."""
    try:
        with _local_processing_slot(session_id, user_id, "file profiling"):
            session = upload_sessions.update(session_id, user_id, phase="PROFILING", progress_message="Analysing file structure and proposed schema.")
            with _session_upload_files(session) as files:
                preview = _preflight(session.mode, session.table_bucket_arn, session.namespace, session.table, files)
        upload_sessions.update(
            session_id, user_id, phase="READY_FOR_REVIEW", progress_message="Data structure analysis is complete.",
            preflight=preview,
        )
        logger.info("upload_session_profiled", extra={"session_id": session_id, "file_count": len(session.files), "phase": "READY_FOR_REVIEW"})
    except Exception as error:
        _session_error(session_id, user_id, "PROFILING", "Data structure analysis failed.", error)


def _analyse_session_key_impact(session_id: str, user_id: str, payload: SessionKeyImpactRequest) -> None:
    """Run raw local Polars key analysis with no sanitization, S3, or Glue work."""
    try:
        with _local_processing_slot(session_id, user_id, "composite-key impact analysis"):
            session = upload_sessions.update(session_id, user_id, phase="KEY_ANALYSING", progress_message="Analysing the selected composite key locally.")
            key_columns = _validate_key_analysis_columns(payload.deduplication_columns)
            paths = [(Path(file.path), file.name) for file in session.files]
            metrics = _raw_key_impact_metrics(paths, key_columns)
        expires_at = int(datetime.now(timezone.utc).timestamp()) + 30 * 60
        acknowledgement = {
            "v": 2, "expires_at": expires_at, "user_id": user_id,
            "table_bucket_arn": session.table_bucket_arn, "namespace": session.namespace, "table": session.table,
            "type_overrides": payload.type_overrides, "deduplication_columns": key_columns,
            "file_digests": [file.sha256 for file in session.files], "session_id": session_id,
        }
        impact = {
            "metrics": metrics, "deduplication_columns": key_columns,
            # This lets a session refresh restore the exact choice represented
            # by the signed acknowledgement, without exposing sample values.
            "type_overrides": payload.type_overrides,
            "acknowledgement_token": _sign_key_analysis(acknowledgement),
            "expires_at": datetime.fromtimestamp(expires_at, timezone.utc).isoformat(),
            "no_storage_or_glue_side_effects": True, "analysis_basis": "raw-local-pre-sanitization",
        }
        upload_sessions.update(
            session_id, user_id, phase="READY_FOR_ACKNOWLEDGEMENT",
            progress_message="Composite-key impact analysis is complete; acknowledge it before upload.", key_impact=impact,
        )
        logger.info("upload_session_key_analysed", extra={"session_id": session_id, "phase": "READY_FOR_ACKNOWLEDGEMENT", "key_column_count": len(key_columns)})
    except Exception as error:
        _session_error(session_id, user_id, "KEY_ANALYSING", "Composite-key impact analysis failed.", error)


@app.post("/api/v2/upload-sessions", status_code=201)
async def create_upload_session(
    background_tasks: BackgroundTasks,
    mode: Literal["create", "append"] = Form(),
    table_bucket_arn: str = Form(),
    namespace: str = Form(),
    table: str = Form(),
    files: list[UploadFile] = File(),
    user: PilotUser = Depends(_current_user),
):
    """Copy uploads once into a private session and profile asynchronously."""
    table = _canonical_table_name(table)
    _require_scope(user, table_bucket_arn, namespace)
    if not files:
        raise HTTPException(400, "Choose at least one supported file")
    if table == UPLOAD_HISTORY_TABLE:
        raise HTTPException(400, "The reserved uploader audit table cannot be selected as an ingestion destination")
    if mode == "append" and not _is_uploader_managed_table(table_bucket_arn, namespace, table):
        raise HTTPException(409, "This table is browse-only because it has no uploader schema and recovery contract")
    invalid = [upload.filename or "<unnamed>" for upload in files if not upload.filename or not upload.filename.lower().endswith(SUPPORTED_UPLOAD_SUFFIXES)]
    if invalid:
        raise HTTPException(400, "Supported files are Parquet, Parquet GZIP, XLSX, XLS, CSV, and TSV")
    try:
        session = upload_sessions.create(
            owner_user_id=user.user_id, mode=mode, table_bucket_arn=table_bucket_arn,
            namespace=namespace, table=table, files=[(upload.filename or "upload", upload.file) for upload in files],
        )
    except Exception as error:
        error_id = safe_error(logger, "upload_session_receipt_failed", phase="RECEIVED", file_count=len(files))
        raise HTTPException(500, detail={"message": "Unable to store the selected files in a private local session.", "error_id": error_id}) from error
    background_tasks.add_task(_profile_upload_session, session.session_id, user.user_id)
    logger.info("upload_session_created", extra={"session_id": session.session_id, "file_count": len(session.files), "total_bytes": sum(file.size_bytes for file in session.files), "phase": "RECEIVED"})
    return session.safe_dict()


@app.get("/api/v2/upload-sessions/{session_id}")
def get_upload_session(session_id: str, user: PilotUser = Depends(_current_user)):
    try:
        return upload_sessions.get(session_id, user.user_id).safe_dict()
    except KeyError as error:
        raise HTTPException(404, "The upload session does not exist, belongs to another user, or has expired") from error


@app.delete("/api/v2/upload-sessions/{session_id}", status_code=204)
def delete_upload_session(session_id: str, user: PilotUser = Depends(_current_user)):
    try:
        upload_sessions.delete(session_id, user.user_id)
    except KeyError as error:
        raise HTTPException(404, "The upload session does not exist, belongs to another user, or has expired") from error


@app.post("/api/v2/upload-sessions/{session_id}/key-impact", status_code=202)
def analyse_upload_session_key_impact(
    session_id: str, payload: SessionKeyImpactRequest, background_tasks: BackgroundTasks,
    user: PilotUser = Depends(_current_user),
):
    try:
        session = upload_sessions.get(session_id, user.user_id)
    except KeyError as error:
        raise HTTPException(404, "The upload session does not exist, belongs to another user, or has expired") from error
    if session.phase not in {"READY_FOR_REVIEW", "READY_FOR_ACKNOWLEDGEMENT"}:
        raise HTTPException(409, f"Key-impact analysis is unavailable while session phase is {session.phase}")
    _validate_key_analysis_columns(payload.deduplication_columns)
    background_tasks.add_task(_analyse_session_key_impact, session_id, user.user_id, payload)
    return {"session_id": session_id, "phase": "KEY_ANALYSING", "message": "Composite-key impact analysis has started."}


async def _start_session_ingestion(session_id: str, user: PilotUser, payload: SessionIngestionRequest, lease: TableLease) -> None:
    """Bridge session artifacts into the existing safe staging/Glue launcher.

    The next preparation batch replaces this compatibility bridge with a fully
    vectorised path. It already guarantees that the browser uploads files only
    once and gives the UI a single session to reconnect to after refresh.
    """
    slot_acquired = False
    try:
        if not local_processing_slots.acquire(blocking=False):
            upload_sessions.update(
                session_id, user.user_id, phase="QUEUED",
                progress_message="Queued for local processing capacity before upload preparation.",
            )
            await asyncio.to_thread(local_processing_slots.acquire)
        slot_acquired = True
        session = upload_sessions.update(
            session_id, user.user_id, phase="STARTING_GLUE",
            progress_message="Preparing sanitized Parquet and starting AWS Glue.",
        )
        request = IngestionRequest(
            mode=session.mode, table=session.table, table_bucket_arn=session.table_bucket_arn,
            namespace=session.namespace, request_id=payload.request_id, reporting_month=payload.reporting_month,
            type_overrides=payload.type_overrides, deduplication_mode=payload.deduplication_mode,
            deduplication_columns=payload.deduplication_columns, key_analysis_token=payload.key_analysis_token,
            manual_encryption_columns=payload.manual_encryption_columns,
        )
        def report_progress(message: str) -> None:
            upload_sessions.update(
                session_id, user.user_id, phase="STARTING_GLUE", progress_message=message,
            )

        ingestion_progress_hooks[payload.request_id] = report_progress
        try:
            def prepare_and_start() -> dict:
                with _session_upload_files(session) as files:
                    return asyncio.run(_start_ingestion(request=request.model_dump_json(), files=files, user=user))
            result = await asyncio.to_thread(prepare_and_start)
        finally:
            ingestion_progress_hooks.pop(payload.request_id, None)
        lease = table_locks.renew(lease, "GLUE_RUNNING", result["job_run_id"])
        pending_table_leases.pop(payload.request_id, None)
        active_table_leases[result["job_run_id"]] = lease
        active_mutations_by_request[payload.request_id] = result
        upload_sessions.update(
            session_id, user.user_id, phase="GLUE_RUNNING",
            progress_message="AWS Glue is queued or running the Iceberg mutation.", ingestion=result,
        )
        logger.info("upload_session_glue_started", extra={"session_id": session_id, "phase": "GLUE_RUNNING", "job_run_id": result["job_run_id"]})
    except HTTPException as error:
        safe_detail = error.detail if isinstance(error.detail, str) else "The upload did not pass validation."
        upload_sessions.update(
            session_id, user.user_id, phase="FAILED", progress_message="Upload was not started.",
            error={"code": "INGESTION_REJECTED", "message": safe_detail},
        )
        pending_table_leases.pop(payload.request_id, None)
        try:
            table_locks.release(lease)
        except TableLockError:
            safe_error(logger, "table_lock_release_failed", session_id=session_id, phase="FAILED")
    except Exception as error:
        pending_table_leases.pop(payload.request_id, None)
        try:
            table_locks.release(lease)
        except TableLockError:
            safe_error(logger, "table_lock_release_failed", session_id=session_id, phase="FAILED")
        _session_error(session_id, user.user_id, "STARTING_GLUE", "Upload preparation or Glue startup failed.", error)
    finally:
        if slot_acquired:
            local_processing_slots.release()


@app.post("/api/v2/upload-sessions/{session_id}/ingestions", status_code=202)
def start_upload_session_ingestion(
    session_id: str, payload: SessionIngestionRequest, background_tasks: BackgroundTasks,
    user: PilotUser = Depends(_current_user),
):
    try:
        session = upload_sessions.get(session_id, user.user_id)
    except KeyError as error:
        raise HTTPException(404, "The upload session does not exist, belongs to another user, or has expired") from error
    if session.phase not in {"READY_FOR_REVIEW", "READY_FOR_ACKNOWLEDGEMENT"}:
        raise HTTPException(409, f"Upload is unavailable while session phase is {session.phase}")
    if not session.preflight or not session.preflight.get("accepted"):
        raise HTTPException(422, "The uploaded files did not pass the completed preflight validation")
    configured_key = (session.preflight or {}).get("deduplication_columns") or []
    if payload.deduplication_mode == "keyed" and (session.mode == "create" or not configured_key):
        impact = session.key_impact or {}
        if impact.get("acknowledgement_token") != payload.key_analysis_token:
            raise HTTPException(422, "Run and acknowledge the latest composite-key impact analysis before uploading")
        if impact.get("deduplication_columns") != payload.deduplication_columns:
            raise HTTPException(422, "The selected composite key changed after analysis; run it again")
    if payload.request_id in active_mutations_by_request:
        return {"session_id": session_id, "phase": "GLUE_RUNNING", "message": "This idempotent request is already queued or running in AWS Glue.", "ingestion": active_mutations_by_request[payload.request_id]}
    if payload.request_id in pending_table_leases:
        return {"session_id": session_id, "phase": "STARTING_GLUE", "message": "This idempotent request is preparing the upload."}
    lease = _acquire_table_mutation_lock(
        table_bucket_arn=session.table_bucket_arn, namespace=session.namespace, table=session.table,
        user=user, request_id=payload.request_id, operation=session.mode, session_id=session_id,
    )
    pending_table_leases[payload.request_id] = lease
    logger.info("table_lock_acquired", extra={"session_id": session_id, "operation": session.mode, "phase": "STARTING_GLUE"})
    background_tasks.add_task(_start_session_ingestion, session_id, user, payload, lease)
    return {"session_id": session_id, "phase": "STARTING_GLUE", "message": "Upload preparation has started."}


async def preflight(
    mode: Literal["create", "append"] = Form(),
    table_bucket_arn: str = Form(),
    namespace: str = Form(),
    table: str = Form(),
    files: list[UploadFile] = File(),
    user: PilotUser = Depends(_current_user),
):
    _require_scope(user, table_bucket_arn, namespace)
    if mode == "append" and not _is_uploader_managed_table(table_bucket_arn, namespace, _canonical_table_name(table)):
        raise HTTPException(409, "This table is browse-only because it has no uploader schema and recovery contract")
    return _preflight(mode, table_bucket_arn, namespace, _canonical_table_name(table), files)


async def key_impact_analysis(
    request: str = Form(),
    files: list[UploadFile] = File(),
    user: PilotUser = Depends(_current_user),
):
    """Evaluate the proposed immutable key without S3 writes or Glue work."""
    try:
        payload = KeyAnalysisRequest.model_validate_json(request)
    except Exception as error:
        raise HTTPException(400, f"Invalid key-impact analysis request: {error}") from error
    _require_scope(user, payload.table_bucket_arn, payload.namespace)
    if not files:
        raise HTTPException(400, "Choose at least one supported file")
    key_columns = _validate_key_analysis_columns(payload.deduplication_columns)
    try:
        metrics, file_digests = _analyse_selected_key(files, key_columns)
    except HTTPException:
        raise
    except (pl.exceptions.PolarsError, UnicodeError, ValueError) as error:
        # Keep raw cell values out of the browser response.  The server log
        # retains the implementation detail for diagnosis.
        raise HTTPException(
            422,
            "The selected file could not be analysed locally. Check its delimiter, encoding, and header row, then try again.",
        ) from error
    expires_at = int(datetime.now(timezone.utc).timestamp()) + 30 * 60
    acknowledgement = {
        "v": 1, "expires_at": expires_at, "user_id": user.user_id,
        "table_bucket_arn": payload.table_bucket_arn, "namespace": payload.namespace, "table": payload.table,
        "type_overrides": payload.type_overrides, "deduplication_columns": key_columns, "file_digests": file_digests,
    }
    return {
        "metrics": metrics,
        "deduplication_columns": key_columns,
        "acknowledgement_token": _sign_key_analysis(acknowledgement),
        "expires_at": datetime.fromtimestamp(expires_at, timezone.utc).isoformat(),
        "no_storage_or_glue_side_effects": True,
        "analysis_basis": "raw-local-pre-sanitization",
    }


def _validate_key_analysis_acknowledgement(
    payload: IngestionRequest, key_columns: list[str],
    files: list[UploadFile], user: PilotUser,
) -> None:
    if not payload.key_analysis_token:
        raise HTTPException(422, "Run and acknowledge the composite-key impact analysis before uploading")
    acknowledgement = _read_key_analysis_token(payload.key_analysis_token)
    expected = {
        "user_id": user.user_id,
        "table_bucket_arn": payload.table_bucket_arn,
        "namespace": payload.namespace,
        "table": payload.table,
        "type_overrides": payload.type_overrides,
        "deduplication_columns": key_columns,
    }
    for key, value in expected.items():
        if acknowledgement.get(key) != value:
            raise HTTPException(422, "The key-impact analysis is stale; run it again")
    digests = []
    for upload in files:
        path, digest = _copy_upload_with_digest(upload)
        path.unlink(missing_ok=True)
        digests.append(digest)
    if acknowledgement.get("file_digests") != digests:
        raise HTTPException(422, "The selected files changed after key analysis; run it again")


async def _start_ingestion(
    request: str = Form(),
    files: list[UploadFile] = File(),
    user: PilotUser = Depends(_current_user),
):
    try:
        payload = IngestionRequest.model_validate_json(request)
    except Exception as error:
        raise HTTPException(400, f"Invalid ingestion request: {error}") from error
    _report_ingestion_progress(payload.request_id, "Checking the authorized table destination and upload request.")
    _require_scope(user, payload.table_bucket_arn, payload.namespace)
    if payload.table == UPLOAD_HISTORY_TABLE:
        raise HTTPException(400, "The reserved uploader audit table cannot be selected as an ingestion destination")
    if payload.mode == "append" and not _is_uploader_managed_table(payload.table_bucket_arn, payload.namespace, payload.table):
        raise HTTPException(409, "This table is browse-only because it has no uploader schema and recovery contract")
    if not files:
        raise HTTPException(400, "Choose at least one supported file")
    _report_ingestion_progress(payload.request_id, "Revalidating schema, data types, and sanitization requirements.")
    preview = _preflight(payload.mode, payload.table_bucket_arn, payload.namespace, payload.table, files)
    if not preview["accepted"]:
        raise HTTPException(
            422,
            detail={
                "message": "Upload rejected by the hard schema and sanitization validation rules.",
                "rejection_reasons": preview["rejection_reasons"],
                "preflight": preview,
            },
        )
    _report_ingestion_progress(payload.request_id, "Finalizing the table schema and de-duplication settings.")
    target_schema = _apply_create_type_overrides(preview, payload.type_overrides) if payload.mode == "create" else preview["target_schema"]
    existing_contract = {"deduplication_columns": []} if payload.mode == "create" else _load_contract_record(payload.table_bucket_arn, payload.namespace, payload.table)
    if payload.mode == "create":
        _report_ingestion_progress(payload.request_id, "Validating the selected first-upload type conversions.")
        allowed_manual = {item["column"] for item in preview.get("sanitization_review", {}).get("manual_encryption_candidates", [])}
        invalid_manual = sorted(set(payload.manual_encryption_columns) - allowed_manual)
        if invalid_manual:
            raise HTTPException(422, f"Manual encryption is not available for: {', '.join(invalid_manual)}")
        manual_encryption_columns = sorted(set(payload.manual_encryption_columns))
    else:
        manual_encryption_columns = list(existing_contract.get("manual_encryption_columns") or [])
    # The retired multipart UI did not send a mode. Preserve its historical
    # keyed behavior while v2 callers always select an explicit mode.
    effective_deduplication_mode = payload.deduplication_mode
    if effective_deduplication_mode == "legacy-full-row":
        effective_deduplication_mode = "keyed" if payload.mode == "create" or existing_contract.get("deduplication_columns") else "legacy-full-row"
    deduplication_columns = _v2_deduplication_columns(
        preview, existing_contract, effective_deduplication_mode, payload.deduplication_columns,
    )
    late_key_activation = (
        payload.mode == "append"
        and effective_deduplication_mode == "keyed"
        and not existing_contract.get("deduplication_columns")
    )
    # Only an explicit first-upload temporal choice may have lossy NULL
    # conversion. Appends always keep this empty and remain strictly checked.
    lossy_temporal_columns: set[str] = set()
    if payload.mode == "create":
        override_issues = []
        for upload in files:
            override_issues.extend(_unsafe_cast_issues(upload, target_schema))
        # A user may deliberately choose DATE or TIMESTAMP for an ambiguous
        # first-upload field.  That explicit choice has a documented lossy
        # behaviour: valid temporal values are retained and incompatible
        # populated values become NULL.  Every other unsafe cast remains a
        # hard rejection.
        lossy_temporal_columns = {
            name for name, target_type in payload.type_overrides.items()
            if target_type in {"DATE", "TIMESTAMP"}
        }
        blocking_override_issues = [
            issue for issue in override_issues
            if not (
                issue["column"] in lossy_temporal_columns
                and issue["target_type"] in {"DATE", "TIMESTAMP"}
            )
        ]
        if blocking_override_issues:
            raise HTTPException(
                422,
                detail={
                    "message": "The chosen first-upload types would discard values; choose compatible types.",
                    "unsafe_casts": blocking_override_issues,
                },
            )
        if effective_deduplication_mode == "keyed":
            _validate_key_analysis_acknowledgement(payload, deduplication_columns, files, user)
    if payload.mode == "append":
        _report_ingestion_progress(payload.request_id, "Configuring S3 Tables recovery snapshot retention.")
        try:
            _configure_snapshot_retention(payload.table_bucket_arn, payload.namespace, payload.table)
        except Exception as error:
            raise HTTPException(500, "Unable to configure the required S3 Tables snapshot retention") from error
    if late_key_activation:
        _report_ingestion_progress(payload.request_id, "Saving the first immutable composite-key contract for this table.")
        _activate_late_deduplication_contract(
            payload.table_bucket_arn, payload.namespace, payload.table,
            existing_contract, deduplication_columns, user.user_id,
        )
    sensitive_columns_present = bool(manual_encryption_columns) or any(
        item["sanitization"]["encrypted_columns"] or item.get("nric_detected_columns") for item in preview["files"]
    )
    try:
        _report_ingestion_progress(payload.request_id, "Preparing healthcare sanitization and encryption requirements.")
        active_key = encryption_key() if sensitive_columns_present else None
    except Exception as error:
        raise HTTPException(500, "Unable to retrieve the configured encryption key") from error
    try:
        _report_ingestion_progress(payload.request_id, "Ensuring 30-day retention for sanitized troubleshooting uploads.")
        _ensure_upload_archive_lifecycle()
    except Exception as error:
        raise HTTPException(500, "Unable to configure the 30-day sanitized-upload archive lifecycle") from error
    request_prefix = f"{WEB_UPLOAD_PREFIX}/{payload.request_id}"
    objects, sanitization_audits = [], []
    for number, upload in enumerate(files):
        _report_ingestion_progress(
            payload.request_id,
            f"Sanitizing and converting file {number + 1} of {len(files)} to Glue-compatible Parquet.",
        )
        path, digest = _copy_upload_with_digest(upload)
        try:
            original_filename = upload.filename or "upload.parquet"
            key = f"{request_prefix}/input/{number:02d}-{Path(original_filename).stem}.parquet"
            staged, transformed, audit = _make_glue_compatible_parquet(
                path, original_filename, active_key, target_schema,
                manual_encryption_columns=manual_encryption_columns,
                nric_columns=preview["files"][number].get("nric_detected_columns", []),
                lossy_temporal_columns=lossy_temporal_columns,
            )
            try:
                _report_ingestion_progress(
                    payload.request_id,
                    f"Staging sanitized file {number + 1} of {len(files)} in S3.",
                )
                with staged.open("rb") as stream:
                    s3.put_object(
                        Bucket=SOURCE_BUCKET,
                        Key=key,
                        Body=stream,
                        ContentType="application/octet-stream",
                        Metadata={
                            "sha256": digest,
                            "original_filename": original_filename,
                            "spark_compatible_staging": str(transformed).lower(),
                            "sanitized": str(bool(
                                audit["dropped_columns"]
                                or audit["encrypted_columns"]
                                or audit["postal_columns"]
                                or audit["age_banded_columns"]
                            )).lower(),
                        },
                        ServerSideEncryption="AES256",
                    )
            finally:
                if staged != path:
                    staged.unlink(missing_ok=True)
            objects.append(f"s3://{SOURCE_BUCKET}/{key}")
            sanitization_audits.append({"filename": original_filename, "sanitized_archive_uri": f"s3://{SOURCE_BUCKET}/{key}", **audit})
        finally:
            path.unlink(missing_ok=True)
    if payload.mode == "create":
        _report_ingestion_progress(payload.request_id, "Writing the immutable uploader schema and de-duplication contract.")
        s3.put_object(
            Bucket=SOURCE_BUCKET,
            Key=_contract_key(payload.table_bucket_arn, payload.namespace, payload.table),
            Body=json.dumps({
                "contract_version": 2,
                "schema": target_schema,
                "deduplication_columns": deduplication_columns if effective_deduplication_mode == "keyed" else [],
                "deduplication_mode": "keyed" if effective_deduplication_mode == "keyed" else "unconfigured",
                "deduplication_policy": "skip-existing-key-report-conflict-v2",
                "manual_encryption_columns": manual_encryption_columns,
                "automatic_sanitization_columns": preview.get("sanitization_review", {}).get("automatic_encrypted_columns", []),
                "created_by": user.user_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }, indent=2).encode(),
            ContentType="application/json", ServerSideEncryption="AES256",
        )
    _report_ingestion_progress(payload.request_id, "Writing the staged-upload manifest for AWS Glue.")
    manifest_key = f"{request_prefix}/manifest.json"
    s3.put_object(
        Bucket=SOURCE_BUCKET, Key=manifest_key,
        Body=json.dumps({
            "files": objects, "schema": target_schema, "sanitization": sanitization_audits,
            "deduplication_columns": deduplication_columns,
            "deduplication_mode": effective_deduplication_mode,
            "deduplication_policy": preview["deduplication_policy"],
        }).encode(),
        ContentType="application/json", ServerSideEncryption="AES256",
    )
    _report_ingestion_progress(payload.request_id, "Ensuring the AWS Glue ingestion job is ready.")
    _ensure_web_job()
    run_id = str(uuid.uuid4())
    upload_id = _upload_id()
    uploaded_at = datetime.now(timezone.utc).isoformat()
    history_prefix = _history_prefix(payload.table_bucket_arn, payload.namespace, payload.table)
    _report_ingestion_progress(payload.request_id, "Starting the AWS Glue Iceberg table mutation.")
    response = glue.start_job_run(
        JobName=WEB_JOB_NAME,
        JobRunQueuingEnabled=True,
        Arguments={
            "--MODE": payload.mode, "--MANIFEST_URI": f"s3://{SOURCE_BUCKET}/{manifest_key}",
            "--TABLE_BUCKET_ARN": payload.table_bucket_arn, "--NAMESPACE": payload.namespace,
            "--TABLE": payload.table, "--QC_PREFIX": QC_PREFIX, "--RUN_ID": run_id,
            "--UPLOAD_ID": upload_id, "--UPLOADED_BY": user.user_id,
            "--ORIGINAL_UPLOADED_BY": user.user_id, "--ORIGINAL_UPLOADED_AT": uploaded_at,
            "--REPORTING_MONTH": payload.reporting_month,
            "--FILENAMES_JSON": json.dumps([upload.filename or "upload" for upload in files]),
            # Glue rejects blank argument values. This is deliberately ignored
            # by create/append modes and only validated by rollback mode.
            "--AUDIT_PREFIX": f"s3://{SOURCE_BUCKET}/{history_prefix}", "--ROLLBACK_SNAPSHOT_ID": "not-applicable",
        },
    )
    return {
        "job_run_id": response["JobRunId"], "qc_uri": f"{QC_PREFIX}/web/{run_id}/report.json",
        "request_id": payload.request_id, "upload_id": upload_id, "operation": "ingestion",
    }


def _retired_multipart_endpoint(replacement: str) -> JSONResponse:
    """One-release no-body tombstone for the retired multipart API."""
    return JSONResponse(
        status_code=410,
        content={
            "code": "MULTIPART_API_RETIRED",
            "detail": "This uploader endpoint has been retired; create an upload session first.",
            "replacement": replacement,
        },
    )


@app.post("/api/preflight")
async def retired_preflight() -> JSONResponse:
    return _retired_multipart_endpoint("POST /api/v2/upload-sessions")


@app.post("/api/key-impact-analysis")
async def retired_key_impact_analysis() -> JSONResponse:
    return _retired_multipart_endpoint("POST /api/v2/upload-sessions/{session_id}/key-impact")


@app.post("/api/ingestions")
async def retired_ingestions() -> JSONResponse:
    return _retired_multipart_endpoint("POST /api/v2/upload-sessions/{session_id}/ingestions")


@app.delete("/api/tables")
def delete_table(payload: DeleteTableRequest, user: PilotUser = Depends(_current_user)):
    _require_scope(user, payload.table_bucket_arn, payload.namespace)
    if not user.is_admin:
        raise HTTPException(403, "Only administrators may delete S3 Tables")
    if payload.table == UPLOAD_HISTORY_TABLE:
        raise HTTPException(400, "The reserved uploader audit table cannot be deleted through this UI")
    if not _is_uploader_managed_table(payload.table_bucket_arn, payload.namespace, payload.table):
        raise HTTPException(409, "This table is browse-only because it was not created by this uploader")
    s3tables.delete_table(
        tableBucketARN=payload.table_bucket_arn,
        namespace=payload.namespace,
        name=payload.table,
    )
    return {"deleted": payload.table, "table_bucket_arn": payload.table_bucket_arn, "namespace": payload.namespace}


@app.get("/api/upload-history")
def upload_history(table_bucket_arn: str, namespace: str, table: str, user: PilotUser = Depends(_current_user)):
    _require_scope(user, table_bucket_arn, namespace)
    if not user.can_view_upload_history:
        raise HTTPException(403, "This user is not permitted to view uploader history")
    if table == UPLOAD_HISTORY_TABLE:
        raise HTTPException(400, "The reserved uploader audit table is not a master-data destination")
    if not _is_uploader_managed_table(table_bucket_arn, namespace, table):
        raise HTTPException(409, "This table is browse-only because it has no uploader history contract")
    history = _history_entries(table_bucket_arn, namespace, table)
    successful = [item for item in history if item.get("status") == "SUCCESS" and item.get("previous_snapshot_id")]
    latest = max(successful, key=lambda item: item.get("uploaded_at") or "", default=None)
    visible_history = history if user.is_admin else [item for item in history if item.get("uploaded_by") == user.user_id]
    return {
        "table_bucket_arn": table_bucket_arn,
        "namespace": namespace,
        "table": table,
        "history": visible_history,
        # This carries no audit content for another user; it lets the client
        # correctly disable an editor's stale rollback button.
        "latest_rollback_upload_id": latest.get("upload_id") if latest else None,
    }


@app.post("/api/rollbacks")
def start_rollback(payload: RollbackRequest, user: PilotUser = Depends(_current_user)):
    _require_scope(user, payload.table_bucket_arn, payload.namespace)
    if not user.can_rollback_uploads:
        raise HTTPException(403, "This user is not permitted to roll back uploader-managed updates")
    if not payload.confirm:
        raise HTTPException(400, "Explicit rollback confirmation is required")
    if payload.table == UPLOAD_HISTORY_TABLE:
        raise HTTPException(400, "The reserved uploader audit table cannot be rolled back through this UI")
    if not _is_uploader_managed_table(payload.table_bucket_arn, payload.namespace, payload.table):
        raise HTTPException(409, "This table is browse-only because it has no uploader history contract")
    history = _history_entries(payload.table_bucket_arn, payload.namespace, payload.table)
    selected = next((item for item in history if item.get("upload_id") == payload.upload_id), None)
    successful = [item for item in history if item.get("status") == "SUCCESS"]
    latest = max(successful, key=lambda item: item.get("uploaded_at") or "", default=None)
    if not selected or selected.get("status") != "SUCCESS":
        raise HTTPException(409, "Only a successful upload that has not already been rolled back can be restored")
    if not user.is_admin and selected.get("uploaded_by") != user.user_id:
        raise HTTPException(403, "A non-admin user may roll back only their own upload")
    if selected != latest:
        raise HTTPException(409, "Only the latest successful uploader-managed update may be rolled back")
    snapshot_id = selected.get("previous_snapshot_id")
    if not snapshot_id:
        raise HTTPException(409, "The initial table load has no earlier snapshot to restore")
    _ensure_web_job()
    run_id = str(uuid.uuid4())
    response = glue.start_job_run(
        JobName=WEB_JOB_NAME,
        JobRunQueuingEnabled=True,
        Arguments={
            # The rollback job does not read a manifest, but Glue requires each
            # supplied command argument to have a non-empty value.
            "--MODE": "rollback", "--MANIFEST_URI": f"s3://{SOURCE_BUCKET}/{WEB_UPLOAD_PREFIX}/not-used-for-rollback.json", "--TABLE_BUCKET_ARN": payload.table_bucket_arn,
            "--NAMESPACE": payload.namespace, "--TABLE": payload.table, "--QC_PREFIX": QC_PREFIX,
            "--RUN_ID": run_id, "--UPLOAD_ID": payload.upload_id, "--UPLOADED_BY": user.user_id,
            "--ORIGINAL_UPLOADED_BY": selected.get("uploaded_by") or user.user_id,
            "--ORIGINAL_UPLOADED_AT": selected.get("uploaded_at") or datetime.now(timezone.utc).isoformat(),
            "--REPORTING_MONTH": selected.get("reporting_month") or "", "--FILENAMES_JSON": selected.get("filenames") or "[]",
            "--AUDIT_PREFIX": f"s3://{SOURCE_BUCKET}/{_history_prefix(payload.table_bucket_arn, payload.namespace, payload.table)}",
            "--ROLLBACK_SNAPSHOT_ID": str(snapshot_id),
        },
    )
    return {
        "job_run_id": response["JobRunId"], "qc_uri": f"{QC_PREFIX}/web/{run_id}/report.json",
        "upload_id": payload.upload_id, "operation": "rollback",
    }


@app.get("/api/ingestions/{job_run_id}")
def ingestion_status(job_run_id: str, operation: Literal["ingestion", "rollback"] = "ingestion"):
    run = glue.get_job_run(JobName=WEB_JOB_NAME, RunId=job_run_id, PredecessorsIncluded=False)["JobRun"]
    state = run["JobRunState"]
    message = {
        "STARTING": "Rollback is starting in AWS Glue…" if operation == "rollback" else "ETL is starting in AWS Glue…",
        "WAITING": "Queued for ETL capacity in AWS Glue.",
        "RUNNING": "Rollback is in process: restoring and verifying the prior Iceberg snapshot…" if operation == "rollback" else "ETL is in process: validating, snapshotting, and appending the uploaded data…",
        "SUCCEEDED": "Rollback completed and was verified." if operation == "rollback" else "ETL completed successfully.",
        "FAILED": "ETL failed; no successful outcome was reported.",
        "TIMEOUT": "ETL timed out.",
        "STOPPED": "ETL was stopped.",
    }.get(state, f"ETL status: {state}")
    retention_configured, retention_warning = None, None
    # A newly-created table does not exist until its first Glue run succeeds.
    # Configure its recovery policy immediately afterwards; append jobs are
    # configured before they are allowed to start.
    arguments = run.get("Arguments", {})
    if state == "SUCCEEDED" and operation == "ingestion" and arguments.get("--MODE") == "create":
        try:
            _configure_snapshot_retention(
                arguments["--TABLE_BUCKET_ARN"], arguments["--NAMESPACE"], arguments["--TABLE"]
            )
            retention_configured = True
        except Exception as error:
            retention_configured, retention_warning = False, str(error)
    if state in {"SUCCEEDED", "FAILED", "TIMEOUT", "STOPPED", "ERROR"}:
        _release_table_mutation_lock(job_run_id)
    return {
        "state": state, "message": message, "error": run.get("ErrorMessage"),
        "started": str(run.get("StartedOn")), "completed": str(run.get("CompletedOn")),
        "retention_configured": retention_configured, "retention_warning": retention_warning,
    }


@app.get("/api/qc")
def qc(uri: str):
    allowed_prefix = f"{QC_PREFIX}/web/"
    if not uri.startswith(allowed_prefix):
        raise HTTPException(400, "QC URI is outside the local pilot web-ingestion prefix")
    bucket, key = uri.removeprefix("s3://").split("/", 1)
    try:
        return json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
    except s3.exceptions.NoSuchKey as error:
        raise HTTPException(404, "No QC report was created because the Glue job failed before it began") from error

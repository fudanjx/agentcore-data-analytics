"""Bucket / namespace / table endpoints — REST-nested under /api/v3/buckets.

Namespaces and tables live *inside* buckets in the S3 Tables data model, so
their URLs mirror that hierarchy:

- ``/api/v3/buckets``                    list / create bucket
- ``/api/v3/buckets/cache/purge``        admin: drop cached bucket tags
- ``/api/v3/buckets/namespaces``         list / create namespace
- ``/api/v3/buckets/tables``             list / delete table

Bucket ARN travels as a query parameter (paths cannot cleanly encode ARNs
that include colons and slashes).
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ...app.dependencies import (
    ContractServiceDep,
    S3Dep,
    SettingsDep,
    TableBucketServiceDep,
    UserDep,
)
from ...core.constants import UPLOAD_HISTORY_TABLE
from ...core.exceptions import TableBucketForbidden
from ...table_lock import S3TableLockManager
from ...utils.api_prefix import get_api_prefix
from ._common import iceberg_row_count, require_admin, require_table_bucket_access


router = APIRouter(prefix=get_api_prefix(Path(__file__), "api"))


class CreateTableBucketRequest(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9-]{3,63}$")


class CreateNamespaceRequest(BaseModel):
    table_bucket_arn: str = Field(min_length=1)
    namespace: str = Field(pattern=r"^[a-z][a-z0-9_]{0,254}$")


class DeleteTableRequest(BaseModel):
    table_bucket_arn: str = Field(min_length=1)
    namespace: str = Field(pattern=r"^[a-z][a-z0-9_]{0,254}$")
    table: str = Field(pattern=r"^[a-z][a-z0-9_]{0,254}$")


# ---------------------------------------------------------------------------
# Buckets
# ---------------------------------------------------------------------------

@router.get("")
def list_buckets(user: UserDep, tables: TableBucketServiceDep) -> dict[str, object]:
    if user.visible_buckets is not None and not user.is_admin and not user.visible_buckets:
        raise TableBucketForbidden("This user has no S3 Tables bucket assignment")
    visible = (
        tables.list_buckets()
        if user.is_admin or user.visible_buckets is None
        else [asdict(grant) for grant in user.visible_buckets]
    )
    return {
        "user_id": user.user_id,
        "is_admin": user.is_admin,
        "can_view_upload_history": user.can_view_upload_history,
        "can_rollback_uploads": user.can_rollback_uploads,
        "buckets": visible,
    }


@router.post("", status_code=201)
def create_bucket(
    payload: CreateTableBucketRequest,
    user: UserDep,
    tables: TableBucketServiceDep,
) -> dict[str, str]:
    require_admin(user)
    return tables.create_bucket(payload.name)


@router.post("/cache/purge")
def purge_bucket_tag_cache(
    user: UserDep, tables: TableBucketServiceDep
) -> dict[str, int]:
    require_admin(user)
    return {"dropped": tables.purge_cache()}


# ---------------------------------------------------------------------------
# Namespaces (nested under buckets)
# ---------------------------------------------------------------------------

@router.get("/namespaces")
def list_namespaces(
    table_bucket_arn: Annotated[str, Query()],
    user: UserDep,
    tables: TableBucketServiceDep,
) -> dict[str, object]:
    require_table_bucket_access(table_bucket_arn, user, tables)
    return {
        "table_bucket_arn": table_bucket_arn,
        "namespaces": tables.list_namespaces(table_bucket_arn),
    }


@router.post("/namespaces", status_code=201)
def create_namespace(
    payload: CreateNamespaceRequest,
    user: UserDep,
    tables: TableBucketServiceDep,
) -> dict[str, str]:
    require_admin(user)
    require_table_bucket_access(payload.table_bucket_arn, user, tables)
    return tables.create_namespace(payload.table_bucket_arn, payload.namespace)


# ---------------------------------------------------------------------------
# Tables (nested under buckets)
# ---------------------------------------------------------------------------

@router.get("/tables")
def list_tables(
    table_bucket_arn: Annotated[str, Query()],
    namespace: Annotated[str, Query()],
    user: UserDep,
    tables: TableBucketServiceDep,
    contracts: ContractServiceDep,
    s3: S3Dep,
) -> dict[str, object]:
    require_table_bucket_access(table_bucket_arn, user, tables)
    rows: list[dict[str, object]] = []
    for entry in tables.list_tables(table_bucket_arn, namespace):
        name = entry["name"]
        if name == UPLOAD_HISTORY_TABLE:
            continue
        details = tables.get_table(table_bucket_arn, namespace, name)
        uploader_managed = contracts.is_uploader_managed(table_bucket_arn, namespace, name)
        contract = (
            contracts.load(table_bucket_arn, namespace, name) if uploader_managed else {}
        )
        raw = entry.get("raw", {})
        rows.append(
            {
                "name": name,
                "created_at": str(raw.get("createdAt")),
                "modified_at": str(raw.get("modifiedAt")),
                "row_count": iceberg_row_count(s3, details.get("metadataLocation")),
                "uploader_managed": uploader_managed,
                "deduplication_columns": contract.get("deduplication_columns", []),
            }
        )
    return {
        "table_bucket": table_bucket_arn,
        "namespace": namespace,
        "is_admin": user.is_admin,
        "tables": sorted(rows, key=lambda item: item["name"]),  # type: ignore[arg-type]
    }


@router.delete("/tables")
def delete_table(
    payload: DeleteTableRequest,
    user: UserDep,
    tables: TableBucketServiceDep,
    contracts: ContractServiceDep,
    s3: S3Dep,
    settings: SettingsDep,
) -> dict[str, str]:
    require_admin(user)
    require_table_bucket_access(payload.table_bucket_arn, user, tables)
    if payload.table == UPLOAD_HISTORY_TABLE:
        raise HTTPException(400, "The reserved uploader audit table cannot be deleted through this UI")
    if not contracts.is_uploader_managed(
        payload.table_bucket_arn, payload.namespace, payload.table
    ):
        raise HTTPException(409, "This table is browse-only because it was not created by this uploader")
    lock = S3TableLockManager(
        s3, settings.landing_bucket, f"{settings.landing_prefix}/table-locks"
    ).get_lease(
        table_bucket_arn=payload.table_bucket_arn,
        namespace=payload.namespace,
        table=payload.table,
    )
    if lock is not None:
        raise HTTPException(409, "TABLE_MUTATION_IN_PROGRESS")
    tables.delete_table(payload.table_bucket_arn, payload.namespace, payload.table)
    return {
        "deleted": payload.table,
        "table_bucket_arn": payload.table_bucket_arn,
        "namespace": payload.namespace,
    }

"""Bucket / namespace / table endpoints — REST-nested under /api/v3/buckets.

Namespaces and tables live *inside* buckets in the S3 Tables data model, so
their URLs mirror that hierarchy:

- ``/api/v3/buckets``                    list / create / delete bucket
- ``/api/v3/buckets/cache/purge``        admin: drop cached bucket tags
- ``/api/v3/buckets/namespaces``         list / create / delete namespace
- ``/api/v3/buckets/tables``             list / delete table

Bucket ARN travels as a query parameter (paths cannot cleanly encode ARNs
that include colons and slashes).
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from ... import skill_bundle
from ...app.dependencies import (
    ContractServiceDep,
    S3Dep,
    SettingsDep,
    SkillDestinationDep,
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


class DeleteTableBucketRequest(BaseModel):
    table_bucket_arn: str = Field(min_length=1)
    force: bool = False
    delete_skill_prefix: bool = False


class DeleteNamespaceRequest(BaseModel):
    table_bucket_arn: str = Field(min_length=1)
    namespace: str = Field(pattern=r"^[a-z][a-z0-9_]{0,254}$")
    confirm: bool = False


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


@router.delete("")
def delete_bucket(
    payload: DeleteTableBucketRequest,
    user: UserDep,
    tables: TableBucketServiceDep,
    contracts: ContractServiceDep,
    s3: S3Dep,
    settings: SettingsDep,
    skill_destination: SkillDestinationDep,
) -> dict[str, object]:
    require_admin(user)
    require_table_bucket_access(payload.table_bucket_arn, user, tables)
    if not payload.force:
        raise HTTPException(
            422,
            "Set force=true to delete a table bucket and all of its contents",
        )

    namespaces = tables.list_namespaces(payload.table_bucket_arn)
    inventory = {
        namespace: [
            str(entry["name"])
            for entry in tables.list_tables(payload.table_bucket_arn, namespace)
        ]
        for namespace in namespaces
    }

    # Check every table before making the first destructive change. This
    # prevents an upload or rollback from losing its destination mid-run.
    lock_manager = S3TableLockManager(
        s3, settings.landing_bucket, f"{settings.landing_prefix}/table-locks"
    )
    for namespace, namespace_tables in inventory.items():
        for table in namespace_tables:
            if lock_manager.get_lease(
                table_bucket_arn=payload.table_bucket_arn,
                namespace=namespace,
                table=table,
            ) is not None:
                raise HTTPException(
                    409,
                    f"TABLE_MUTATION_IN_PROGRESS: {namespace}.{table}",
                )

    deleted_tables = 0
    deleted_namespaces = 0
    deleted_contracts = 0
    for namespace in namespaces:
        for table in inventory[namespace]:
            tables.delete_table(payload.table_bucket_arn, namespace, table)
            deleted_tables += 1
        deleted_contracts += contracts.purge_namespace(
            payload.table_bucket_arn, namespace
        )
        tables.delete_namespace(payload.table_bucket_arn, namespace)
        deleted_namespaces += 1

    deleted_skill_versions = 0
    if payload.delete_skill_prefix:
        deleted_skill_versions = int(
            skill_bundle.delete_skill_prefix(
                s3,
                payload.table_bucket_arn,
                **skill_destination,
            )["deleted_versions"]
        )

    tables.delete_bucket(payload.table_bucket_arn)
    return {
        "deleted": payload.table_bucket_arn,
        "deleted_tables": deleted_tables,
        "deleted_namespaces": deleted_namespaces,
        "deleted_contracts": deleted_contracts,
        "skill_prefix_deleted": payload.delete_skill_prefix,
        "deleted_skill_versions": deleted_skill_versions,
    }


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


@router.delete("/namespaces")
def delete_namespace(
    payload: DeleteNamespaceRequest,
    user: UserDep,
    tables: TableBucketServiceDep,
) -> dict[str, str]:
    require_admin(user)
    require_table_bucket_access(payload.table_bucket_arn, user, tables)
    if not payload.confirm:
        raise HTTPException(422, "Confirm deletion before removing a namespace")
    tables.delete_namespace(payload.table_bucket_arn, payload.namespace)
    return {
        "deleted": payload.namespace,
        "table_bucket_arn": payload.table_bucket_arn,
    }


# ---------------------------------------------------------------------------
# Tables (nested under buckets)
# ---------------------------------------------------------------------------

@router.get("/tables")
async def list_tables(
    table_bucket_arn: Annotated[str, Query()],
    namespace: Annotated[str, Query()],
    user: UserDep,
    tables: TableBucketServiceDep,
    contracts: ContractServiceDep,
    s3: S3Dep,
) -> dict[str, object]:
    await run_in_threadpool(require_table_bucket_access, table_bucket_arn, user, tables)
    entries = await run_in_threadpool(tables.list_tables, table_bucket_arn, namespace)

    def build_row(entry: dict[str, object]) -> dict[str, object]:
        name = str(entry["name"])
        details = tables.get_table(table_bucket_arn, namespace, name)
        uploader_managed = contracts.is_uploader_managed(table_bucket_arn, namespace, name)
        contract = (
            contracts.load(table_bucket_arn, namespace, name) if uploader_managed else {}
        )
        raw = entry.get("raw", {}) or {}
        return {
            "name": name,
            "created_at": str(raw.get("createdAt")),
            "modified_at": str(raw.get("modifiedAt")),
            "row_count": iceberg_row_count(s3, details.get("metadataLocation")),
            "uploader_managed": uploader_managed,
            "deduplication_columns": contract.get("deduplication_columns", []),
        }

    # Fan out per-table AWS calls concurrently through the shared anyio
    # threadpool so N tables cost ~one round-trip's worth of latency instead
    # of N sequential trips.
    rows = await asyncio.gather(
        *(
            run_in_threadpool(build_row, entry)
            for entry in entries
            if entry["name"] != UPLOAD_HISTORY_TABLE
        )
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

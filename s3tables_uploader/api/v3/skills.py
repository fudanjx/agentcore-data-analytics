"""Skill bundle upload / download / delete.

Calls into :mod:`skill_bundle` directly — the module owns validation, S3
IO and its own error type (a subclass of :class:`UploaderError`, so the
global exception handler translates it). The router threads the
Settings-driven destination through as ``**dest`` on every call.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Iterator
from urllib.parse import quote

from botocore.exceptions import ClientError
from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ... import skill_bundle
from ...app.dependencies import (
    S3Dep,
    SkillDestinationDep,
    TableBucketServiceDep,
    UserDep,
)
from ...utils.api_prefix import get_api_prefix
from ._common import require_table_bucket_access


router = APIRouter(prefix=get_api_prefix(Path(__file__), "api"))


class DeleteSkillFileRequest(BaseModel):
    table_bucket_arn: str = Field(min_length=1)
    path: str = Field(min_length=1, max_length=1024)
    confirm: bool = False


def _stream(body: Any) -> Iterator[bytes]:
    try:
        while chunk := body.read(1024 * 1024):
            yield chunk
    finally:
        body.close()


def _not_found_or_gateway(error: ClientError) -> HTTPException:
    code = error.response.get("Error", {}).get("Code", "") if hasattr(error, "response") else ""
    if code in {"404", "NoSuchKey", "NoSuchVersion", "NotFound"}:
        return HTTPException(404, "The requested skill file no longer exists")
    return HTTPException(502, "Unable to reach S3 for the requested skill file")


# ---------------------------------------------------------------------------
# Skill files
# ---------------------------------------------------------------------------

@router.get("/files")
def list_files(
    table_bucket_arn: Annotated[str, Query()],
    user: UserDep,
    tables: TableBucketServiceDep,
    dest: SkillDestinationDep,
    s3: S3Dep,
) -> dict[str, object]:
    require_table_bucket_access(table_bucket_arn, user, tables)
    return skill_bundle.list_skill_files(s3, table_bucket_arn, **dest)


@router.post("/files")
async def upload_files(
    user: UserDep,
    tables: TableBucketServiceDep,
    dest: SkillDestinationDep,
    s3: S3Dep,
    table_bucket_arn: Annotated[str, Form()],
    paths_json: Annotated[str, Form()],
    files: Annotated[list[UploadFile], File()],
) -> dict[str, object]:
    require_table_bucket_access(table_bucket_arn, user, tables)
    paths = skill_bundle.parse_paths_json(paths_json)
    if len(paths) != len(files):
        raise HTTPException(422, "Each uploaded skill file must have one matching relative path")
    try:
        payload = [
            (path, await upload.read(skill_bundle.MAX_FILE_BYTES + 1))
            for path, upload in zip(paths, files, strict=True)
        ]
        return await run_in_threadpool(
            skill_bundle.publish_files,
            s3,
            table_bucket_arn,
            user.user_id,
            payload,
            **dest,
        )
    finally:
        for upload in files:
            await upload.close()


@router.get("/files/download")
def download_file(
    table_bucket_arn: Annotated[str, Query()],
    path: Annotated[str, Query()],
    user: UserDep,
    tables: TableBucketServiceDep,
    dest: SkillDestinationDep,
    s3: S3Dep,
) -> StreamingResponse:
    require_table_bucket_access(table_bucket_arn, user, tables)
    destination_bucket, key, safe_path = skill_bundle.skill_file_location(
        table_bucket_arn, path, **dest
    )
    try:
        result = s3.get_object(Bucket=destination_bucket, Key=key)
    except ClientError as error:
        raise _not_found_or_gateway(error) from error
    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(safe_path.rsplit('/', 1)[-1])}"
    }
    if result.get("ContentLength") is not None:
        headers["Content-Length"] = str(result["ContentLength"])
    return StreamingResponse(
        _stream(result["Body"]),
        media_type=result.get("ContentType") or "application/octet-stream",
        headers=headers,
    )


@router.delete("/files")
def delete_file(
    payload: DeleteSkillFileRequest,
    user: UserDep,
    tables: TableBucketServiceDep,
    dest: SkillDestinationDep,
    s3: S3Dep,
) -> dict[str, str]:
    require_table_bucket_access(payload.table_bucket_arn, user, tables)
    if not payload.confirm:
        raise HTTPException(422, "Confirm deletion before removing a skill file")
    destination_bucket, key, safe_path = skill_bundle.skill_file_location(
        payload.table_bucket_arn, payload.path, **dest
    )
    try:
        s3.head_object(Bucket=destination_bucket, Key=key)
        s3.delete_object(Bucket=destination_bucket, Key=key)
    except ClientError as error:
        raise _not_found_or_gateway(error) from error
    return {"deleted_path": safe_path}


# ---------------------------------------------------------------------------
# Skill versions
# ---------------------------------------------------------------------------

@router.get("/versions")
def list_versions(
    table_bucket_arn: Annotated[str, Query()],
    user: UserDep,
    tables: TableBucketServiceDep,
    dest: SkillDestinationDep,
    s3: S3Dep,
) -> dict[str, object]:
    require_table_bucket_access(table_bucket_arn, user, tables)
    return skill_bundle.list_skill_versions(s3, table_bucket_arn, **dest)


@router.post("/versions", status_code=201)
async def upload_version(
    user: UserDep,
    tables: TableBucketServiceDep,
    dest: SkillDestinationDep,
    s3: S3Dep,
    table_bucket_arn: Annotated[str, Form()],
    file: Annotated[UploadFile, File()],
    uploaded_by: Annotated[
        str,
        Form(min_length=1, max_length=skill_bundle.MAX_UPLOADED_BY_CHARS),
    ],
    description: Annotated[str, Form(max_length=skill_bundle.MAX_DESCRIPTION_CHARS)] = "",
) -> dict[str, object]:
    require_table_bucket_access(table_bucket_arn, user, tables)
    try:
        content = await file.read(skill_bundle.MAX_ZIP_BYTES + 1)
        return await run_in_threadpool(
            skill_bundle.publish_version,
            s3,
            table_bucket_arn,
            file.filename or "",
            content,
            uploaded_by,
            description,
            **dest,
        )
    finally:
        await file.close()


@router.get("/versions/download")
def download_version(
    table_bucket_arn: Annotated[str, Query()],
    version_id: Annotated[str, Query(min_length=1, max_length=1024)],
    user: UserDep,
    tables: TableBucketServiceDep,
    dest: SkillDestinationDep,
    s3: S3Dep,
) -> StreamingResponse:
    require_table_bucket_access(table_bucket_arn, user, tables)
    bucket, key = skill_bundle.version_location(table_bucket_arn, **dest)
    try:
        result = s3.get_object(Bucket=bucket, Key=key, VersionId=version_id)
    except ClientError as error:
        raise _not_found_or_gateway(error) from error
    filename = key.rsplit("/", 1)[-1]
    headers = {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"}
    if result.get("ContentLength") is not None:
        headers["Content-Length"] = str(result["ContentLength"])
    return StreamingResponse(_stream(result["Body"]), media_type="application/zip", headers=headers)

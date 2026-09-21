"""Skill bundle upload / download / delete."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote

from botocore.exceptions import ClientError
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ...app.dependencies import (
    SkillServiceDep,
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
    if code in {"404", "NoSuchKey", "NotFound"}:
        return HTTPException(404, "The requested skill file no longer exists")
    return HTTPException(502, "Unable to reach S3 for the requested skill file")


# ---------------------------------------------------------------------------
# Skill files
# ---------------------------------------------------------------------------

@router.get("/files")
def list_files(
    table_bucket_arn: str,
    user: UserDep,
    tables: TableBucketServiceDep,
    skills: SkillServiceDep,
) -> dict[str, object]:
    require_table_bucket_access(table_bucket_arn, user, tables)
    return skills.list_files(table_bucket_arn)


@router.post("/files")
async def upload_files(
    user: UserDep,
    tables: TableBucketServiceDep,
    skills: SkillServiceDep,
    table_bucket_arn: str = Form(),
    paths_json: str = Form(),
    files: list[UploadFile] = File(),
) -> dict[str, object]:
    require_table_bucket_access(table_bucket_arn, user, tables)
    paths = skills.parse_paths_json(paths_json)
    if len(paths) != len(files):
        raise HTTPException(422, "Each uploaded skill file must have one matching relative path")
    try:
        payload = [
            (path, await upload.read(skills.max_file_bytes + 1))
            for path, upload in zip(paths, files, strict=True)
        ]
        return skills.publish_files(table_bucket_arn, user.user_id, payload)
    finally:
        for upload in files:
            await upload.close()


@router.get("/files/download")
def download_file(
    table_bucket_arn: str,
    path: str,
    user: UserDep,
    tables: TableBucketServiceDep,
    skills: SkillServiceDep,
) -> StreamingResponse:
    require_table_bucket_access(table_bucket_arn, user, tables)
    destination_bucket, key, safe_path = skills.file_location(table_bucket_arn, path)
    try:
        result = skills.s3.get_object(Bucket=destination_bucket, Key=key)
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
    skills: SkillServiceDep,
) -> dict[str, str]:
    require_table_bucket_access(payload.table_bucket_arn, user, tables)
    if not payload.confirm:
        raise HTTPException(422, "Confirm deletion before removing a skill file")
    destination_bucket, key, safe_path = skills.file_location(
        payload.table_bucket_arn, payload.path
    )
    try:
        skills.s3.head_object(Bucket=destination_bucket, Key=key)
        skills.s3.delete_object(Bucket=destination_bucket, Key=key)
    except ClientError as error:
        raise _not_found_or_gateway(error) from error
    return {"deleted_path": safe_path}


# ---------------------------------------------------------------------------
# Skill versions
# ---------------------------------------------------------------------------

@router.get("/versions")
def list_versions(
    table_bucket_arn: str,
    user: UserDep,
    tables: TableBucketServiceDep,
    skills: SkillServiceDep,
) -> dict[str, object]:
    require_table_bucket_access(table_bucket_arn, user, tables)
    return skills.list_versions(table_bucket_arn)


@router.post("/versions", status_code=201)
async def upload_version(
    user: UserDep,
    tables: TableBucketServiceDep,
    skills: SkillServiceDep,
    table_bucket_arn: str = Form(),
    file: UploadFile = File(),
) -> dict[str, object]:
    require_table_bucket_access(table_bucket_arn, user, tables)
    try:
        content = await file.read(skills.max_zip_bytes + 1)
        return skills.publish_version(
            table_bucket_arn, user.user_id, file.filename or "", content
        )
    finally:
        await file.close()


@router.get("/versions/download")
def download_version(
    table_bucket_arn: str,
    filename: str,
    user: UserDep,
    tables: TableBucketServiceDep,
    skills: SkillServiceDep,
) -> StreamingResponse:
    require_table_bucket_access(table_bucket_arn, user, tables)
    bucket, key = skills.version_location(table_bucket_arn, filename)
    try:
        result = skills.s3.get_object(Bucket=bucket, Key=key)
    except ClientError as error:
        raise _not_found_or_gateway(error) from error
    headers = {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"}
    if result.get("ContentLength") is not None:
        headers["Content-Length"] = str(result["ContentLength"])
    return StreamingResponse(_stream(result["Body"]), media_type="application/zip", headers=headers)


@router.delete("/versions")
def delete_version(
    payload: DeleteSkillFileRequest,
    user: UserDep,
    tables: TableBucketServiceDep,
    skills: SkillServiceDep,
) -> dict[str, str]:
    require_table_bucket_access(payload.table_bucket_arn, user, tables)
    if not payload.confirm:
        raise HTTPException(422, "Confirm deletion before removing a skill version")
    bucket, key = skills.version_location(payload.table_bucket_arn, payload.path)
    try:
        skills.s3.head_object(Bucket=bucket, Key=key)
        skills.s3.delete_object(Bucket=bucket, Key=key)
    except ClientError as error:
        raise _not_found_or_gateway(error) from error
    return {"deleted_filename": payload.path}

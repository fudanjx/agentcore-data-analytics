"""Validate and publish complete, user-supplied Agent Skill bundles to S3."""

from __future__ import annotations

import json
import io
import mimetypes
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote, unquote

from botocore.exceptions import BotoCoreError, ClientError

from .core.constants import S3_SSE
from .core.exceptions import UploaderError


MAX_FILES = 500
MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_TOTAL_BYTES = 250 * 1024 * 1024
MAX_ZIP_BYTES = 50 * 1024 * 1024
MAX_DESCRIPTION_CHARS = 500
MAX_ENCODED_DESCRIPTION_BYTES = 1024
MAX_LISTED_VERSIONS = 10
_BUCKET_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")
_FRONTMATTER_RE = re.compile(
    r"\A---[ \t]*\r?\n(?P<header>.*?)\r?\n---[ \t]*(?P<rest>\r?\n.*|\Z)",
    re.DOTALL,
)
_NAME_RE = re.compile(r"(?m)^name\s*:.*$")
_DESCRIPTION_RE = re.compile(r"(?m)^description\s*:\s*(?P<value>.*)$")


class SkillBundleError(UploaderError):
    """Safe client-facing validation or S3 publication failure.

    Inherits from :class:`UploaderError` so the global exception handler
    translates it into the standard JSON shape without router-level
    try/except.
    """

    error_code = "SKILL_BUNDLE_ERROR"

    def __init__(self, message: str, status_code: int = 422):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class SkillBundleFile:
    path: str
    content: bytes


def table_bucket_name(table_bucket_arn: str) -> str:
    name = table_bucket_arn.rstrip("/").rsplit("/", 1)[-1]
    if not _BUCKET_NAME_RE.fullmatch(name):
        raise SkillBundleError("The selected S3 Tables bucket has an invalid skill name")
    return name


def _destination(
    bucket_name: str,
    *,
    destination_bucket: str,
    destination_prefix: str,
) -> tuple[str, str, str]:
    """Return the S3 destination for this table bucket's skill files.

    ``destination_bucket`` / ``destination_prefix`` are the Settings-driven
    values threaded through from the API layer (see
    ``app.dependencies.get_skill_destination``). Both are required — the
    module does not read env vars directly.
    """
    bucket = destination_bucket.strip()
    raw_prefix = destination_prefix.strip()
    prefix_parts = [part for part in raw_prefix.replace("\\", "/").split("/") if part]
    if not bucket or any(part in {".", ".."} for part in prefix_parts):
        raise SkillBundleError("The skill-bundle destination configuration is invalid", 503)
    prefix = "/".join([*prefix_parts, bucket_name])
    return bucket, prefix, f"s3://{bucket}/{prefix}/"


def _safe_relative_path(value: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise SkillBundleError("Each skill-bundle path must be a safe non-empty relative path")
    candidate = PurePosixPath(value)
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        raise SkillBundleError("Skill-bundle paths cannot be absolute or contain traversal")
    normal = candidate.as_posix()
    if normal != value or len(normal) > 1024:
        raise SkillBundleError("Skill-bundle path is invalid")
    return normal


def _normalise_skill_frontmatter(raw: bytes, bucket_name: str) -> bytes:
    try:
        content = raw.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise SkillBundleError("SKILL.md must be valid UTF-8 text") from error
    match = _FRONTMATTER_RE.match(content)
    if not match:
        raise SkillBundleError("SKILL.md must start with YAML frontmatter")
    header = match.group("header")
    descriptions = list(_DESCRIPTION_RE.finditer(header))
    description = descriptions[0].group("value").strip() if len(descriptions) == 1 else ""
    if not description or description in {"''", "\"\""}:
        raise SkillBundleError("SKILL.md frontmatter must contain one non-empty description")
    names = list(_NAME_RE.finditer(header))
    if len(names) > 1:
        raise SkillBundleError("SKILL.md frontmatter cannot contain more than one name")
    if names:
        header = _NAME_RE.sub(f"name: {bucket_name}", header, count=1)
    else:
        header = f"name: {bucket_name}\n{header}"
    return f"---\n{header}\n---{match.group('rest')}".encode("utf-8")


def parse_paths_json(value: str) -> list[str]:
    try:
        paths = json.loads(value)
    except (TypeError, json.JSONDecodeError) as error:
        raise SkillBundleError("Skill bundle paths must be a JSON array") from error
    if not isinstance(paths, list) or not all(isinstance(item, str) for item in paths):
        raise SkillBundleError("Skill bundle paths must be a JSON array of strings")
    return paths


def validate_bundle(table_bucket_arn: str, files: list[tuple[str, bytes]]) -> tuple[str, list[SkillBundleFile]]:
    """Validate a complete bundle for the legacy replacement endpoint.

    New file-explorer uploads use :func:`validate_upload_files`, which permits
    an individual resource file to be uploaded without requiring ``SKILL.md``.
    """
    bucket_name, normalised = validate_upload_files(table_bucket_arn, files)
    if not any(item.path == "SKILL.md" for item in normalised):
        raise SkillBundleError("A skill bundle must contain exactly one root SKILL.md")
    return bucket_name, normalised


def validate_upload_files(table_bucket_arn: str, files: list[tuple[str, bytes]]) -> tuple[str, list[SkillBundleFile]]:
    """Validate files for incremental upload below one bucket's skill prefix.

    Root ``SKILL.md`` is normalised when included.  Other files can be added or
    replaced independently, so users can maintain ``references/``, ``scripts/``
    and ``assets/`` without replacing unrelated objects.
    """
    bucket_name = table_bucket_name(table_bucket_arn)
    if not files or len(files) > MAX_FILES:
        raise SkillBundleError(f"A skill bundle must contain between 1 and {MAX_FILES} files")
    seen: set[str] = set()
    total = 0
    bundle: list[SkillBundleFile] = []
    for supplied_path, content in files:
        path = _safe_relative_path(supplied_path)
        if path in seen:
            raise SkillBundleError(f"Skill bundle contains duplicate path: {path}")
        if len(content) > MAX_FILE_BYTES:
            raise SkillBundleError(f"Skill bundle file exceeds {MAX_FILE_BYTES // (1024 * 1024)} MB")
        total += len(content)
        if total > MAX_TOTAL_BYTES:
            raise SkillBundleError(f"Skill bundle exceeds {MAX_TOTAL_BYTES // (1024 * 1024)} MB total")
        seen.add(path)
        bundle.append(SkillBundleFile(path=path, content=content))
    normalised: list[SkillBundleFile] = []
    for item in bundle:
        content = _normalise_skill_frontmatter(item.content, bucket_name) if item.path == "SKILL.md" else item.content
        normalised.append(SkillBundleFile(path=item.path, content=content))
    return bucket_name, normalised


def _existing_object_keys(s3_client: Any, destination_bucket: str, destination_prefix: str) -> set[str]:
    existing: set[str] = set()
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=destination_bucket, Prefix=f"{destination_prefix}/"):
        existing.update(item["Key"] for item in page.get("Contents", []))
    return existing


def list_skill_files(
    s3_client: Any,
    table_bucket_arn: str,
    *,
    destination_bucket: str,
    destination_prefix: str,
) -> dict:
    """Return safe, relative object metadata for one table bucket's skill area."""
    bucket_name = table_bucket_name(table_bucket_arn)
    destination_bucket, destination_prefix, destination_uri = _destination(
        bucket_name,
        destination_bucket=destination_bucket,
        destination_prefix=destination_prefix,
    )
    try:
        files: list[dict] = []
        paginator = s3_client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=destination_bucket, Prefix=f"{destination_prefix}/"):
            for item in page.get("Contents", []):
                key = item.get("Key", "")
                path = key.removeprefix(f"{destination_prefix}/")
                try:
                    path = _safe_relative_path(path)
                except SkillBundleError:
                    # Ignore S3 folder-marker objects or any pre-existing
                    # malformed object; the API never exposes it as a path.
                    continue
                modified = item.get("LastModified")
                files.append(
                    {
                        "path": path,
                        "size": int(item.get("Size", 0)),
                        "last_modified": modified.isoformat() if modified else None,
                    }
                )
    except (BotoCoreError, ClientError) as error:
        raise SkillBundleError("Unable to list skill files from S3", 502) from error
    return {
        "skill_name": bucket_name,
        "destination_uri": destination_uri,
        "files": sorted(files, key=lambda item: item["path"].casefold()),
    }


def validate_version_zip(table_bucket_arn: str, filename: str, content: bytes) -> None:
    """Validate a complete skill ZIP before storing the original bytes."""
    if not filename.lower().endswith(".zip") or not content or len(content) > MAX_ZIP_BYTES:
        raise SkillBundleError("Upload one ZIP file of at most 50 MB")
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            members = archive.infolist()
            if len(members) > MAX_FILES * 2:
                raise SkillBundleError("The skill ZIP contains too many entries")
            entries = [entry for entry in members if not entry.is_dir()]
            if not entries or len(entries) > MAX_FILES:
                raise SkillBundleError(f"A skill ZIP must contain 1 to {MAX_FILES} files")
            seen: set[str] = set()
            total = 0
            for entry in members:
                path = _safe_relative_path(entry.filename[:-1] if entry.is_dir() else entry.filename)
                file_type = (entry.external_attr >> 16) & 0o170000
                if entry.flag_bits & 1 or file_type == 0o120000:
                    raise SkillBundleError("The skill ZIP contains an encrypted or linked entry")
                if entry.is_dir():
                    continue
                if path in seen:
                    raise SkillBundleError("The skill ZIP contains a duplicate file")
                seen.add(path)
                total += entry.file_size
                if entry.file_size > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
                    raise SkillBundleError("The skill ZIP expands beyond the allowed size")
            skill_paths = [path for path in seen if path == "SKILL.md" or (path.count("/") == 1 and path.endswith("/SKILL.md"))]
            if len(skill_paths) != 1:
                raise SkillBundleError("The skill ZIP must contain one SKILL.md at the root or inside one enclosing folder")
            skill_path = skill_paths[0]
            if skill_path != "SKILL.md":
                folder = skill_path.split("/", 1)[0]
                if any(not path.startswith(f"{folder}/") for path in seen):
                    raise SkillBundleError("All files in a wrapped skill ZIP must share one enclosing folder")
            _normalise_skill_frontmatter(archive.read(skill_path), table_bucket_name(table_bucket_arn))
            for entry in entries:
                # Reading members verifies their CRCs before publication.
                if entry.filename != skill_path:
                    archive.read(entry)
    except (zipfile.BadZipFile, EOFError, RuntimeError, OSError, NotImplementedError) as error:
        raise SkillBundleError("The uploaded file is not a valid ZIP archive") from error


def version_location(
    table_bucket_arn: str,
    *,
    destination_bucket: str,
    destination_prefix: str,
) -> tuple[str, str]:
    bucket_name = table_bucket_name(table_bucket_arn)
    bucket, prefix, _ = _destination(
        bucket_name,
        destination_bucket=destination_bucket,
        destination_prefix=destination_prefix,
    )
    return bucket, f"{prefix}/{bucket_name}.zip"


def _normalise_description(description: str) -> tuple[str, str]:
    normalised = description.strip()
    if len(normalised) > MAX_DESCRIPTION_CHARS:
        raise SkillBundleError(f"Description must be at most {MAX_DESCRIPTION_CHARS} characters")
    encoded = quote(normalised, safe="")
    if len(encoded.encode("ascii")) > MAX_ENCODED_DESCRIPTION_BYTES:
        raise SkillBundleError("Description is too large to store in S3 object metadata")
    return normalised, encoded


def ensure_bucket_versioning(s3_client: Any, bucket: str) -> None:
    """Enable native S3 versioning on the configured skill archive bucket."""
    try:
        status = s3_client.get_bucket_versioning(Bucket=bucket).get("Status")
        if status != "Enabled":
            s3_client.put_bucket_versioning(
                Bucket=bucket,
                VersioningConfiguration={"Status": "Enabled"},
            )
    except (BotoCoreError, ClientError) as error:
        raise SkillBundleError("Unable to enable versioning on the skill-bundle S3 bucket", 502) from error


def list_skill_versions(
    s3_client: Any,
    table_bucket_arn: str,
    *,
    destination_bucket: str,
    destination_prefix: str,
) -> dict:
    bucket_name = table_bucket_name(table_bucket_arn)
    bucket, prefix, _ = _destination(
        bucket_name,
        destination_bucket=destination_bucket,
        destination_prefix=destination_prefix,
    )
    key = f"{prefix}/{bucket_name}.zip"
    uri = f"s3://{bucket}/{key}"
    versions = []
    try:
        for page in s3_client.get_paginator("list_object_versions").paginate(Bucket=bucket, Prefix=key):
            for item in page.get("Versions", []):
                if item.get("Key") != key:
                    continue
                version_id = item.get("VersionId")
                if not version_id:
                    continue
                uploaded = item.get("LastModified")
                versions.append({
                    "version_id": version_id,
                    "is_latest": bool(item.get("IsLatest")),
                    "uploaded_at": uploaded.isoformat() if uploaded else None,
                    "size": int(item.get("Size", 0)),
                })
        versions = sorted(
            versions,
            key=lambda item: (item["uploaded_at"] or "", item["version_id"]),
            reverse=True,
        )[:MAX_LISTED_VERSIONS]
        for version in versions:
            metadata = s3_client.head_object(
                Bucket=bucket, Key=key, VersionId=version["version_id"]
            ).get("Metadata", {})
            version["description"] = unquote(metadata.get("description", ""))
    except (BotoCoreError, ClientError) as error:
        raise SkillBundleError("Unable to list skill versions from S3", 502) from error
    return {
        "skill_name": bucket_name,
        "destination_uri": uri,
        "versions": versions,
    }


def publish_version(
    s3_client: Any,
    table_bucket_arn: str,
    user_id: str,
    filename: str,
    content: bytes,
    description: str = "",
    *,
    destination_bucket: str,
    destination_prefix: str,
) -> dict:
    validate_version_zip(table_bucket_arn, filename, content)
    description, encoded_description = _normalise_description(description)
    bucket_name = table_bucket_name(table_bucket_arn)
    bucket, prefix, _ = _destination(
        bucket_name,
        destination_bucket=destination_bucket,
        destination_prefix=destination_prefix,
    )
    key = f"{prefix}/{bucket_name}.zip"
    uri = f"s3://{bucket}/{key}"
    uploaded = datetime.now(timezone.utc)
    try:
        ensure_bucket_versioning(s3_client, bucket)
        result = s3_client.put_object(
            Bucket=bucket, Key=key, Body=content,
            ContentType="application/zip", ServerSideEncryption=S3_SSE,
            Metadata={
                "s3-table-bucket": bucket_name,
                "uploaded-by": quote(user_id, safe="@._-")[:256],
                "original-filename": quote(filename, safe="._-")[:256],
                "description": encoded_description,
            },
        )
    except (BotoCoreError, ClientError) as error:
        raise SkillBundleError("Unable to upload the skill version to S3", 502) from error
    version_id = result.get("VersionId")
    if not version_id:
        raise SkillBundleError(
            "S3 did not return a version ID for the uploaded skill; verify bucket versioning is enabled",
            502,
        )
    return {
        "skill_name": bucket_name,
        "destination_uri": uri,
        "filename": f"{bucket_name}.zip",
        "version_id": version_id,
        "uploaded_at": uploaded.isoformat(),
        "size": len(content),
        "description": description,
    }


def skill_file_location(
    table_bucket_arn: str,
    path: str,
    *,
    destination_bucket: str,
    destination_prefix: str,
) -> tuple[str, str, str]:
    """Return the configured S3 bucket/key after validating a relative path."""
    bucket_name = table_bucket_name(table_bucket_arn)
    safe_path = _safe_relative_path(path)
    destination_bucket, destination_prefix, _ = _destination(
        bucket_name,
        destination_bucket=destination_bucket,
        destination_prefix=destination_prefix,
    )
    return destination_bucket, f"{destination_prefix}/{safe_path}", safe_path


def publish_files(
    s3_client: Any,
    table_bucket_arn: str,
    user_id: str,
    files: list[tuple[str, bytes]],
    *,
    destination_bucket: str,
    destination_prefix: str,
) -> dict:
    """Add or overwrite only the supplied skill files; retain all other files."""
    bucket_name, bundle = validate_upload_files(table_bucket_arn, files)
    destination_bucket, destination_prefix, destination_uri = _destination(
        bucket_name,
        destination_bucket=destination_bucket,
        destination_prefix=destination_prefix,
    )
    try:
        existing = _existing_object_keys(s3_client, destination_bucket, destination_prefix)
        ordered = sorted(bundle, key=lambda item: (item.path == "SKILL.md", item.path))
        created: list[str] = []
        overwritten: list[str] = []
        for item in ordered:
            key = f"{destination_prefix}/{item.path}"
            if key in existing:
                overwritten.append(item.path)
            else:
                created.append(item.path)
            content_type = mimetypes.guess_type(item.path)[0] or "application/octet-stream"
            if item.path.endswith(".md"):
                content_type = "text/markdown; charset=utf-8"
            s3_client.put_object(
                Bucket=destination_bucket, Key=key, Body=item.content,
                ContentType=content_type,
                ServerSideEncryption=S3_SSE,
                Metadata={"s3-table-bucket": bucket_name, "uploaded-by": quote(user_id, safe="@._-")[:256]},
            )
    except (BotoCoreError, ClientError) as error:
        raise SkillBundleError("Unable to upload skill files to S3", 502) from error
    return {
        "skill_name": bucket_name,
        "destination_uri": destination_uri,
        "uploaded_paths": [item.path for item in ordered],
        "created_paths": created,
        "overwritten_paths": overwritten,
        "restart_reminder": "Restart or resynchronize the consuming runtime before it uses changed skill files.",
    }


def publish_bundle(
    s3_client: Any,
    table_bucket_arn: str,
    user_id: str,
    files: list[tuple[str, bytes]],
    *,
    destination_bucket: str,
    destination_prefix: str,
) -> dict:
    """Compatibility wrapper; no longer deletes files absent from an upload."""
    validate_bundle(table_bucket_arn, files)
    result = publish_files(
        s3_client,
        table_bucket_arn,
        user_id,
        files,
        destination_bucket=destination_bucket,
        destination_prefix=destination_prefix,
    )
    return {**result, "deleted_paths": []}

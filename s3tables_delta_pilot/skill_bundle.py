"""Validate and publish complete, user-supplied Agent Skill bundles to S3."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import quote

import boto3
from botocore.exceptions import BotoCoreError, ClientError


DEFAULT_DESTINATION_BUCKET = "agentcore-harness-dev"
DEFAULT_DESTINATION_PREFIX = "skills"
MAX_FILES = 500
MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_TOTAL_BYTES = 250 * 1024 * 1024
_BUCKET_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")
_FRONTMATTER_RE = re.compile(
    r"\A---[ \t]*\r?\n(?P<header>.*?)\r?\n---[ \t]*(?P<rest>\r?\n.*|\Z)",
    re.DOTALL,
)
_NAME_RE = re.compile(r"(?m)^name\s*:.*$")
_DESCRIPTION_RE = re.compile(r"(?m)^description\s*:\s*(?P<value>.*)$")

s3 = boto3.client(
    "s3",
    region_name=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "ap-southeast-1",
)


class SkillBundleError(RuntimeError):
    """Safe client-facing validation or S3 publication failure."""

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


def _destination(bucket_name: str) -> tuple[str, str, str]:
    bucket = os.environ.get("PILOT_SKILL_BUNDLE_BUCKET", DEFAULT_DESTINATION_BUCKET).strip()
    raw_prefix = os.environ.get("PILOT_SKILL_BUNDLE_PREFIX", DEFAULT_DESTINATION_PREFIX).strip()
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
    if "SKILL.md" not in seen:
        raise SkillBundleError("A skill bundle must contain exactly one root SKILL.md")
    normalised: list[SkillBundleFile] = []
    for item in bundle:
        content = _normalise_skill_frontmatter(item.content, bucket_name) if item.path == "SKILL.md" else item.content
        normalised.append(SkillBundleFile(path=item.path, content=content))
    return bucket_name, normalised


def publish_bundle(table_bucket_arn: str, user_id: str, files: list[tuple[str, bytes]]) -> dict:
    bucket_name, bundle = validate_bundle(table_bucket_arn, files)
    destination_bucket, destination_prefix, destination_uri = _destination(bucket_name)
    desired = {f"{destination_prefix}/{item.path}" for item in bundle}
    try:
        existing: set[str] = set()
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=destination_bucket, Prefix=f"{destination_prefix}/"):
            existing.update(item["Key"] for item in page.get("Contents", []))
        ordered = sorted(bundle, key=lambda item: (item.path == "SKILL.md", item.path))
        uploaded: list[str] = []
        for item in ordered:
            key = f"{destination_prefix}/{item.path}"
            s3.put_object(
                Bucket=destination_bucket, Key=key, Body=item.content,
                ContentType="text/markdown; charset=utf-8" if item.path.endswith(".md") else "application/octet-stream",
                ServerSideEncryption="AES256",
                Metadata={"s3-table-bucket": bucket_name, "uploaded-by": quote(user_id, safe="@._-")[:256]},
            )
            uploaded.append(item.path)
        stale = sorted(existing - desired)
        for key in stale:
            s3.delete_object(Bucket=destination_bucket, Key=key)
    except (BotoCoreError, ClientError) as error:
        raise SkillBundleError("Unable to publish the complete skill bundle to S3", 502) from error
    return {
        "skill_name": bucket_name,
        "destination_uri": destination_uri,
        "uploaded_paths": uploaded,
        "deleted_paths": [key.removeprefix(f"{destination_prefix}/") for key in stale],
        "restart_reminder": "Restart or resynchronize the consuming runtime before it uses the replacement skill bundle.",
    }

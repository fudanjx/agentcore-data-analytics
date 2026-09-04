"""Build, validate, and publish one Agent Skill per S3 Tables bucket."""

from __future__ import annotations

import os
import re
from urllib.parse import quote, unquote, urlsplit

import boto3
import httpx
from botocore.exceptions import BotoCoreError, ClientError


DEFAULT_DIFY_URL = "https://dify-eks.bot-alex.com/v1/chat-messages"
DEFAULT_DESTINATION_BUCKET = "agentcore-harness-dev"
DEFAULT_DESTINATION_PREFIX = "skills/"
DEFAULT_MAX_BYTES = 200_000
DEFAULT_TIMEOUT_SECONDS = 300
_S3_URI_RE = re.compile(r"s3://[^\s<>()\[\]{}\"'`]+")
_BUCKET_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")
_FRONTMATTER_RE = re.compile(
    r"\A---[ \t]*\r?\n(?P<header>.*?)\r?\n---[ \t]*(?P<rest>\r?\n.*|\Z)",
    re.DOTALL,
)
_NAME_RE = re.compile(r"(?m)^name\s*:.*$")
_DESCRIPTION_RE = re.compile(r"(?m)^description\s*:\s*(?P<value>.*)$")

s3 = boto3.client(
    "s3",
    region_name=(
        os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or "ap-southeast-1"
    ),
)


class SkillBuildError(RuntimeError):
    """Safe, user-visible failure raised by the skill-building integration."""

    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


def _positive_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as error:
        raise SkillBuildError(f"{name} must be an integer", 503) from error
    if value < 1:
        raise SkillBuildError(f"{name} must be greater than zero", 503)
    return value


def table_bucket_name(table_bucket_arn: str) -> str:
    """Return the bucket name used as both skill directory and skill name."""
    name = table_bucket_arn.rstrip("/").rsplit("/", 1)[-1]
    if not _BUCKET_NAME_RE.fullmatch(name):
        raise SkillBuildError("The selected S3 Tables bucket has an invalid skill name", 422)
    return name


def destination(bucket_name: str) -> tuple[str, str, str]:
    destination_bucket = os.environ.get(
        "SKILL_BUILD_DESTINATION_BUCKET", DEFAULT_DESTINATION_BUCKET
    ).strip()
    if not destination_bucket:
        raise SkillBuildError("SKILL_BUILD_DESTINATION_BUCKET is empty", 503)

    configured_prefix = os.environ.get(
        "SKILL_BUILD_DESTINATION_PREFIX", DEFAULT_DESTINATION_PREFIX
    ).strip()
    parts = [part for part in configured_prefix.replace("\\", "/").split("/") if part]
    if any(part in {".", ".."} for part in parts):
        raise SkillBuildError("SKILL_BUILD_DESTINATION_PREFIX is invalid", 503)
    prefix = "/".join(parts)
    key = "/".join(part for part in (prefix, bucket_name, "SKILL.md") if part)
    return destination_bucket, key, f"s3://{destination_bucket}/{key}"


def extract_skill_uri(answer: str) -> str:
    """Extract exactly one S3 SKILL.md URI from a Dify answer."""
    candidates: list[str] = []
    for raw in _S3_URI_RE.findall(answer):
        candidate = raw.rstrip(".,;:!?")
        parsed = urlsplit(candidate)
        key = unquote(parsed.path.lstrip("/"))
        if (
            parsed.scheme == "s3"
            and parsed.netloc
            and not parsed.query
            and not parsed.fragment
            and key.rsplit("/", 1)[-1] == "SKILL.md"
        ):
            candidates.append(candidate)
    candidates = list(dict.fromkeys(candidates))
    if not candidates:
        raise SkillBuildError("Dify did not return an S3 URI for SKILL.md", 502)
    if len(candidates) != 1:
        raise SkillBuildError("Dify returned more than one S3 URI for SKILL.md", 502)
    return candidates[0]


def normalize_skill_name(content: str, bucket_name: str) -> str:
    """Force YAML frontmatter name to match the S3 skill directory."""
    match = _FRONTMATTER_RE.match(content)
    if not match:
        raise SkillBuildError("Generated SKILL.md has no valid YAML frontmatter", 422)

    header = match.group("header")
    descriptions = list(_DESCRIPTION_RE.finditer(header))
    description = descriptions[0].group("value").strip() if len(descriptions) == 1 else ""
    if not description or description in {'""', "''"}:
        raise SkillBuildError(
            "Generated SKILL.md must have one non-empty frontmatter description",
            422,
        )

    names = list(_NAME_RE.finditer(header))
    if len(names) > 1:
        raise SkillBuildError("Generated SKILL.md has multiple frontmatter names", 422)
    if names:
        header = _NAME_RE.sub(f"name: {bucket_name}", header, count=1)
    else:
        header = f"name: {bucket_name}\n{header}"

    return f"---\n{header}\n---{match.group('rest')}"


def _call_dify(instruction: str, user_id: str) -> str:
    api_key = os.environ.get("SKILL_BUILD_DIFY_API_KEY", "").strip()
    if not api_key:
        raise SkillBuildError("SKILL_BUILD_DIFY_API_KEY is not configured", 503)
    url = os.environ.get("SKILL_BUILD_DIFY_URL", DEFAULT_DIFY_URL).strip()
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
        raise SkillBuildError("SKILL_BUILD_DIFY_URL must be an HTTPS endpoint", 503)

    timeout = _positive_int_env(
        "SKILL_BUILD_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS
    )
    try:
        response = httpx.post(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "inputs": {},
                "query": instruction,
                "response_mode": "blocking",
                "conversation_id": "",
                "user": user_id,
                "files": [],
            },
            timeout=timeout,
        )
        response.raise_for_status()
    except httpx.TimeoutException as error:
        raise SkillBuildError("Dify skill generation timed out", 504) from error
    except httpx.HTTPStatusError as error:
        raise SkillBuildError(
            f"Dify skill generation returned HTTP {error.response.status_code}", 502
        ) from error
    except httpx.RequestError as error:
        raise SkillBuildError("Unable to reach the Dify skill builder", 502) from error

    try:
        payload = response.json()
    except ValueError as error:
        raise SkillBuildError("Dify returned an invalid JSON response", 502) from error
    answer = payload.get("answer") if isinstance(payload, dict) else None
    if not isinstance(answer, str) or not answer.strip():
        raise SkillBuildError("Dify returned no skill-build answer", 502)
    return answer


def _download_skill(uri: str) -> str:
    parsed = urlsplit(uri)
    bucket = parsed.netloc
    key = unquote(parsed.path.lstrip("/"))
    max_bytes = _positive_int_env("SKILL_BUILD_MAX_BYTES", DEFAULT_MAX_BYTES)
    try:
        response = s3.get_object(Bucket=bucket, Key=key)
        body = response["Body"]
        try:
            raw = body.read(max_bytes + 1)
        finally:
            close = getattr(body, "close", None)
            if callable(close):
                close()
    except (BotoCoreError, ClientError, KeyError) as error:
        raise SkillBuildError("Unable to download Dify's generated SKILL.md", 502) from error
    if len(raw) > max_bytes:
        raise SkillBuildError(
            f"Generated SKILL.md exceeds SKILL_BUILD_MAX_BYTES={max_bytes}", 422
        )
    try:
        content = raw.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise SkillBuildError("Generated SKILL.md must contain UTF-8 text", 422) from error
    if not content.strip():
        raise SkillBuildError("Generated SKILL.md is empty", 422)
    return content


def build_skill_draft(
    instruction: str, user_id: str, table_bucket_arn: str
) -> dict[str, str]:
    bucket_name = table_bucket_name(table_bucket_arn)
    answer = _call_dify(instruction, user_id)
    source_uri = extract_skill_uri(answer)
    content = normalize_skill_name(_download_skill(source_uri), bucket_name)
    _, _, destination_uri = destination(bucket_name)
    return {
        "source_uri": source_uri,
        "destination_uri": destination_uri,
        "skill_name": bucket_name,
        "content": content,
    }


def publish_skill(content: str, user_id: str, table_bucket_arn: str) -> dict:
    bucket_name = table_bucket_name(table_bucket_arn)
    max_bytes = _positive_int_env("SKILL_BUILD_MAX_BYTES", DEFAULT_MAX_BYTES)
    normalized = normalize_skill_name(content, bucket_name)
    encoded = normalized.encode("utf-8")
    if len(encoded) > max_bytes:
        raise SkillBuildError(
            f"Edited SKILL.md exceeds SKILL_BUILD_MAX_BYTES={max_bytes}", 422
        )
    destination_bucket, key, destination_uri = destination(bucket_name)
    try:
        result = s3.put_object(
            Bucket=destination_bucket,
            Key=key,
            Body=encoded,
            ContentType="text/markdown; charset=utf-8",
            ServerSideEncryption="AES256",
            Metadata={
                "s3-table-bucket": bucket_name,
                "published-by": quote(user_id, safe="@._-")[:256],
            },
        )
    except (BotoCoreError, ClientError) as error:
        raise SkillBuildError("Unable to publish the confirmed SKILL.md", 502) from error
    return {
        "destination_uri": destination_uri,
        "skill_name": bucket_name,
        "content": normalized,
        "etag": str(result.get("ETag", "")).strip('"') or None,
        "version_id": result.get("VersionId"),
    }

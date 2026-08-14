"""Sync complete Agent Skills from S3 and expose their local text resources."""

import logging
import os
from pathlib import Path

import boto3
from strands import tool


logger = logging.getLogger(__name__)
BUCKET = os.environ.get("SKILLS_BUCKET", "").strip()
PREFIX = os.environ.get("SKILLS_PREFIX", "").strip()
if PREFIX and not PREFIX.endswith("/"):
    PREFIX += "/"
LOCAL_DIR = Path(os.environ.get("SKILLS_LOCAL_DIR", "/tmp/strands-agent-skills"))
MAX_RESOURCE_CHARS = max(
    1_000, int(os.environ.get("SKILLS_MAX_RESOURCE_CHARS", "100000"))
)
MAX_OBJECT_BYTES = max(
    1_000, int(os.environ.get("SKILLS_MAX_OBJECT_BYTES", "50000000"))
)
MAX_SYNC_BYTES = max(
    MAX_OBJECT_BYTES, int(os.environ.get("SKILLS_MAX_SYNC_BYTES", "250000000"))
)
ACTIVATION_GUIDANCE = """

---

## Agent Skills

- The available-skills list contains only skill names and descriptions. When a skill matches the request, activate it with the skills tool before using the related MCP Gateway or Code Interpreter tools.
- After activation, follow the complete skill instructions. Read every required UTF-8 text reference with read_skill_resource before constructing a query or analysis.
- Skills provide operational guidance; MCP Gateway and Code Interpreter remain the tools that retrieve data and perform work.
"""


def skills_enabled() -> bool:
    """Return whether an S3 skills bucket is configured."""
    return bool(BUCKET)


def _local_path_for_key(key: str) -> Path:
    """Map an S3 object key safely beneath the configured local skill root."""
    if not key.startswith(PREFIX):
        raise ValueError("object key is outside the configured skills prefix")
    relative = key.removeprefix(PREFIX)
    parts = relative.split("/")
    if not relative or any(
        part in {"", ".", ".."} or "\\" in part or ":" in part or "\x00" in part
        for part in parts
    ):
        raise ValueError("object key contains an unsafe path")
    root = LOCAL_DIR.resolve()
    local = root.joinpath(*parts).resolve()
    if root not in local.parents:
        raise ValueError("object key resolves outside the configured skills directory")
    return local


def sync_skills() -> list[str]:
    """Download complete skill packages while preserving their S3 hierarchy."""
    if not skills_enabled():
        return []
    LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    client = boto3.client(
        "s3", region_name=os.environ.get("AWS_DEFAULT_REGION", "ap-southeast-1")
    )
    paths: list[str] = []
    downloaded_bytes = 0
    token = None
    try:
        while True:
            request = {"Bucket": BUCKET, "Prefix": PREFIX}
            if token:
                request["ContinuationToken"] = token
            response = client.list_objects_v2(**request)
            for item in response.get("Contents", []):
                key = str(item.get("Key") or "")
                if key.endswith("/"):
                    continue
                try:
                    size = int(item.get("Size", 0))
                    if size < 0:
                        raise ValueError("object size is negative")
                    local = _local_path_for_key(key)
                except (TypeError, ValueError) as error:
                    logger.warning("Skipping unsafe skill object %s: %s", key, error)
                    continue
                if size > MAX_OBJECT_BYTES:
                    logger.warning(
                        "Skipping oversized skill object %s (%d > %d bytes)",
                        key,
                        size,
                        MAX_OBJECT_BYTES,
                    )
                    continue
                if downloaded_bytes + size > MAX_SYNC_BYTES:
                    logger.warning(
                        "Skipping skill object %s because the %d-byte sync limit would be exceeded",
                        key,
                        MAX_SYNC_BYTES,
                    )
                    continue
                local.parent.mkdir(parents=True, exist_ok=True)
                try:
                    client.download_file(BUCKET, key, str(local))
                    actual_size = local.stat().st_size
                except Exception as error:
                    logger.warning("Unable to download skill object %s: %s", key, error)
                    continue
                if (
                    actual_size > MAX_OBJECT_BYTES
                    or downloaded_bytes + actual_size > MAX_SYNC_BYTES
                ):
                    try:
                        local.unlink()
                    except OSError:
                        logger.warning(
                            "Unable to remove downloaded skill object that exceeded a size limit: %s",
                            local,
                        )
                    logger.warning(
                        "Discarded skill object %s after its downloaded size exceeded a limit",
                        key,
                    )
                    continue
                downloaded_bytes += actual_size
                paths.append(str(local))
            if not response.get("IsTruncated"):
                break
            token = response.get("NextContinuationToken")
    except Exception as error:
        logger.warning("Skills sync from s3://%s/%s failed: %s", BUCKET, PREFIX, error)
    logger.info(
        "Skills sync complete: %d files, %d bytes", len(paths), downloaded_bytes
    )
    return paths


def _resolve_resource(skill_name: str, resource_path: str) -> Path:
    """Resolve one resource while preventing access outside the skill root."""
    requested = Path(resource_path)
    root = LOCAL_DIR.resolve()
    if not skill_name or Path(skill_name).name != skill_name:
        raise ValueError("Skill name must be one directory name")
    if requested.is_absolute():
        raise ValueError("Skill resource path must be relative to its skill directory")
    skill_root = (root / skill_name).resolve()
    if skill_root.parent != root:
        raise ValueError(
            "Skill directory must stay inside the configured skills directory"
        )
    candidate = (skill_root / requested).resolve()
    if candidate == skill_root or skill_root not in candidate.parents:
        raise ValueError(
            "Skill resource path must stay inside its activated skill directory"
        )
    if not candidate.is_file():
        raise FileNotFoundError(f"Skill resource does not exist: {resource_path}")
    return candidate


def skill_resource_s3_uri(skill_name: str, resource_path: str) -> str:
    """Return the canonical S3 URI for a resource present in the synced skill."""
    if not skills_enabled():
        raise ValueError("SKILLS_BUCKET must be configured to stage skill resources")
    local = _resolve_resource(skill_name, resource_path)
    relative = local.relative_to(LOCAL_DIR.resolve()).as_posix()
    return f"s3://{BUCKET}/{PREFIX}{relative}"


@tool(
    name="read_skill_resource",
    description=(
        "Read a UTF-8 text resource belonging to an activated Agent Skill, including "
        "Markdown, JSON, CSV, SQL, or source code. Pass a path relative to that "
        "skill's directory, such as skill_name="
        "'hospital-data-analyst-nuh' and resource_path='references/emd.md'. Use this "
        "when an activated skill requires a text resource before using an operational "
        "tool. Binary resources need a compatible binary or file-processing tool."
    ),
)
def read_skill_resource(skill_name: str, resource_path: str) -> str:
    """Return a bounded UTF-8 text resource from the local skill cache."""
    try:
        path = _resolve_resource(skill_name, resource_path)
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError, ValueError) as error:
        return f"Unable to read skill resource: {error}"
    if len(content) > MAX_RESOURCE_CHARS:
        return content[:MAX_RESOURCE_CHARS] + "\n\n[skill resource truncated]"
    return content

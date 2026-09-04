"""Sync optional Claude Agent Skills from S3 at container startup."""

import logging
import os
from pathlib import Path

import boto3


logger = logging.getLogger(__name__)

BUCKET = os.environ.get("SKILLS_BUCKET", "").strip()
PREFIX = os.environ.get("SKILLS_PREFIX", "").strip()
if PREFIX and not PREFIX.endswith("/"):
    PREFIX += "/"

# Claude discovers project skills beneath <cwd>/.claude/skills. Keep this path
# paired with agent.py's cwd="/app" rather than making it independently movable.
LOCAL_DIR = Path("/app/.claude/skills")
MAX_OBJECT_BYTES = max(
    1_000, int(os.environ.get("SKILLS_MAX_OBJECT_BYTES", "50000000"))
)
MAX_SYNC_BYTES = max(
    MAX_OBJECT_BYTES, int(os.environ.get("SKILLS_MAX_SYNC_BYTES", "250000000"))
)


def skills_enabled() -> bool:
    """Return whether an S3 skills bucket is configured."""
    return bool(BUCKET)


def _local_path_for_key(key: str) -> Path:
    """Map an S3 key safely beneath Claude's project skill directory."""
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
        logger.info("Skills disabled: SKILLS_BUCKET is empty")
        return []

    LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    region = (
        os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or "ap-southeast-1"
    )
    client = boto3.client("s3", region_name=region)
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
                logger.info("Loaded skill resource: %s (%d bytes)", key, actual_size)

            if not response.get("IsTruncated"):
                break
            token = response.get("NextContinuationToken")
            if not token:
                logger.warning("Skills sync stopped: S3 response omitted continuation token")
                break
    except Exception as error:
        logger.warning("Skills sync from s3://%s/%s failed: %s", BUCKET, PREFIX, error)

    logger.info(
        "Skills sync complete: %d files, %d bytes in %s",
        len(paths),
        downloaded_bytes,
        LOCAL_DIR,
    )
    return paths

"""
Sync agent skills from S3 into /app/.claude/skills/ at container startup.

Bucket: s3://ah-data-analytics/skills/
The hierarchy below the prefix is preserved so structured skills can include
SKILL.md entry points and references.
"""

import logging
import os

import boto3

logger = logging.getLogger(__name__)

BUCKET = "ah-data-analytics"
PREFIX = "skills/"
LOCAL_DIR = "/app/.claude/skills"


def sync_skills() -> list[str]:
    """Download all skill files from S3. Returns list of local paths."""
    os.makedirs(LOCAL_DIR, exist_ok=True)
    s3 = boto3.client("s3", region_name="ap-southeast-1")

    paths: list[str] = []
    try:
        resp = s3.list_objects_v2(Bucket=BUCKET, Prefix=PREFIX)
    except Exception as e:
        logger.warning("Skills sync: cannot list s3://%s/%s — %s", BUCKET, PREFIX, e)
        return []

    for obj in resp.get("Contents", []):
        key = obj["Key"]
        if key.endswith("/") or not key.endswith(".md"):
            continue
        relative = key[len(PREFIX):]
        local = os.path.join(LOCAL_DIR, *relative.split("/"))
        os.makedirs(os.path.dirname(local), exist_ok=True)
        try:
            s3.download_file(BUCKET, key, local)
            paths.append(local)
            logger.info("Loaded skill: %s (%d bytes)", relative, obj["Size"])
        except Exception as e:
            logger.warning("Skills sync: failed to download %s — %s", key, e)

    logger.info("Skills sync complete: %d files in %s", len(paths), LOCAL_DIR)
    return paths

"""Load an optional base system prompt from S3."""

import logging
import os
from functools import lru_cache
from urllib.parse import unquote, urlsplit

import boto3


logger = logging.getLogger(__name__)

ENV_NAME = "BASE_SYSTEM_PROMPT"
DEFAULT_MAX_BYTES = 200_000

def _positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error
    if value < 1:
        raise ValueError(f"{name} must be greater than zero")
    return value


def _parse_s3_uri(uri: str) -> tuple[str, str]:
    parsed = urlsplit(uri)
    key = unquote(parsed.path.lstrip("/"))
    if (
        parsed.scheme.lower() != "s3"
        or not parsed.netloc
        or not key
        or parsed.username
        or parsed.password
        or parsed.port is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            f"{ENV_NAME} must be an S3 object URI such as "
            "s3://my-bucket/prompts/system.txt"
        )
    return parsed.netloc, key


@lru_cache(maxsize=1)
def load() -> str:
    """Return the configured UTF-8 S3 prompt, cached for this warm container."""
    uri = os.environ.get(ENV_NAME, "").strip()
    if not uri:
        logger.info("%s is unset; no base system prompt will be added", ENV_NAME)
        return ""

    bucket, key = _parse_s3_uri(uri)
    max_bytes = _positive_int("BASE_SYSTEM_PROMPT_MAX_BYTES", DEFAULT_MAX_BYTES)
    region = (
        os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or "ap-southeast-1"
    )

    try:
        response = boto3.client("s3", region_name=region).get_object(
            Bucket=bucket,
            Key=key,
        )
        content = response["Body"].read(max_bytes + 1)
    except Exception as error:
        raise RuntimeError(f"Unable to load {ENV_NAME} from {uri}: {error}") from error

    if len(content) > max_bytes:
        raise ValueError(
            f"{ENV_NAME} object exceeds BASE_SYSTEM_PROMPT_MAX_BYTES={max_bytes}"
        )
    try:
        prompt = content.decode("utf-8-sig").strip()
    except UnicodeDecodeError as error:
        raise ValueError(f"{ENV_NAME} object must contain UTF-8 text") from error
    if not prompt:
        raise ValueError(f"{ENV_NAME} object is empty")

    logger.info("Loaded base system prompt from %s (%d characters)", uri, len(prompt))
    return prompt

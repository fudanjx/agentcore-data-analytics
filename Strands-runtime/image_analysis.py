"""Request-scoped encrypted image analysis with Amazon Bedrock vision."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import boto3
from botocore.config import Config
from strands import tool

import document_encryption


logger = logging.getLogger(__name__)
VISION_MODEL_ID = (
    os.environ.get("VISION_MODEL_ID", "").strip()
    or os.environ.get("MODEL_ARN", "").strip()
    or os.environ.get("MODEL_ID", "").strip()
)
VISION_REGION = (
    os.environ.get("VISION_MODEL_REGION", "").strip()
    or os.environ.get("MODEL_REGION", "").strip()
    or os.environ.get("AWS_DEFAULT_REGION", "ap-southeast-1").strip()
)
VISION_MAX_IMAGE_BYTES = min(
    3_750_000,
    max(1_024, int(os.environ.get("VISION_MAX_IMAGE_BYTES", "3750000"))),
)
VISION_MAX_OUTPUT_TOKENS = min(
    8_192,
    max(256, int(os.environ.get("VISION_MAX_OUTPUT_TOKENS", "2048"))),
)
VISION_MAX_RESULT_CHARS = min(
    100_000,
    max(1_000, int(os.environ.get("VISION_MAX_RESULT_CHARS", "20000"))),
)

_s3_client = None
_bedrock_client = None


def enabled() -> bool:
    return bool(VISION_MODEL_ID)


def get_s3_client():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client(
            "s3",
            region_name=VISION_REGION,
            config=Config(
                connect_timeout=10,
                read_timeout=60,
                retries={"mode": "standard", "max_attempts": 2},
            ),
        )
    return _s3_client


def get_bedrock_client():
    global _bedrock_client
    if _bedrock_client is None:
        _bedrock_client = boto3.client(
            "bedrock-runtime",
            region_name=VISION_REGION,
            config=Config(
                connect_timeout=10,
                read_timeout=300,
                retries={"mode": "standard", "max_attempts": 2},
            ),
        )
    return _bedrock_client


def _read_bounded_body(body: Any, declared_size: Any) -> bytes:
    if isinstance(declared_size, int) and declared_size > VISION_MAX_IMAGE_BYTES:
        raise ValueError("Image exceeds the configured vision size limit")
    try:
        raw = body.read(VISION_MAX_IMAGE_BYTES + 1)
    finally:
        close = getattr(body, "close", None)
        if callable(close):
            close()
    if not raw:
        raise ValueError("Image input is empty")
    if len(raw) > VISION_MAX_IMAGE_BYTES:
        raise ValueError("Image exceeds the configured vision size limit")
    return raw


def _download_s3(source_url: str) -> bytes:
    parsed = urlparse(source_url)
    if parsed.scheme != "s3" or not parsed.netloc or not parsed.path.lstrip("/"):
        raise ValueError("Image input must use a valid s3:// URL")
    response = get_s3_client().get_object(
        Bucket=parsed.netloc,
        Key=parsed.path.lstrip("/"),
    )
    return _read_bounded_body(response["Body"], response.get("ContentLength"))


def _image_format(raw: bytes, filename: str) -> str:
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if raw.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "webp"
    extension = Path(filename).suffix.lower()
    if extension in {".jpg", ".jpeg"}:
        expected = "JPEG"
    elif extension in {".png", ".gif", ".webp"}:
        expected = extension[1:].upper()
    else:
        expected = "supported"
    raise ValueError(f"Image bytes are not a valid {expected} image")


def _vision_prompt(filename: str, analysis_request: str) -> str:
    request = " ".join(str(analysis_request or "").split())[:4_000]
    if not request:
        request = "Describe the image and extract information relevant to the user request."
    return (
        f"Analyze the uploaded image {Path(filename).name!r}. {request}\n\n"
        "Report only what is supported by the image. Describe important visible "
        "content, text, labels, tables, charts, trends, anomalies, and uncertainty "
        "as applicable. Do not infer hidden or unreadable details."
    )


def _analyze_bytes(raw: bytes, filename: str, analysis_request: str) -> str:
    image_format = _image_format(raw, filename)
    response = get_bedrock_client().converse(
        modelId=VISION_MODEL_ID,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "image": {
                            "format": image_format,
                            "source": {"bytes": raw},
                        }
                    },
                    {"text": _vision_prompt(filename, analysis_request)},
                ],
            }
        ],
        inferenceConfig={
            "maxTokens": VISION_MAX_OUTPUT_TOKENS,
            "temperature": 0,
        },
    )
    blocks = response.get("output", {}).get("message", {}).get("content", [])
    text = "\n".join(
        block["text"].strip()
        for block in blocks
        if isinstance(block, dict)
        and isinstance(block.get("text"), str)
        and block["text"].strip()
    )
    if not text:
        raise RuntimeError("Vision model returned no text analysis")
    if len(text) > VISION_MAX_RESULT_CHARS:
        text = text[:VISION_MAX_RESULT_CHARS] + "\n[vision result truncated]"
    return text


def _analyze_image(
    image: dict[str, str],
    encryption: dict[str, str] | None,
    analysis_request: str,
) -> str:
    raw = _download_s3(image["source_url"])
    encrypted = encryption is not None
    if encryption is not None:
        key = document_encryption.unwrap_key(encryption["wrapped_key"])
        raw = document_encryption.decrypt_envelope(raw, key)
    analysis = _analyze_bytes(
        raw,
        (encryption or {}).get("original_name", image["stored_name"]),
        analysis_request,
    )
    return json.dumps(
        {
            "ok": True,
            "filename": (encryption or {}).get(
                "original_name", image["stored_name"]
            ),
            "encrypted_input": encrypted,
            "analysis": analysis,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def build_tools(
    image_inputs: list[dict[str, str]],
    encrypted_documents: list[dict[str, str]] | None = None,
) -> list:
    """Create a vision tool restricted to images declared by this request."""
    if not image_inputs:
        return []
    if not enabled():
        raise RuntimeError(
            "Image inputs were supplied, but VISION_MODEL_ID is not configured"
        )
    images_by_name = {item["stored_name"]: item for item in image_inputs}
    encryption_by_name = {
        item["stored_name"]: item for item in (encrypted_documents or [])
    }

    @tool(
        name="analyze_uploaded_image",
        description=(
            "Analyze one request-provided <IMAGE_INPUT> with a vision model. Pass "
            "its exact stored filename and a concise description of what to inspect. "
            "The tool securely downloads the request-bound S3 object and automatically "
            "decrypts a matching CLARA encrypted image before vision analysis. Do not "
            "use Code Interpreter, execute_command, or Python image libraries first."
        ),
    )
    async def analyze_uploaded_image(
        stored_filename: str,
        analysis_request: str = "Describe and analyze this image",
    ) -> str:
        image = images_by_name.get(stored_filename)
        if image is None:
            return json.dumps(
                {
                    "ok": False,
                    "error": "No image input exists for that stored filename",
                },
                separators=(",", ":"),
            )
        started_at = time.perf_counter()
        try:
            result = await asyncio.to_thread(
                _analyze_image,
                image,
                encryption_by_name.get(stored_filename),
                analysis_request,
            )
        except Exception as error:
            logger.exception("Uploaded image analysis failed")
            result = json.dumps(
                {"ok": False, "error": f"Image analysis failed: {error}"},
                separators=(",", ":"),
            )
        logger.info(
            "VISION_RESULT %s",
            json.dumps(
                {
                    "event": "vision_result",
                    "encrypted_input": stored_filename in encryption_by_name,
                    "result_chars": len(result),
                    "duration_ms": round(
                        (time.perf_counter() - started_at) * 1000
                    ),
                },
                separators=(",", ":"),
            ),
        )
        return result

    return [analyze_uploaded_image]

"""Validate and remove CLARA's wrapped-key message carrier."""

import base64
import json
import re


_CONTEXT_RE = re.compile(
    r"<encrypted_document_context>\s*([A-Za-z0-9_-]*)\s*</encrypted_document_context>",
    re.IGNORECASE,
)


def decode_context(encoded: str) -> list[dict[str, str]]:
    if len(encoded) > 100_000:
        raise ValueError("Encrypted document context is too large")
    try:
        padding = "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded + padding))
    except Exception as error:
        raise ValueError("Encrypted document context is invalid") from error
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ValueError("Encrypted document context has an unsupported version")
    files = payload.get("files")
    if not isinstance(files, list) or len(files) > 30:
        raise ValueError("Encrypted document context must contain at most 30 files")

    required = {
        "content_encryption": "AES-256-GCM",
        "key_wrap": "RSA-OAEP-256",
        "envelope": "CLARAENC1",
    }
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in files:
        if not isinstance(item, dict) or any(
            item.get(key) != value for key, value in required.items()
        ):
            raise ValueError("Encrypted document metadata is invalid")
        values = {
            key: item.get(key)
            for key in ("stored_name", "original_name", "wrapped_key", "key_id")
        }
        if any(not isinstance(value, str) or not value for value in values.values()):
            raise ValueError("Encrypted document metadata is incomplete")
        entry = {**required, **values}
        stored_name = entry["stored_name"]
        if stored_name in seen:
            raise ValueError("Encrypted document stored names must be unique")
        seen.add(stored_name)
        normalized.append(entry)
    return normalized


def extract_from_messages(
    messages: list[dict],
) -> tuple[list[dict], list[dict[str, str]]]:
    """Return carrier-free messages plus validated request-scoped metadata."""
    contexts: list[dict[str, str]] = []
    cleaned: list[dict] = []
    for message in messages:
        item = dict(message)
        content = item.get("content")
        if isinstance(content, str):

            def replace(match: re.Match) -> str:
                if match.group(1):
                    contexts.extend(decode_context(match.group(1)))
                return ""

            item["content"] = _CONTEXT_RE.sub(replace, content)
        cleaned.append(item)
    if len({entry["stored_name"] for entry in contexts}) != len(contexts):
        raise ValueError("Encrypted document contexts contain duplicate stored names")
    return cleaned, contexts

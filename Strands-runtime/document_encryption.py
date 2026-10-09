"""Validate and remove CLARA's wrapped-key message carrier."""

import base64
import json
import os
import re


_CONTEXT_RE = re.compile(
    r"<encrypted_document_context>\s*([A-Za-z0-9_-]*)\s*</encrypted_document_context>",
    re.IGNORECASE,
)


def decryption_enabled() -> bool:
    """Return whether the Runtime has the CLARA envelope private key."""
    return bool(os.environ.get("CLARA_FILE_DECRYPTION_PRIVATE_KEY", "").strip())


def unwrap_key(wrapped_key: str) -> bytes:
    """Unwrap one request-scoped AES key without exposing it to the model."""
    private_key_pem = (
        os.environ.get("CLARA_FILE_DECRYPTION_PRIVATE_KEY", "")
        .replace("\\n", "\n")
        .strip()
    )
    if not private_key_pem:
        raise RuntimeError("CLARA_FILE_DECRYPTION_PRIVATE_KEY is not configured")
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding

        private_key = serialization.load_pem_private_key(
            private_key_pem.encode("utf-8"), password=None
        )
        data_key = private_key.decrypt(
            base64.b64decode(wrapped_key, validate=True),
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            ),
        )
    except Exception as error:
        raise RuntimeError("Unable to unwrap the encrypted document key") from error
    if len(data_key) != 32:
        raise RuntimeError("Unwrapped document key has an invalid length")
    return data_key


def decrypt_envelope(raw: bytes, data_key: bytes) -> bytes:
    """Decrypt and authenticate one CLARAENC1 byte envelope in memory."""
    if len(raw) < 37 or raw[:8] != b"CLARAENC" or raw[8] != 1:
        raise ValueError("Invalid encrypted document envelope")
    header, nonce = raw[:21], raw[9:21]
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        return AESGCM(data_key).decrypt(nonce, raw[21:], header)
    except Exception as error:
        raise ValueError("Unable to decrypt encrypted document") from error


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

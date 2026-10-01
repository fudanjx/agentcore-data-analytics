"""Request-scoped AgentCore Code Interpreter tools for Strands."""

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import shlex
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import boto3
import code_interpreter_result
from botocore.config import Config
from strands import tool


logger = logging.getLogger(__name__)
REGION = os.environ.get(
    "CODE_INTERPRETER_REGION",
    os.environ.get("AWS_DEFAULT_REGION", "ap-southeast-1"),
)
CODE_INTERPRETER_ID = os.environ.get("CODE_INTERPRETER_ID", "").strip()
SESSION_TIMEOUT_SECONDS = min(
    28_800,
    max(60, int(os.environ.get("CODE_INTERPRETER_SESSION_TIMEOUT_SECONDS", "1800"))),
)
MAX_RESULT_CHARS = max(
    1_000, int(os.environ.get("CODE_INTERPRETER_MAX_RESULT_CHARS", "200000"))
)
RESULT_MODE = os.environ.get("CODE_INTERPRETER_RESULT_MODE", "semantic").strip().lower()
if RESULT_MODE not in {"semantic", "legacy"}:
    raise ValueError("CODE_INTERPRETER_RESULT_MODE must be 'semantic' or 'legacy'")
SEMANTIC_MAX_RESULT_CHARS = min(
    20_000,
    max(2_000, int(os.environ.get("CODE_INTERPRETER_SEMANTIC_MAX_CHARS", "10000"))),
)
_client = None


SEMANTIC_RESULT_GUIDANCE = """
## Code Interpreter result contract

When you use Code Interpreter, keep bulk data, full logs, and generated file
contents inside the sandbox or S3. Do not print full dataframes, raw SQL
results, broad recursive listings, or long logs. Aggregate and calculate in
the sandbox instead.

For every successful code or shell task, print one final single-line marker:
`AGENTCORE_RESULT_JSON=<JSON object>`. The JSON object must include boolean
`ok` and a concise `summary`. It may include `row_count`, up to 20 `columns`,
up to 20 scalar `metrics`, up to 30 `sample_rows` (each with up to 20 scalar
fields), up to 20 `artifacts` (`s3_uri`, `filename`, `content_type`), and
`warnings`. For failures, print the same marker with `ok: false`, a concise
summary, and an actionable `error`. Put complete results in an artifact and
return its metadata rather than embedding file content in the result.
""".strip()


def system_guidance() -> str:
    """Return stable semantic-result instructions for the model prompt."""
    return SEMANTIC_RESULT_GUIDANCE if RESULT_MODE == "semantic" else ""


def get_client():
    global _client
    if _client is None:
        _client = boto3.client(
            "bedrock-agentcore",
            region_name=REGION,
            config=Config(
                read_timeout=15 * 60,
                connect_timeout=10,
                retries={"mode": "standard", "max_attempts": 2},
            ),
        )
    return _client


def _require_identifier() -> str:
    if not CODE_INTERPRETER_ID:
        raise RuntimeError("CODE_INTERPRETER_ID must be configured")
    return CODE_INTERPRETER_ID


def _session_name(runtime_session_id: str | None) -> str:
    suffix = (runtime_session_id or "request").strip() or "request"
    return f"runtime-{suffix}"[:100]


def start_session(runtime_session_id: str | None) -> str:
    response = get_client().start_code_interpreter_session(
        codeInterpreterIdentifier=_require_identifier(),
        name=_session_name(runtime_session_id),
        sessionTimeoutSeconds=SESSION_TIMEOUT_SECONDS,
    )
    session_id = response.get("sessionId")
    if not isinstance(session_id, str) or not session_id:
        raise RuntimeError("Code Interpreter did not return a session ID")
    logger.info("Code Interpreter session started: %s", session_id)
    return session_id


def stop_session(session_id: str) -> None:
    try:
        get_client().stop_code_interpreter_session(
            codeInterpreterIdentifier=_require_identifier(), sessionId=session_id
        )
    except Exception:
        logger.warning(
            "Unable to stop Code Interpreter session %s", session_id, exc_info=True
        )


def _invoke_and_collect(session_id: str, name: str, arguments: dict) -> str:
    response = get_client().invoke_code_interpreter(
        codeInterpreterIdentifier=_require_identifier(),
        sessionId=session_id,
        name=name,
        arguments=arguments,
    )
    if RESULT_MODE == "legacy":
        return code_interpreter_result.render_legacy_events(
            response["stream"], MAX_RESULT_CHARS
        )
    return code_interpreter_result.render_semantic_events(
        response["stream"], SEMANTIC_MAX_RESULT_CHARS
    )


def _log_result(name: str, rendered: str, duration_ms: int) -> None:
    """Log model-facing result shape without emitting customer data."""
    max_chars = (
        MAX_RESULT_CHARS if RESULT_MODE == "legacy" else SEMANTIC_MAX_RESULT_CHARS
    )
    payload = code_interpreter_result.result_metadata(
        rendered,
        mode=RESULT_MODE,
        max_chars=max_chars,
    )
    payload.update({"tool": name, "duration_ms": duration_ms})
    logger.info(
        "CODE_INTERPRETER_RESULT %s",
        json.dumps(payload, separators=(",", ":")),
    )


async def _invoke_tool(session_id: str, name: str, arguments: dict) -> str:
    started_at = time.perf_counter()
    try:
        rendered = await asyncio.to_thread(
            _invoke_and_collect, session_id, name, arguments
        )
    except Exception as error:
        logger.exception("Code Interpreter tool failed: %s", name)
        if RESULT_MODE == "semantic":
            rendered = code_interpreter_result.render_runtime_error(
                error, SEMANTIC_MAX_RESULT_CHARS
            )
        else:
            rendered = f"Code Interpreter {name} failed: {error}"
    _log_result(
        name,
        rendered,
        round((time.perf_counter() - started_at) * 1000),
    )
    return rendered


def _tool_result_is_error(rendered: str) -> bool:
    """Return whether a rendered AgentCore tool response reports a failure."""
    return code_interpreter_result.result_is_error(rendered)


def _skill_resource_destination(skill_name: str, resource_path: str) -> str:
    """Build a safe collision-resistant destination in the interpreter sandbox."""
    digest = hashlib.sha256(
        f"{skill_name}\0{resource_path}".encode("utf-8")
    ).hexdigest()[:12]
    filename = re.sub(r"[^A-Za-z0-9._-]", "_", Path(resource_path).name)[:100]
    return f"/tmp/skill-resource-{digest}-{filename or 'resource'}"


def _zip_member_extract_code(archive_path: str, member_path: str, destination: str) -> str:
    """Build Python code with JSON-quoted paths for one bounded ZIP member."""
    return (
        "import os, zipfile\n"
        f"archive_path = {json.dumps(archive_path)}\n"
        f"member_path = {json.dumps(member_path)}\n"
        f"destination = {json.dumps(destination)}\n"
        "try:\n"
        "    with zipfile.ZipFile(archive_path) as archive, archive.open(member_path) as source, open(destination, 'wb') as target:\n"
        "        copied = 0\n"
        "        while chunk := source.read(1024 * 1024):\n"
        "            copied += len(chunk)\n"
        "            if copied > 52_428_800:\n"
        "                raise ValueError('skill ZIP member exceeds 50 MiB')\n"
        "            target.write(chunk)\n"
        "    print('Skill resource extracted')\n"
        "finally:\n"
        "    if os.path.exists(archive_path):\n"
        "        os.remove(archive_path)\n"
    )


def _unwrap_document_key(wrapped_key: str) -> bytes:
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


def document_decryption_enabled() -> bool:
    """Return whether this Runtime is configured to decrypt CLARA documents."""
    return bool(os.environ.get("CLARA_FILE_DECRYPTION_PRIVATE_KEY", "").strip())


def _document_destination(stored_name: str, original_name: str) -> tuple[str, str]:
    digest = hashlib.sha256(stored_name.encode("utf-8")).hexdigest()[:12]
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", Path(original_name).name)[:160]
    plaintext = f"/tmp/clara-{digest}-{safe_name or 'document.bin'}"
    return f"{plaintext}.encrypted", plaintext


def _document_download_command(source_url: str, destination: str) -> str:
    parsed = urlparse(source_url)
    if parsed.scheme == "s3" and parsed.netloc and parsed.path:
        return f"aws s3 cp --only-show-errors {shlex.quote(source_url)} {shlex.quote(destination)}"
    if parsed.scheme == "https" and parsed.netloc:
        return (
            "curl --fail --location --silent --show-error "
            f"--output {shlex.quote(destination)} {shlex.quote(source_url)}"
        )
    raise ValueError("Encrypted document URL must use s3:// or https://")


def _document_decryption_code(
    encrypted_path: str, plaintext_path: str, data_key: bytes
) -> str:
    encoded_key = base64.b64encode(data_key).decode("ascii")
    return (
        "import base64, json, os\n"
        "from cryptography.hazmat.primitives.ciphers.aead import AESGCM\n"
        f"source = {json.dumps(encrypted_path)}\n"
        f"destination = {json.dumps(plaintext_path)}\n"
        f"key = base64.b64decode({json.dumps(encoded_key)})\n"
        "try:\n"
        "    raw = open(source, 'rb').read()\n"
        "    if len(raw) < 37 or raw[:8] != b'CLARAENC' or raw[8] != 1:\n"
        "        raise ValueError('invalid encrypted document envelope')\n"
        "    header, nonce = raw[:21], raw[9:21]\n"
        "    plaintext = AESGCM(key).decrypt(nonce, raw[21:], header)\n"
        "    with open(destination, 'xb') as output:\n"
        "        output.write(plaintext)\n"
        "    print('AGENTCORE_RESULT_JSON=' + json.dumps({'ok': True, 'summary': 'Encrypted document staged at ' + destination}))\n"
        "finally:\n"
        "    if os.path.exists(source):\n"
        "        os.remove(source)\n"
    )


def build_tools(
    session_id: str,
    skill_resource_uri: Callable[[str, str], str | tuple[str, str | None]] | None = None,
    encrypted_documents: list[dict[str, str]] | None = None,
) -> list:
    """Create Strands tools bound to one managed interpreter session."""

    @tool(
        name="execute_code",
        description=(
            "Execute code in managed AgentCore Code Interpreter. Use Python for "
            "uploaded-file analysis, calculations, transformation, statistics, "
            "forecasting, machine learning, and chart generation. Return concise "
            "aggregates and representative samples, not full datasets. End with "
            "one AGENTCORE_RESULT_JSON marker containing ok, summary, and only "
            "bounded optional metrics, sample rows, warnings, or artifact metadata."
        ),
    )
    async def execute_code(code: str, language: str = "python") -> str:
        normalized = language.lower()
        if normalized not in {"python", "javascript", "typescript"}:
            return f"Unsupported Code Interpreter language: {normalized}"
        return await _invoke_tool(
            session_id, "executeCode", {"code": code, "language": normalized}
        )

    @tool(
        name="execute_command",
        description=(
            "Execute a shell command in managed AgentCore Code Interpreter. Use this "
            "to download a request-provided S3 URI, inspect files, or upload a "
            "generated artifact. Operate only on paths and S3 URIs from this request. "
            "Avoid broad listings and long logs. End with one AGENTCORE_RESULT_JSON "
            "marker containing a concise result, errors, and artifact metadata."
        ),
    )
    async def execute_command(command: str) -> str:
        return await _invoke_tool(session_id, "executeCommand", {"command": command})

    tools = [execute_code, execute_command]
    encrypted_by_name = (
        {item["stored_name"]: item for item in (encrypted_documents or [])}
        if document_decryption_enabled()
        else {}
    )
    if encrypted_by_name:

        @tool(
            name="stage_encrypted_document",
            description=(
                "Download and decrypt one request-provided encrypted document into "
                "this request's managed Code Interpreter session. Pass the exact "
                "stored filename and URL from its document_input tag. The tool returns "
                "the plaintext sandbox path; encryption keys are never model-visible."
            ),
        )
        async def stage_encrypted_document(stored_filename: str, source_url: str) -> str:
            metadata = encrypted_by_name.get(stored_filename)
            if metadata is None:
                return "No encryption metadata exists for that stored filename"
            encrypted_path, plaintext_path = _document_destination(
                stored_filename, metadata["original_name"]
            )
            try:
                command = _document_download_command(source_url, encrypted_path)
                data_key = _unwrap_document_key(metadata["wrapped_key"])
            except (RuntimeError, ValueError) as error:
                return f"Unable to stage encrypted document: {error}"
            downloaded = await _invoke_tool(
                session_id, "executeCommand", {"command": command}
            )
            if _tool_result_is_error(downloaded):
                return f"Unable to download encrypted document: {downloaded}"
            decrypted = await _invoke_tool(
                session_id,
                "executeCode",
                {
                    "language": "python",
                    "code": _document_decryption_code(
                        encrypted_path, plaintext_path, data_key
                    ),
                },
            )
            if _tool_result_is_error(decrypted):
                return f"Unable to decrypt encrypted document: {decrypted}"
            return f"Encrypted document staged at {plaintext_path}"

        tools.append(stage_encrypted_document)

    if skill_resource_uri is not None:

        @tool(
            name="stage_skill_resource",
            description=(
                "Copy a resource from an activated Agent Skill into this request's "
                "managed Code Interpreter session. Provide the activated skill name "
                "and its relative resource path. The tool accepts only resources "
                "present in the synchronized skill package and returns the sandbox "
                "path to use with execute_code or execute_command."
            ),
        )
        async def stage_skill_resource(skill_name: str, resource_path: str) -> str:
            try:
                location = skill_resource_uri(skill_name, resource_path)
            except (OSError, ValueError) as error:
                return f"Unable to stage skill resource: {error}"
            uri, member_path = location if isinstance(location, tuple) else (location, None)
            destination = _skill_resource_destination(skill_name, resource_path)
            archive_path = f"{destination}.snapshot.zip" if member_path else destination
            command = (
                "aws s3 cp --only-show-errors "
                f"{shlex.quote(uri)} {shlex.quote(archive_path)}"
            )
            result = await _invoke_tool(
                session_id,
                "executeCommand",
                {"command": command},
            )
            if _tool_result_is_error(result):
                return f"Unable to stage skill resource from {uri}: {result}"
            if member_path:
                extraction = await _invoke_tool(
                    session_id, "executeCode",
                    {"language": "python", "code": _zip_member_extract_code(archive_path, member_path, destination)},
                )
                if _tool_result_is_error(extraction):
                    return f"Unable to extract skill resource from {uri}: {extraction}"
            return f"Skill resource staged at {destination}"

        tools.append(stage_skill_resource)

    return tools

"""Request-scoped AgentCore Code Interpreter tools for Strands."""

import asyncio
import json
import logging
import os
from typing import Any

import boto3
from botocore.config import Config
from strands import tool


logger = logging.getLogger(__name__)
REGION = os.environ.get(
    "CODE_INTERPRETER_REGION",
    os.environ.get("AWS_DEFAULT_REGION", "ap-southeast-1"),
)
CODE_INTERPRETER_ID = os.environ.get(
    "CODE_INTERPRETER_ID", "code_interpreter_runtime_dev-PEpoCecsBL"
).strip()
SESSION_TIMEOUT_SECONDS = min(
    28_800,
    max(60, int(os.environ.get("CODE_INTERPRETER_SESSION_TIMEOUT_SECONDS", "1800"))),
)
MAX_RESULT_CHARS = max(
    1_000, int(os.environ.get("CODE_INTERPRETER_MAX_RESULT_CHARS", "200000"))
)
_client = None


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
        logger.warning("Unable to stop Code Interpreter session %s", session_id, exc_info=True)


def _json_default(value: Any):
    if isinstance(value, (bytes, bytearray)):
        return {"binaryBytes": len(value)}
    return str(value)


def _invoke_and_collect(session_id: str, name: str, arguments: dict) -> str:
    response = get_client().invoke_code_interpreter(
        codeInterpreterIdentifier=_require_identifier(),
        sessionId=session_id,
        name=name,
        arguments=arguments,
    )
    events = [event.get("result", event) for event in response["stream"]]
    rendered = json.dumps(events, default=_json_default, separators=(",", ":"))
    if len(rendered) > MAX_RESULT_CHARS:
        rendered = rendered[:MAX_RESULT_CHARS] + "\n[tool result truncated]"
    return rendered


async def _invoke_tool(session_id: str, name: str, arguments: dict) -> str:
    try:
        return await asyncio.to_thread(_invoke_and_collect, session_id, name, arguments)
    except Exception as error:
        logger.exception("Code Interpreter tool failed: %s", name)
        return f"Code Interpreter {name} failed: {error}"


def build_tools(session_id: str) -> list:
    """Create Strands tools bound to one managed interpreter session."""

    @tool(
        name="execute_code",
        description=(
            "Execute code in managed AgentCore Code Interpreter. Use Python for "
            "uploaded-file analysis, calculations, transformation, statistics, "
            "forecasting, machine learning, and chart generation."
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
            "generated artifact. Operate only on paths and S3 URIs from this request."
        ),
    )
    async def execute_command(command: str) -> str:
        return await _invoke_tool(session_id, "executeCommand", {"command": command})

    return [execute_code, execute_command]

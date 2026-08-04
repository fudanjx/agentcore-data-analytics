"""AgentCore Code Interpreter tools for the Claude Agent SDK runtime."""

import asyncio
import json
import logging
import os
from typing import Any

import boto3
from botocore.config import Config
from claude_agent_sdk import create_sdk_mcp_server, tool


logger = logging.getLogger(__name__)

REGION = os.environ.get(
    "CODE_INTERPRETER_REGION",
    os.environ.get("AWS_DEFAULT_REGION", "ap-southeast-1"),
)
CODE_INTERPRETER_ID = os.environ.get(
    "CODE_INTERPRETER_ID",
    "code_interpreter_runtime_dev-PEpoCecsBL",
).strip()
SESSION_TIMEOUT_SECONDS = min(
    28_800,
    max(60, int(os.environ.get("CODE_INTERPRETER_SESSION_TIMEOUT_SECONDS", "1800"))),
)
MAX_RESULT_CHARS = max(
    1_000,
    int(os.environ.get("CODE_INTERPRETER_MAX_RESULT_CHARS", "200000")),
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


def _start_session(runtime_session_id: str | None) -> str:
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


async def start_session(runtime_session_id: str | None) -> str:
    return await asyncio.to_thread(_start_session, runtime_session_id)


def _stop_session(session_id: str) -> None:
    get_client().stop_code_interpreter_session(
        codeInterpreterIdentifier=_require_identifier(),
        sessionId=session_id,
    )
    logger.info("Code Interpreter session stopped: %s", session_id)


async def stop_session(session_id: str) -> None:
    try:
        await asyncio.to_thread(_stop_session, session_id)
    except Exception:
        logger.warning(
            "Unable to stop Code Interpreter session %s",
            session_id,
            exc_info=True,
        )


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
    events = []
    for event in response["stream"]:
        if "result" in event:
            events.append(event["result"])
        else:
            events.append(event)

    rendered = json.dumps(events, default=_json_default, separators=(",", ":"))
    if len(rendered) > MAX_RESULT_CHARS:
        rendered = rendered[:MAX_RESULT_CHARS] + "\n[tool result truncated]"
    return rendered


async def _invoke_tool(session_id: str, name: str, arguments: dict) -> dict:
    try:
        result = await asyncio.to_thread(
            _invoke_and_collect,
            session_id,
            name,
            arguments,
        )
        parsed = json.loads(result)
        is_error = any(
            isinstance(event, dict) and bool(event.get("isError"))
            for event in parsed
        )
    except Exception as error:
        logger.error(
            "Code Interpreter tool failed (session=%s tool=%s): %s",
            session_id,
            name,
            error,
        )
        result = f"Code Interpreter {name} failed: {error}"
        is_error = True
    return {
        "content": [{"type": "text", "text": result}],
        "is_error": is_error,
    }


def build_mcp_server(session_id: str):
    """Build tools bound to one active, request-scoped interpreter session."""

    @tool(
        "execute_code",
        "Execute code in the managed AgentCore Code Interpreter. Use Python for "
        "uploaded-file analysis, calculations, data transformation, statistics, "
        "forecasting, and chart generation. Files downloaded earlier in this "
        "Code Interpreter session remain available.",
        {"code": str, "language": str},
    )
    async def execute_code(arguments):
        language = str(arguments.get("language") or "python").lower()
        if language not in {"python", "javascript", "typescript"}:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"Unsupported Code Interpreter language: {language}",
                    }
                ],
                "is_error": True,
            }
        return await _invoke_tool(
            session_id,
            "executeCode",
            {"code": str(arguments.get("code") or ""), "language": language},
        )

    @tool(
        "execute_command",
        "Execute a shell command in the managed AgentCore Code Interpreter. Use "
        "this to download a user-provided S3 URI with `aws s3 cp`, inspect files, "
        "or upload a generated artifact. Only operate on paths and S3 URIs "
        "provided for the current request.",
        {"command": str},
    )
    async def execute_command(arguments):
        return await _invoke_tool(
            session_id,
            "executeCommand",
            {"command": str(arguments.get("command") or "")},
        )

    return create_sdk_mcp_server(
        name="code_interpreter",
        version="1.0.0",
        tools=[execute_code, execute_command],
    )

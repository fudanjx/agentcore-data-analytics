"""Request-scoped AgentCore Code Interpreter tools for Strands."""

import asyncio
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


def build_tools(
    session_id: str,
    skill_resource_uri: Callable[[str, str], str] | None = None,
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
                uri = skill_resource_uri(skill_name, resource_path)
            except (OSError, ValueError) as error:
                return f"Unable to stage skill resource: {error}"
            destination = _skill_resource_destination(skill_name, resource_path)
            command = (
                "aws s3 cp --only-show-errors "
                f"{shlex.quote(uri)} {shlex.quote(destination)}"
            )
            result = await _invoke_tool(
                session_id,
                "executeCommand",
                {"command": command},
            )
            if _tool_result_is_error(result):
                return f"Unable to stage skill resource from {uri}: {result}"
            return f"Skill resource staged at {destination}"

        tools.append(stage_skill_resource)

    return tools

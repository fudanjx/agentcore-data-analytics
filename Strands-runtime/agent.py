"""Strands data analyst with Gateway, Memory, skills, and Code Interpreter."""

import asyncio
import logging
import os
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, AsyncIterator

from strands import Agent, AgentSkills
from strands.handlers.callback_handler import null_callback_handler
from strands.models import BedrockModel, CacheConfig
from strands.tools.mcp import MCPClient

import code_interpreter
import gateway_proxy
import memory
import skills_sync
import system_prompt


logger = logging.getLogger(__name__)
MODEL_ID = os.environ.get(
    "MODEL_ID",
    os.environ.get(
        "MODEL_ARN",
        "arn:aws:bedrock:us-east-1:964340114883:application-inference-profile/ji5jakx5lho3",
    ),
).strip()
MODEL_REGION = os.environ.get("MODEL_REGION", "").strip()
ENABLE_GATEWAYS = os.environ.get("ENABLE_GATEWAYS", "true").lower() not in {"0", "false", "no"}
ENABLE_CODE_INTERPRETER = os.environ.get(
    "ENABLE_CODE_INTERPRETER", "true"
).lower() not in {"0", "false", "no"}


@dataclass(frozen=True)
class InvocationRequest:
    messages: list[dict]
    actor_id: str | None
    session_id: str
    model_slug: str
    stream: bool

    @classmethod
    def from_payload(cls, payload: dict, context: Any = None) -> "InvocationRequest":
        if not isinstance(payload, dict):
            raise ValueError("Invocation payload must be a JSON object")
        has_messages = payload.get("messages") is not None
        messages = payload.get("messages")
        if messages is None:
            prompt = payload.get("prompt") or payload.get("input") or payload.get("inputText")
            if prompt is None:
                raise ValueError("Provide 'messages', 'prompt', 'input', or 'inputText'")
            messages = [{"role": "user", "content": prompt}]
        if not isinstance(messages, list) or not messages:
            raise ValueError("'messages' must be a non-empty array")
        normalized = []
        for item in messages:
            if not isinstance(item, dict):
                raise ValueError("Every message must be a JSON object")
            normalized.append(
                {
                    "role": str(item.get("role") or "user").lower(),
                    "content": item.get("content", ""),
                }
            )

        context_session = getattr(context, "session_id", None) if context else None
        session_id = str(
            context_session
            or payload.get("session_id")
            or payload.get("sessionId")
            or payload.get("chat_id")
            or uuid.uuid4()
        )
        if len(session_id) < 33:
            session_id = session_id.ljust(33, "x")

        headers = getattr(context, "request_headers", None) or {}
        header_actor = next(
            (
                value
                for key, value in headers.items()
                if key.lower()
                in {
                    "x-amzn-bedrock-agentcore-runtime-custom-actor-id",
                    "x-amzn-bedrock-agentcore-runtime-custom-user-id",
                    "x-actor-id",
                    "x-user-id",
                }
            ),
            None,
        )
        model_info = payload.get("model_item") or {}
        if not isinstance(model_info, dict):
            model_info = {}
        nested_info = model_info.get("info") or {}
        if not isinstance(nested_info, dict):
            nested_info = {}
        actor = (
            header_actor
            or payload.get("actor_id")
            or payload.get("actorId")
            or payload.get("user_id")
            or payload.get("userId")
            or payload.get("runtimeUserId")
            or nested_info.get("user_id")
        )
        return cls(
            messages=normalized,
            actor_id=str(actor) if actor else None,
            session_id=session_id,
            model_slug=str(payload.get("model") or "strands-data-analyst"),
            # The existing Dify/OpenAI proxy expects Runtime message payloads
            # to stream even though it does not add a downstream `stream` flag.
            # Simple AgentCore console prompts remain blocking by default.
            stream=(
                _as_bool(payload["stream"])
                if "stream" in payload
                else has_messages
            ),
        )


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
                elif block.get("type") in {"image_url", "image"}:
                    parts.append("[image supplied by the caller]")
        return "\n".join(parts)
    if content is None:
        return ""
    return str(content)


def _split_system(messages: list[dict]) -> tuple[list[str], list[dict]]:
    systems = [_content_text(item["content"]) for item in messages if item["role"] == "system"]
    ordinary = [item for item in messages if item["role"] != "system"]
    return systems, ordinary


def _build_prompt(messages: list[dict]) -> str:
    if not messages:
        return ""
    if len(messages) == 1 and messages[0]["role"] == "user":
        return _content_text(messages[0]["content"])
    lines: list[str] = []
    for item in messages[:-1]:
        lines.append(f"{item['role'].upper()}: {_content_text(item['content'])}")
    last = messages[-1]
    lines.extend(["", f"Current {last['role']} message: {_content_text(last['content'])}"])
    return "\n".join(lines)


def _latest_user_text(messages: list[dict]) -> str:
    for item in reversed(messages):
        if item["role"] == "user":
            return _content_text(item["content"])
    return ""


def _memory_context(request: InvocationRequest, prompt: str) -> tuple[str, str]:
    if not request.actor_id:
        return "", ""
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="memory") as executor:
        short_future = executor.submit(
            memory.retrieve_short_term_context, request.actor_id, request.session_id
        )
        long_future = executor.submit(
            memory.retrieve_long_term_context, request.actor_id, prompt
        )
        return short_future.result(), long_future.result()


def _model_region() -> str:
    if MODEL_REGION:
        return MODEL_REGION
    match = re.match(r"^arn:[^:]+:bedrock:([^:]+):", MODEL_ID)
    return match.group(1) if match else os.environ.get("AWS_DEFAULT_REGION", "ap-southeast-1")


def _make_gateway_clients() -> list[MCPClient]:
    if not ENABLE_GATEWAYS:
        return []
    clients = []
    for slug, gateway in gateway_proxy.GATEWAY_CONFIGS.items():
        clients.append(
            MCPClient(
                lambda target=gateway: gateway_proxy.mcp_transport(target),
                startup_timeout=30,
                prefix=slug,
            )
        )
    return clients


def _prepare(request: InvocationRequest):
    system_messages, ordinary_messages = _split_system(request.messages)
    prompt = _build_prompt(ordinary_messages)
    if not prompt.strip():
        raise ValueError("The invocation does not contain a user question")
    short_context, long_context = _memory_context(request, prompt)
    if short_context:
        prompt = f"{short_context}\n\n---\n\n## Current request\n\n{prompt}"

    base_prompt = system_prompt.load()
    system_prompt_text = base_prompt + skills_sync.ACTIVATION_GUIDANCE + long_context
    if system_messages:
        system_prompt_text += (
            "\n\n---\n\n## Caller-provided system guidance\n\n"
            + "\n\n".join(system_messages)
        )

    interpreter_session = None
    try:
        tools: list = [skills_sync.read_skill_resource]
        if ENABLE_CODE_INTERPRETER and code_interpreter.CODE_INTERPRETER_ID:
            interpreter_session = code_interpreter.start_session(request.session_id)
            tools.extend(code_interpreter.build_tools(interpreter_session))
        tools.extend(_make_gateway_clients())

        model = BedrockModel(
            model_id=MODEL_ID,
            region_name=_model_region(),
            # The default model is an opaque inference-profile ARN, so Strands
            # cannot infer the provider when CacheConfig uses strategy="auto".
            cache_config=CacheConfig(strategy="anthropic"),
            cache_tools="default",
        )
        runtime_agent = Agent(
            model=model,
            tools=tools,
            plugins=[AgentSkills(skills=skills_sync.LOCAL_DIR)],
            system_prompt=system_prompt_text,
            callback_handler=null_callback_handler,
            name="data-analyst",
            description="Data analyst with connected databases and managed code execution",
        )
        return (
            runtime_agent,
            interpreter_session,
            prompt,
            _latest_user_text(ordinary_messages) or prompt,
        )
    except BaseException:
        if interpreter_session:
            code_interpreter.stop_session(interpreter_session)
        raise


def _result_text(result: Any) -> str:
    message = getattr(result, "message", None)
    if isinstance(message, dict):
        parts = []
        for block in message.get("content", []):
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        if parts:
            return "".join(parts)
    return str(result)


def _cleanup(runtime_agent: Agent | None, interpreter_session: str | None) -> None:
    if runtime_agent is not None:
        try:
            runtime_agent.cleanup()
        except Exception:
            logger.warning("Unable to clean up Strands tools", exc_info=True)
    if interpreter_session:
        code_interpreter.stop_session(interpreter_session)


def run(request: InvocationRequest) -> dict:
    """Run one isolated Strands invocation and return a blocking response."""
    runtime_agent = None
    interpreter_session = None
    try:
        runtime_agent, interpreter_session, prompt, current_user = _prepare(request)
        result = runtime_agent(prompt)
        text = _result_text(result)
        if request.actor_id:
            memory.save_turn(request.actor_id, request.session_id, current_user, text)
        return {
            "result": text,
            "session_id": request.session_id,
            "model": request.model_slug,
        }
    finally:
        _cleanup(runtime_agent, interpreter_session)


_STEP_UNSAFE = re.compile(r"[^A-Za-z0-9 ._:/()\-]")


def _safe_tool_name(value: Any) -> str:
    raw = " ".join(str(value or "Agent tool").split())
    raw = _STEP_UNSAFE.sub("", raw).strip()
    if "_" in raw and raw.split("_", 1)[0] in gateway_proxy.GATEWAY_CONFIGS:
        prefix, operation = raw.split("_", 1)
        raw = f"{gateway_proxy.mcp_label(prefix)}: {operation.replace('_', ' ')}"
    else:
        raw = raw.replace("_", " ")
    return (raw or "Agent tool")[:120]


async def stream(request: InvocationRequest) -> AsyncIterator[dict]:
    """Stream OpenAI-compatible chunks for the existing Dify proxy."""
    runtime_agent = None
    interpreter_session = None
    text_parts: list[str] = []
    active_steps: dict[str, str] = {}
    current_user = ""
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())
    try:
        runtime_agent, interpreter_session, prompt, current_user = await asyncio.to_thread(
            _prepare, request
        )
        async for event in runtime_agent.stream_async(prompt):
            data = event.get("data")
            if isinstance(data, str) and data:
                text_parts.append(data)
                yield {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": request.model_slug,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": data},
                            "finish_reason": None,
                        }
                    ],
                }

            tool_use = event.get("current_tool_use") or {}
            tool_id = str(tool_use.get("toolUseId") or "")
            tool_name = tool_use.get("name")
            if tool_id and tool_name and tool_id not in active_steps:
                safe_name = _safe_tool_name(tool_name)
                active_steps[tool_id] = safe_name
                yield {
                    "event": "agent_step",
                    "step": {"type": "tool", "name": safe_name, "status": "started"},
                }

        for safe_name in active_steps.values():
            yield {
                "event": "agent_step",
                "step": {"type": "tool", "name": safe_name, "status": "completed"},
            }
        text = "".join(text_parts)
        if request.actor_id:
            await asyncio.to_thread(
                memory.save_turn,
                request.actor_id,
                request.session_id,
                current_user,
                text,
            )
    except Exception as error:
        logger.exception("Strands streaming invocation failed")
        error_text = f"\n\n[error] {error}"
        text_parts.append(error_text)
        yield {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": request.model_slug,
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": error_text},
                    "finish_reason": None,
                }
            ],
        }
    finally:
        await asyncio.to_thread(_cleanup, runtime_agent, interpreter_session)

    yield {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": request.model_slug,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }

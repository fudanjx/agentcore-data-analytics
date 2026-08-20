"""Strands data analyst with Gateway, Memory, skills, and Code Interpreter."""

import asyncio
import json
import logging
import math
import os
import re
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import code_interpreter
import gateway_proxy
import memory
import skills_sync
import system_prompt
from strands import Agent, AgentSkills
from strands.handlers.callback_handler import null_callback_handler
from strands.models import BedrockModel, CacheConfig, CacheToolsConfig
from strands.tools.mcp import MCPClient
from botocore.config import Config as BotocoreConfig

logger = logging.getLogger(__name__)


def _price_env(name: str, default: str) -> float:
    raw = os.environ.get(name, default).strip()
    try:
        value = float(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be a non-negative number") from error
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be a non-negative number")
    return value


def _bounded_int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


MODEL_ID = os.environ.get("MODEL_ID", "").strip() or os.environ.get(
    "MODEL_ARN", ""
).strip()
MODEL_REGION = os.environ.get("MODEL_REGION", "").strip()
AGENT_NAME = os.environ.get("AGENT_NAME", "data-analyst").strip() or "data-analyst"
AGENT_DESCRIPTION = (
    os.environ.get(
        "AGENT_DESCRIPTION",
        "Data analyst with connected databases and managed code execution",
    ).strip()
    or "Data analyst with connected databases and managed code execution"
)
PROMPT_CACHE_TTL = os.environ.get("PROMPT_CACHE_TTL", "5m").strip().lower() or "5m"
if PROMPT_CACHE_TTL not in {"5m", "1h"}:
    raise ValueError("PROMPT_CACHE_TTL must be '5m' or '1h'")
ENABLE_MODEL_USAGE_LOGS = os.environ.get(
    "ENABLE_MODEL_USAGE_LOGS", "true"
).lower() not in {"0", "false", "no"}
MODEL_PRICING_LABEL = os.environ.get(
    "MODEL_PRICING_LABEL", "claude-sonnet-4.6-standard-2026-08"
).strip()
MODEL_INPUT_PRICE_PER_MTOK_USD = _price_env(
    "MODEL_INPUT_PRICE_PER_MTOK_USD", "3.00"
)
MODEL_OUTPUT_PRICE_PER_MTOK_USD = _price_env(
    "MODEL_OUTPUT_PRICE_PER_MTOK_USD", "15.00"
)
MODEL_CACHE_READ_PRICE_PER_MTOK_USD = _price_env(
    "MODEL_CACHE_READ_PRICE_PER_MTOK_USD", "0.30"
)
MODEL_CACHE_WRITE_5M_PRICE_PER_MTOK_USD = _price_env(
    "MODEL_CACHE_WRITE_5M_PRICE_PER_MTOK_USD", "3.75"
)
MODEL_CACHE_WRITE_1H_PRICE_PER_MTOK_USD = _price_env(
    "MODEL_CACHE_WRITE_1H_PRICE_PER_MTOK_USD", "6.00"
)
ENABLE_GATEWAYS = os.environ.get("ENABLE_GATEWAYS", "true").lower() not in {"0", "false", "no"}
ENABLE_CODE_INTERPRETER = os.environ.get(
    "ENABLE_CODE_INTERPRETER", "true"
).lower() not in {"0", "false", "no"}
ENABLE_TOOL_DETAILS = os.environ.get(
    "ENABLE_TOOL_DETAILS", "false"
).lower() in {"1", "true", "yes", "on"}
TOOL_DETAIL_MAX_CHARS = min(
    1_000_000,
    max(1_000, int(os.environ.get("TOOL_DETAIL_MAX_CHARS", "200000"))),
)
MODEL_READ_TIMEOUT_SECONDS = _bounded_int_env(
    "MODEL_READ_TIMEOUT_SECONDS", 900, 60, 900
)
MODEL_CONNECT_TIMEOUT_SECONDS = _bounded_int_env(
    "MODEL_CONNECT_TIMEOUT_SECONDS", 10, 1, 60
)
MODEL_RETRY_MAX_ATTEMPTS = _bounded_int_env(
    "MODEL_RETRY_MAX_ATTEMPTS", 2, 0, 5
)
RUNTIME_STREAM_HEARTBEAT_SECONDS = _bounded_int_env(
    "RUNTIME_STREAM_HEARTBEAT_SECONDS", 15, 5, 300
)


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
            raise TypeError("Invocation payload must be a JSON object")
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
                raise TypeError("Every message must be a JSON object")
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
            model_slug=str(payload.get("model") or "strands-agent"),
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
    """Flatten caller history only when no persistent session manager is active."""
    if len(messages) == 1 and messages[0]["role"] == "user":
        return _content_text(messages[0]["content"])
    lines = [
        f"{item['role'].upper()}: {_content_text(item['content'])}"
        for item in messages[:-1]
    ]
    last = messages[-1]
    lines.extend(["", f"Current {last['role']} message: {_content_text(last['content'])}"])
    return "\n".join(lines)


def _latest_user_text(messages: list[dict]) -> str:
    for item in reversed(messages):
        if item["role"] == "user":
            return _content_text(item["content"])
    return ""


def _model_region() -> str:
    if MODEL_REGION:
        return MODEL_REGION
    match = re.match(r"^arn:[^:]+:bedrock:([^:]+):", MODEL_ID)
    return match.group(1) if match else os.environ.get("AWS_DEFAULT_REGION", "ap-southeast-1")


def _model_usage_payload(
    request: InvocationRequest,
    runtime_agent: Any,
    *,
    duration_ms: int,
    succeeded: bool,
) -> dict[str, Any] | None:
    metrics = getattr(runtime_agent, "event_loop_metrics", None)
    usage = getattr(metrics, "accumulated_usage", None)
    if not isinstance(usage, dict):
        return None

    input_tokens = int(usage.get("inputTokens", 0) or 0)
    output_tokens = int(usage.get("outputTokens", 0) or 0)
    cache_read_tokens = int(usage.get("cacheReadInputTokens", 0) or 0)
    cache_write_tokens = int(usage.get("cacheWriteInputTokens", 0) or 0)
    total_input_tokens = input_tokens + cache_read_tokens + cache_write_tokens
    cache_write_rate = (
        MODEL_CACHE_WRITE_1H_PRICE_PER_MTOK_USD
        if PROMPT_CACHE_TTL == "1h"
        else MODEL_CACHE_WRITE_5M_PRICE_PER_MTOK_USD
    )

    def cost(tokens: int, rate: float) -> float:
        return round(tokens * rate / 1_000_000, 10)

    cost_breakdown = {
        "input": cost(input_tokens, MODEL_INPUT_PRICE_PER_MTOK_USD),
        "output": cost(output_tokens, MODEL_OUTPUT_PRICE_PER_MTOK_USD),
        "cache_read": cost(
            cache_read_tokens,
            MODEL_CACHE_READ_PRICE_PER_MTOK_USD,
        ),
        "cache_write": cost(cache_write_tokens, cache_write_rate),
    }
    return {
        "event": "model_usage",
        "model_id": MODEL_ID,
        "model_slug": request.model_slug,
        "session_id": request.session_id,
        "stream": request.stream,
        "succeeded": succeeded,
        "duration_ms": duration_ms,
        "prompt_cache_ttl": PROMPT_CACHE_TTL,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": cache_read_tokens,
        "cache_write_input_tokens": cache_write_tokens,
        "total_input_tokens": total_input_tokens,
        "total_tokens_reported": int(usage.get("totalTokens", 0) or 0),
        "cache_read_ratio": (
            round(cache_read_tokens / total_input_tokens, 6)
            if total_input_tokens
            else 0.0
        ),
        "estimated_cost_usd": round(sum(cost_breakdown.values()), 10),
        "estimated_cost_breakdown_usd": cost_breakdown,
        "pricing": {
            "label": MODEL_PRICING_LABEL,
            "unit": "USD_per_million_tokens",
            "input": MODEL_INPUT_PRICE_PER_MTOK_USD,
            "output": MODEL_OUTPUT_PRICE_PER_MTOK_USD,
            "cache_read": MODEL_CACHE_READ_PRICE_PER_MTOK_USD,
            "cache_write": cache_write_rate,
        },
    }


def _log_model_usage(
    request: InvocationRequest,
    runtime_agent: Any,
    *,
    started_at: float,
    succeeded: bool,
) -> dict[str, Any] | None:
    """Collect final invocation usage and optionally write it to CloudWatch logs."""
    if runtime_agent is None:
        return None
    payload = _model_usage_payload(
        request,
        runtime_agent,
        duration_ms=round((time.perf_counter() - started_at) * 1000),
        succeeded=succeeded,
    )
    if payload is not None and ENABLE_MODEL_USAGE_LOGS:
        logger.info("MODEL_USAGE %s", json.dumps(payload, separators=(",", ":")))
    return payload


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
    if not MODEL_ID:
        raise ValueError("MODEL_ID or MODEL_ARN must be configured")

    system_messages, ordinary_messages = _split_system(request.messages)
    current_user = _latest_user_text(ordinary_messages)
    if not current_user.strip():
        raise ValueError("The invocation does not contain a user question")

    interpreter_session = None
    memory_session_manager = None
    try:
        memory_session_manager = memory.create_session_manager(
            request.actor_id,
            request.session_id,
            async_mode=request.stream,
        )
        # Native session management restores prior turns. If memory is disabled,
        # retain the caller-provided history as a stateless fallback.
        prompt = current_user if memory_session_manager else _build_prompt(ordinary_messages)

        document_guidance = """When <document_input> tags are present:
Each <document_input> provides the uploaded file’s original filename and S3 URL. Use Code Interpreter to download these files"""
        base_prompt = "\n\n".join(
            part for part in (system_prompt.load(), document_guidance) if part
        )
        skills_enabled = skills_sync.skills_enabled()
        skills_guidance = skills_sync.ACTIVATION_GUIDANCE if skills_enabled else ""
        memory_guidance = memory.MEMORY_GUIDANCE if memory.memory_enabled() else ""
        system_prompt_text = base_prompt + skills_guidance + memory_guidance
        if system_messages:
            system_prompt_text += (
                "\n\n---\n\n## Caller-provided system guidance\n\n"
                + "\n\n".join(system_messages)
            )

        tools: list = []
        plugins: list = []
        if skills_enabled:
            tools.append(skills_sync.read_skill_resource)
            plugins.append(AgentSkills(skills=skills_sync.LOCAL_DIR))
        if ENABLE_CODE_INTERPRETER and code_interpreter.CODE_INTERPRETER_ID:
            interpreter_session = code_interpreter.start_session(request.session_id)
            tools.extend(
                code_interpreter.build_tools(
                    interpreter_session,
                    skill_resource_uri=(
                        skills_sync.skill_resource_s3_uri if skills_enabled else None
                    ),
                )
            )
        tools.extend(_make_gateway_clients())

        model = BedrockModel(
            model_id=MODEL_ID,
            region_name=_model_region(),
            boto_client_config=BotocoreConfig(
                connect_timeout=MODEL_CONNECT_TIMEOUT_SECONDS,
                read_timeout=MODEL_READ_TIMEOUT_SECONDS,
                retries={"mode": "standard", "max_attempts": MODEL_RETRY_MAX_ATTEMPTS},
            ),
            # The default model is an opaque inference-profile ARN, so Strands
            # cannot infer the provider when CacheConfig uses strategy="auto".
            cache_config=CacheConfig(
                strategy="anthropic",
                ttl=PROMPT_CACHE_TTL,
            ),
            cache_tools=CacheToolsConfig(ttl=PROMPT_CACHE_TTL),
        )
        runtime_agent = Agent(
            model=model,
            tools=tools,
            plugins=plugins,
            session_manager=memory_session_manager,
            system_prompt=system_prompt_text,
            callback_handler=null_callback_handler,
            name=AGENT_NAME,
            description=AGENT_DESCRIPTION,
        )
        return (
            runtime_agent,
            interpreter_session,
            memory_session_manager,
            prompt,
        )
    except BaseException:
        if interpreter_session:
            code_interpreter.stop_session(interpreter_session)
        if memory_session_manager:
            try:
                memory_session_manager.close()
            except Exception:
                logger.warning("Unable to flush AgentCore Memory", exc_info=True)
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


def _cleanup(
    runtime_agent: Agent | None,
    interpreter_session: str | None,
    memory_session_manager: Any = None,
) -> None:
    if runtime_agent is not None:
        try:
            runtime_agent.cleanup()
        except Exception:
            logger.warning("Unable to clean up Strands tools", exc_info=True)
    if interpreter_session:
        code_interpreter.stop_session(interpreter_session)
    if memory_session_manager is not None:
        try:
            memory_session_manager.close()
        except Exception:
            logger.warning("Unable to flush AgentCore Memory", exc_info=True)


def run(request: InvocationRequest) -> dict:
    """Run one isolated Strands invocation and return a blocking response."""
    started_at = time.perf_counter()
    succeeded = False
    runtime_agent = None
    interpreter_session = None
    memory_session_manager = None
    response = None
    usage_payload = None
    try:
        runtime_agent, interpreter_session, memory_session_manager, prompt = _prepare(request)
        result = runtime_agent(prompt)
        text = _result_text(result)
        succeeded = True
        response = {
            "result": text,
            "session_id": request.session_id,
            "model": request.model_slug,
        }
    finally:
        usage_payload = _log_model_usage(
            request,
            runtime_agent,
            started_at=started_at,
            succeeded=succeeded,
        )
        _cleanup(runtime_agent, interpreter_session, memory_session_manager)

    if usage_payload is not None:
        response["model_usage"] = usage_payload
    return response


_STEP_UNSAFE = re.compile(r"[^A-Za-z0-9 ._:/()\-]")
_DETAIL_MISSING = object()


def _safe_tool_name(value: Any) -> str:
    raw = " ".join(str(value or "Agent tool").split())
    raw = _STEP_UNSAFE.sub("", raw).strip()
    if "_" in raw and raw.split("_", 1)[0] in gateway_proxy.GATEWAY_CONFIGS:
        prefix, operation = raw.split("_", 1)
        raw = f"{gateway_proxy.mcp_label(prefix)}: {operation.replace('_', ' ')}"
    else:
        raw = raw.replace("_", " ")
    return (raw or "Agent tool")[:120]


def _tool_kind(raw_name: Any) -> str:
    """Classify native skill operations separately for frontend presentation."""
    skill_tools = {"skills", "read_skill_resource"}
    return "skill" if str(raw_name or "") in skill_tools else "tool"


def _step_display_name(raw_name: Any, tool_input: Any) -> Any:
    """Use the activated skill/resource name instead of the generic tool name."""
    if not isinstance(tool_input, dict):
        return raw_name
    if str(raw_name or "") == "skills":
        return tool_input.get("skill_name") or raw_name
    if str(raw_name or "") == "read_skill_resource":
        skill_name = tool_input.get("skill_name")
        resource_path = tool_input.get("resource_path")
        if skill_name and resource_path:
            return f"{skill_name}: {resource_path}"
        return skill_name or resource_path or raw_name
    return raw_name


def _parsed_tool_input(value: Any) -> tuple[Any, bool]:
    """Parse Strands' incrementally assembled JSON tool input when complete."""
    if not isinstance(value, str):
        return value, True
    if not value.strip():
        # Strands may emit an empty first input delta before the JSON arguments.
        # Wait for the real payload so skill events use the selected skill name.
        return {}, False
    try:
        return json.loads(value), True
    except (TypeError, ValueError, json.JSONDecodeError):
        return value, False


def _json_safe_detail(value: Any) -> Any:
    """Return a JSON-safe copy without allowing binary payloads into the stream."""

    def fallback(item: Any) -> Any:
        if isinstance(item, (bytes, bytearray, memoryview)):
            return {"type": "binary", "bytes": len(item)}
        return str(item)

    return json.loads(json.dumps(value, ensure_ascii=False, default=fallback))


def _bounded_detail(value: Any) -> tuple[Any, bool]:
    """Bound one frontend detail while retaining structured JSON when it fits."""
    safe_value = _json_safe_detail(value)
    rendered = json.dumps(safe_value, ensure_ascii=False, separators=(",", ":"))
    if len(rendered) <= TOOL_DETAIL_MAX_CHARS:
        return safe_value, False
    return {
        "preview": rendered[:TOOL_DETAIL_MAX_CHARS],
        "original_chars": len(rendered),
    }, True


def _agent_step(
    tool_id: str,
    raw_name: Any,
    status: str,
    *,
    tool_input: Any = _DETAIL_MISSING,
    output: Any = _DETAIL_MISSING,
) -> dict[str, Any]:
    """Build a bounded sideband event that a frontend can selectively render."""
    details: dict[str, Any] = {}
    if ENABLE_TOOL_DETAILS:
        truncated = False
        if tool_input is not _DETAIL_MISSING:
            bounded_input, input_truncated = _bounded_detail(tool_input)
            details["input"] = bounded_input
            truncated = truncated or input_truncated
        if output is not _DETAIL_MISSING:
            bounded_output, output_truncated = _bounded_detail(output)
            details["output"] = bounded_output
            truncated = truncated or output_truncated
        if truncated:
            details["truncated"] = True

    step: dict[str, Any] = {
        "id": tool_id[:200],
        "type": _tool_kind(raw_name),
        "name": _safe_tool_name(_step_display_name(raw_name, tool_input)),
        "status": status,
    }
    if details:
        step["details"] = details
    return {"event": "agent_step", "step": step}


def _event_tool_results(event: dict[str, Any]) -> list[dict[str, Any]]:
    """Read direct and message-wrapped Strands tool-result event shapes."""
    results: list[dict[str, Any]] = []
    direct_result = event.get("tool_result")
    if isinstance(direct_result, dict):
        results.append(direct_result)

    message = event.get("message")
    if not isinstance(message, dict):
        return results
    content = message.get("content")
    if not isinstance(content, list):
        return results
    for block in content:
        if not isinstance(block, dict):
            continue
        message_result = block.get("toolResult")
        if isinstance(message_result, dict):
            results.append(message_result)
    return results


async def _events_with_heartbeats(events: AsyncIterator[dict]) -> AsyncIterator[dict]:
    """Keep the AgentCore response active while Strands awaits a model or tool."""
    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(maxsize=1)

    async def consume_events() -> None:
        try:
            async for event in events:
                await queue.put(("event", event))
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await queue.put(("error", error))
        else:
            await queue.put(("done", None))

    # Consume the complete Strands iterator in one task. Advancing the same
    # async generator from a new task for each event breaks OpenTelemetry's
    # ContextVar tokens and can prevent Strands from completing its stream.
    producer = asyncio.create_task(consume_events())
    pending_get = None
    try:
        while True:
            pending_get = asyncio.create_task(queue.get())
            done, _ = await asyncio.wait(
                {pending_get, producer},
                timeout=RUNTIME_STREAM_HEARTBEAT_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                pending_get.cancel()
                try:
                    await pending_get
                except asyncio.CancelledError:
                    pass
                pending_get = None
                yield {"event": "heartbeat"}
                continue

            if pending_get in done:
                item_type, payload = pending_get.result()
                pending_get = None
            else:
                # A cancelled or failed producer has no queue item to wake the
                # consumer. Propagate it immediately instead of heartbeating
                # forever. A normally completed producer always queues its
                # terminal item, so wait for that already-scheduled delivery.
                if producer.cancelled() or producer.exception() is not None:
                    pending_get.cancel()
                    try:
                        await pending_get
                    except asyncio.CancelledError:
                        pass
                    pending_get = None
                    await producer
                    return
                item_type, payload = await pending_get
                pending_get = None

            if item_type == "done":
                return
            if item_type == "error":
                raise payload
            yield payload
    finally:
        if pending_get is not None and not pending_get.done():
            pending_get.cancel()
            try:
                await pending_get
            except asyncio.CancelledError:
                pass
        if not producer.done():
            producer.cancel()
        try:
            await producer
        except asyncio.CancelledError:
            pass


async def stream(request: InvocationRequest) -> AsyncIterator[dict]:
    """Stream OpenAI-compatible chunks for the existing Dify proxy."""
    started_at = time.perf_counter()
    succeeded = False
    runtime_agent = None
    interpreter_session = None
    memory_session_manager = None
    usage_payload = None
    text_parts: list[str] = []
    active_steps: dict[str, dict[str, Any]] = {}
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())
    try:
        runtime_agent, interpreter_session, memory_session_manager, prompt = await asyncio.to_thread(
            _prepare, request
        )
        async for event in _events_with_heartbeats(
            runtime_agent.stream_async(prompt)
        ):
            if event.get("event") == "heartbeat":
                yield event
                continue
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
            if tool_id and tool_name:
                step_state = active_steps.setdefault(
                    tool_id,
                    {"name": tool_name, "input": _DETAIL_MISSING, "started": False},
                )
                if "input" in tool_use:
                    step_state["input"] = tool_use["input"]
                if not step_state["started"]:
                    parsed_input, input_complete = _parsed_tool_input(
                        step_state["input"]
                    )
                    if input_complete:
                        step_state["started"] = True
                        yield _agent_step(
                            tool_id,
                            step_state["name"],
                            "started",
                            tool_input=parsed_input,
                        )

            for tool_result in _event_tool_results(event):
                result_tool_id = str(tool_result.get("toolUseId") or "")
                step_state = active_steps.pop(result_tool_id, None)
                if not result_tool_id or step_state is None:
                    continue
                parsed_input, _ = _parsed_tool_input(step_state["input"])
                result_status = tool_result.get("status")
                if not step_state["started"]:
                    yield _agent_step(
                        result_tool_id,
                        step_state["name"],
                        "started",
                        tool_input=parsed_input,
                    )
                yield _agent_step(
                    result_tool_id,
                    step_state["name"],
                    "completed" if result_status == "success" else "failed",
                    tool_input=parsed_input,
                    output=tool_result.get("content", []),
                )

        # Compatibility fallback if a future/older Strands version omits the
        # dedicated tool_result event from the public async event stream.
        for tool_id, step_state in active_steps.items():
            parsed_input, _ = _parsed_tool_input(step_state["input"])
            yield _agent_step(
                tool_id,
                step_state["name"],
                "completed",
                tool_input=parsed_input,
            )
        active_steps.clear()
        succeeded = True
    except asyncio.CancelledError:
        logger.warning(
            "Strands streaming invocation cancelled (session=%s)",
            request.session_id,
        )
        raise
    except Exception as error:
        logger.exception("Strands streaming invocation failed")
        for tool_id, step_state in active_steps.items():
            parsed_input, _ = _parsed_tool_input(step_state["input"])
            yield _agent_step(
                tool_id,
                step_state["name"],
                "failed",
                tool_input=parsed_input,
                output={"error": str(error)},
            )
        active_steps.clear()
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
        usage_payload = _log_model_usage(
            request,
            runtime_agent,
            started_at=started_at,
            succeeded=succeeded,
        )
        await asyncio.to_thread(
            _cleanup,
            runtime_agent,
            interpreter_session,
            memory_session_manager,
        )

    if usage_payload is not None:
        yield usage_payload

    yield {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": request.model_slug,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }

"""
Claude Agent SDK loop for the agentcore_poc runtime.

Phase 2 refactor:
- Streams text deltas as an async generator (was: single buffered return).
- Uses AgentCore Gateway MCP servers via localhost SigV4 proxy (was: in-process psycopg2 tool).
- Loads project Agent Skills from /app/.claude/skills/, with S3 updates at startup.
- Merges `role: system` messages from the caller into the document guidance,
  so Open WebUI's "System Prompt" setting flows straight through.
"""

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from typing import AsyncIterator

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, ResultMessage
from claude_agent_sdk.types import (
    AssistantMessage,
    StreamEvent,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from app import code_interpreter, gateway_proxy, memory, skills_sync

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentStep:
    """Sanitized user-visible lifecycle event for one skill or tool call."""

    kind: str
    name: str
    status: str


_STEP_NAME_UNSAFE_RE = re.compile(r"[^A-Za-z0-9 ._:/()\-]")


def _safe_step_name(value: object, fallback: str) -> str:
    normalized = " ".join(str(value or "").split())
    normalized = _STEP_NAME_UNSAFE_RE.sub("", normalized).strip()
    return (normalized or fallback)[:120]


def _tool_step(block: ToolUseBlock) -> AgentStep:
    """Convert an SDK tool-use block into safe display metadata."""
    raw_name = str(block.name or "")
    if raw_name.lower() == "skill":
        skill_name = (block.input or {}).get("skill") or (block.input or {}).get("name")
        return AgentStep(
            kind="skill",
            name=_safe_step_name(skill_name, "Agent skill"),
            status="started",
        )

    if raw_name.startswith("mcp__"):
        parts = raw_name.split("__", 2)
        if len(parts) == 3:
            server = gateway_proxy.mcp_label(parts[1])
            operation = parts[2].replace("_", " ")
            display_name = f"{server}: {operation}"
        else:
            display_name = raw_name
    else:
        display_name = raw_name.replace("_", " ")

    return AgentStep(
        kind="tool",
        name=_safe_step_name(display_name, "Agent tool"),
        status="started",
    )

MAX_SDK_BUFFER_BYTES = int(
    os.environ.get("CLAUDE_AGENT_MAX_BUFFER_BYTES", str(10 * 1024 * 1024))
)

INFERENCE_PROFILE_ARN = os.environ.get(
    "MODEL_ARN",
    "arn:aws:bedrock:us-east-1:964340114883:application-inference-profile/ji5jakx5lho3",
)

DOCUMENT_GUIDANCE = """When <document_input> tags are present:
Each <document_input> provides the uploaded file’s original filename and S3 URL. Use Code Interpreter to download these files"""


def _split_system(messages: list[dict]) -> tuple[list[str], list[dict]]:
    extras = [str(m.get("content", "")) for m in messages if m.get("role") == "system"]
    non_system = [m for m in messages if m.get("role") != "system"]
    return extras, non_system


def _build_prompt(messages: list[dict]) -> str:
    """Flatten a list of user/assistant messages into a single prompt string."""
    if not messages:
        return ""
    if len(messages) == 1 and messages[0].get("role") == "user":
        return str(messages[0].get("content", ""))

    lines: list[str] = []
    for m in messages[:-1]:
        role = m.get("role", "user")
        content = str(m.get("content", ""))
        lines.append(f"{role.upper()}: {content}")
    last = messages[-1]
    lines.append("")
    lines.append(f"Current question: {last.get('content', '')}")
    return "\n".join(lines)


def _latest_user_text(messages: list[dict]) -> str:
    """Return only the current user turn for short-term memory persistence."""
    for message in reversed(messages):
        if message.get("role") == "user":
            return str(message.get("content", ""))
    return ""


def _build_mcp_servers() -> dict:
    """Return McpHttpServerConfig dicts pointing at the local SigV4 proxy."""
    urls = gateway_proxy.mcp_urls()
    return {
        slug: {"type": "http", "url": url}
        for slug, url in urls.items()
    }


def _build_agent_options(system_prompt: str, mcp_servers: dict) -> ClaudeAgentOptions:
    bedrock_env = {
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "AWS_REGION": "ap-southeast-1",
        "AWS_DEFAULT_REGION": "ap-southeast-1",
        "ENABLE_PROMPT_CACHING_1H": "1"
    }
    return ClaudeAgentOptions(
        model=INFERENCE_PROFILE_ARN,
        cwd="/app",
        setting_sources=["project"],
        system_prompt=system_prompt,
        mcp_servers=mcp_servers,
        allowed_tools=[
            "mcp__code_interpreter__execute_code",
            "mcp__code_interpreter__execute_command",
        ],
        # [] is the Claude SDK's explicit "skills off" value. None would still
        # allow the CLI's own defaults to expose skills.
        skills="all" if skills_sync.skills_enabled() else [],
        permission_mode="bypassPermissions",
        max_turns=50,
        max_buffer_size=MAX_SDK_BUFFER_BYTES,
        include_partial_messages=True,
        env=bedrock_env,
    )


async def stream(
    messages: list[dict],
    actor_id: str | None = None,
    session_id: str | None = None,
) -> AsyncIterator[str | AgentStep]:
    """Yield text deltas from the agent as they arrive.

    actor_id / session_id enable AgentCore Memory:
    - facts relevant to the current prompt are retrieved and appended to the system prompt
    - after the response completes, the user/assistant turn is saved to memory
    """
    system_extras, non_system_msgs = _split_system(messages)
    system_prompt = DOCUMENT_GUIDANCE
    if system_extras:
        system_prompt += "\n\n---\n\n" + "\n\n".join(system_extras)

    prompt = _build_prompt(non_system_msgs)
    if not prompt:
        yield "Please provide a question."
        return
    current_user_text = _latest_user_text(non_system_msgs) or prompt

    # Reconstruct current-session events and retrieve relevant cross-session
    # records from AgentCore Memory. Raw conversation history belongs in the
    # user prompt; only extracted long-term context is appended to the system
    # prompt. Keep the blocking AWS calls off the event loop.
    if actor_id:
        short_term_context, long_term_context = await asyncio.gather(
            asyncio.to_thread(
                memory.retrieve_short_term_context,
                actor_id,
                session_id or "",
            ),
            asyncio.to_thread(
                memory.retrieve_long_term_context,
                actor_id,
                prompt,
            ),
        )
        if short_term_context:
            prompt = short_term_context + "\n\n---\n\n## Current request\n\n" + prompt
        if long_term_context:
            system_prompt += long_term_context
        if short_term_context or long_term_context:
            logger.info(
                "Memory: injected short_term_chars=%d long_term_chars=%d",
                len(short_term_context),
                len(long_term_context),
            )

    code_interpreter_session_id = await code_interpreter.start_session(session_id)
    try:
        mcp_servers = _build_mcp_servers()
        mcp_servers["code_interpreter"] = code_interpreter.build_mcp_server(
            code_interpreter_session_id
        )
        options = _build_agent_options(system_prompt, mcp_servers)
    except BaseException:
        await code_interpreter.stop_session(code_interpreter_session_id)
        raise

    logger.info(
        "Agent invoke: prompt_chars=%d, mcp_servers=%s, actor=%s, session=%s",
        len(prompt), list(mcp_servers.keys()), actor_id, session_id,
    )

    any_text = False
    assistant_buffer: list[str] = []  # accumulated final text for memory.save_turn
    active_steps: dict[str, AgentStep] = {}
    try:
        async with ClaudeSDKClient(options=options) as client:
            await client.query(prompt)
            async for message in client.receive_response():
                if isinstance(message, StreamEvent):
                    # Anthropic raw stream events — token-level deltas. This is the path
                    # that gives real streaming to the client.
                    evt = message.event or {}
                    if evt.get("type") == "content_block_delta":
                        delta = evt.get("delta") or {}
                        if delta.get("type") == "text_delta":
                            text = delta.get("text")
                            if text:
                                any_text = True
                                assistant_buffer.append(text)
                                yield text
                elif isinstance(message, AssistantMessage):
                    # Assistant messages also carry tool-use blocks. Emit only safe
                    # names; raw tool inputs may contain SQL, file paths, or secrets.
                    for block in message.content:
                        if isinstance(block, ToolUseBlock):
                            step = _tool_step(block)
                            active_steps[block.id] = step
                            yield step
                        elif not any_text and isinstance(block, TextBlock) and block.text:
                            any_text = True
                            assistant_buffer.append(block.text)
                            yield block.text
                elif isinstance(message, UserMessage) and isinstance(message.content, list):
                    # Claude Agent SDK returns tool results as user-message content.
                    # Report only lifecycle state, never result content.
                    for block in message.content:
                        if not isinstance(block, ToolResultBlock):
                            continue
                        started = active_steps.pop(block.tool_use_id, None)
                        if started:
                            yield AgentStep(
                                kind=started.kind,
                                name=started.name,
                                status="failed" if block.is_error else "completed",
                            )
                elif isinstance(message, ResultMessage):
                    logger.info(
                        "Agent done: is_error=%s stop=%s turns=%d streamed=%s",
                        message.is_error,
                        message.stop_reason,
                        message.num_turns,
                        any_text,
                    )
                    if not any_text and message.result:
                        assistant_buffer.append(message.result)
                        yield message.result
                    elif message.is_error and message.result:
                        yield f"\n\n[error] {message.result}"
    finally:
        await code_interpreter.stop_session(code_interpreter_session_id)

    # Persist this turn to AgentCore Memory. Fire-and-forget in a thread so
    # we don't delay the SSE response completion.
    if actor_id and session_id and assistant_buffer:
        final_text = "".join(assistant_buffer)
        await asyncio.to_thread(
            memory.save_turn,
            actor_id,
            session_id,
            current_user_text,
            final_text,
        )

"""
Claude Agent SDK loop for the agentcore_poc runtime.

Phase 2 refactor:
- Streams text deltas as an async generator (was: single buffered return).
- Uses AgentCore Gateway MCP servers via localhost SigV4 proxy (was: in-process psycopg2 tool).
- Loads Agent Skills from /app/skills/ synced from S3 at startup.
- Merges `role: system` messages from the caller into the base system prompt,
  so Open WebUI's "System Prompt" setting flows straight through.
"""

import asyncio
import logging
import os
from typing import AsyncIterator

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query
from claude_agent_sdk.types import AssistantMessage, StreamEvent, TextBlock

from app import gateway_proxy, memory

logger = logging.getLogger(__name__)

INFERENCE_PROFILE_ARN = os.environ.get(
    "MODEL_ARN",
    "arn:aws:bedrock:us-east-1:964340114883:application-inference-profile/ji5jakx5lho3",
)

BASE_SYSTEM_PROMPT = """You are a data analyst for Alexandra Hospital (AH) and National University Hospital (NUH).

You have access to three MCP tool servers:
- `nuh` — SQL against the nuh-analytics database (tables: emd, inpatient_movement, soc, surgery)
- `ah`  — SQL against the ah-analytics database (tables: outpatient, urgentcarecenter, admission, discharge, inflight, procedure)
- `fm`  — TimesFM time-series forecasting (`timesfm_forecast` tool)

Always check the loaded skill files (Skill_*.md, SKILL.md) for column semantics,
mandatory WHERE filters, and canonical SQL patterns before writing queries.
When generating charts/dashboards, return a single self-contained HTML document
wrapped in one ```html fenced block starting with <!DOCTYPE html>.
Explain findings clearly.
"""


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


def _load_skill_paths() -> list[str]:
    """Return the list of skill file paths that exist on disk."""
    skill_dir = "/app/skills"
    if not os.path.isdir(skill_dir):
        return []
    paths = sorted(
        os.path.join(skill_dir, f)
        for f in os.listdir(skill_dir)
        if f.endswith(".md")
    )
    return paths


def _build_mcp_servers() -> dict:
    """Return McpHttpServerConfig dicts pointing at the local SigV4 proxy."""
    urls = gateway_proxy.mcp_urls()
    return {
        slug: {"type": "http", "url": url}
        for slug, url in urls.items()
    }


async def stream(
    messages: list[dict],
    actor_id: str | None = None,
    session_id: str | None = None,
) -> AsyncIterator[str]:
    """Yield text deltas from the agent as they arrive.

    actor_id / session_id enable AgentCore Memory:
    - facts relevant to the current prompt are retrieved and appended to the system prompt
    - after the response completes, the user/assistant turn is saved to memory
    """
    system_extras, non_system_msgs = _split_system(messages)
    system_prompt = BASE_SYSTEM_PROMPT
    if system_extras:
        system_prompt += "\n\n---\n\n" + "\n\n".join(system_extras)

    prompt = _build_prompt(non_system_msgs)
    if not prompt:
        yield "Please provide a question."
        return

    # Inject prior-conversation context from AgentCore Memory
    if actor_id:
        mem_context = memory.retrieve_context(actor_id, session_id or "", prompt)
        if mem_context:
            system_prompt += mem_context
            logger.info("Memory: %d chars of context injected", len(mem_context))

    skill_paths = _load_skill_paths()
    mcp_servers = _build_mcp_servers()

    logger.info(
        "Agent invoke: prompt_chars=%d, skills=%d, mcp_servers=%s, actor=%s, session=%s",
        len(prompt), len(skill_paths), list(mcp_servers.keys()), actor_id, session_id,
    )

    bedrock_env = {
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "AWS_REGION": "us-east-1",
        "AWS_DEFAULT_REGION": "us-east-1",
    }

    options = ClaudeAgentOptions(
        model=INFERENCE_PROFILE_ARN,
        system_prompt=system_prompt,
        mcp_servers=mcp_servers,
        skills=skill_paths if skill_paths else None,
        permission_mode="bypassPermissions",
        max_turns=15,
        include_partial_messages=True,
        env=bedrock_env,
    )

    any_text = False
    assistant_buffer: list[str] = []  # accumulated final text for memory.save_turn
    async for message in query(prompt=prompt, options=options):
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
            # Emitted after the full assistant turn. Only yield if StreamEvent
            # didn't already deliver the text (defensive fallback).
            if not any_text:
                for block in message.content:
                    if isinstance(block, TextBlock) and block.text:
                        any_text = True
                        assistant_buffer.append(block.text)
                        yield block.text
        elif isinstance(message, ResultMessage):
            logger.info(
                "Agent done: is_error=%s stop=%s turns=%d streamed=%s",
                message.is_error, message.stop_reason, message.num_turns, any_text,
            )
            if not any_text and message.result:
                assistant_buffer.append(message.result)
                yield message.result
            elif message.is_error and message.result:
                yield f"\n\n[error] {message.result}"

    # Persist this turn to AgentCore Memory. Fire-and-forget in a thread so
    # we don't delay the SSE response completion.
    if actor_id and session_id and assistant_buffer:
        final_text = "".join(assistant_buffer)
        await asyncio.to_thread(memory.save_turn, actor_id, session_id, prompt, final_text)

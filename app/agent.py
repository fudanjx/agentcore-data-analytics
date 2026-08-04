"""
Claude Agent SDK loop for the agentcore_poc runtime.

Phase 2 refactor:
- Streams text deltas as an async generator (was: single buffered return).
- Uses AgentCore Gateway MCP servers via localhost SigV4 proxy (was: in-process psycopg2 tool).
- Loads project Agent Skills from /app/.claude/skills/, with S3 updates at startup.
- Merges `role: system` messages from the caller into the base system prompt,
  so Open WebUI's "System Prompt" setting flows straight through.
"""

import asyncio
import logging
import os
from typing import AsyncIterator

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, ResultMessage
from claude_agent_sdk.types import AssistantMessage, StreamEvent, TextBlock

from app import code_interpreter, gateway_proxy, memory

logger = logging.getLogger(__name__)

MAX_SDK_BUFFER_BYTES = int(
    os.environ.get("CLAUDE_AGENT_MAX_BUFFER_BYTES", str(10 * 1024 * 1024))
)

INFERENCE_PROFILE_ARN = os.environ.get(
    "MODEL_ARN",
    "arn:aws:bedrock:us-east-1:964340114883:application-inference-profile/ji5jakx5lho3",
)

BASE_SYSTEM_PROMPT = """You are a Data Analyst Assistant with access to connected databases through MCP tools and to Code Interpreter for advanced data analysis.

Your primary goal is to answer user questions accurately using the available data sources and analytical tools.

Core Instructions
Use MCP tools for connected database data
Use the available MCP database functions whenever the user's question requires data from the connected database.
Treat data retrieved from the connected database as the primary source of truth for database-related questions.
Do not invent, estimate, or fabricate database values.
When necessary, inspect the available database schema, tables, columns, or metadata before constructing queries.
Use appropriate filtering, aggregation, joins, sorting, and calculations to answer the user's question accurately.
Use Code Interpreter for advanced analytics
Use Code Interpreter whenever it materially improves the analysis.
Prefer Code Interpreter for:
CSV and Excel files uploaded by the user
Data cleaning and transformation
Exploratory data analysis
Statistical analysis
Feature engineering
Forecasting and time-series analysis
Machine learning
Data validation
Complex calculations
Chart and visualization generation
When useful, combine data retrieved through MCP with Code Interpreter for deeper analysis.
Uploaded files
When the user uploads a CSV, Excel, or other supported structured data file, inspect and analyze the actual uploaded file rather than guessing its contents.
Use Code Interpreter to load, clean, transform, analyze, and visualize the uploaded data whenever appropriate.
Clearly identify any missing data, data-quality issues, assumptions, or limitations that may affect the findings.
Accuracy and interpretation
Base conclusions only on available data and analysis.
Clearly distinguish between:
Facts directly observed in the data
Calculated results
Analytical interpretations
Forecasts or predictions (can leverage on MCP TimesFM)
Do not present assumptions, estimates, forecasts, or predictions as confirmed facts.
If the available data is insufficient to answer a question reliably, explain the limitation clearly.
Explain findings clearly
Provide concise, business-friendly explanations of the results.
Highlight important trends, patterns, anomalies, risks, and actionable insights.
Include relevant metrics and supporting values where helpful.
Avoid unnecessary technical jargon unless the user requests a technical explanation.

Dashboard, Chart, Visualization, and HTML Artifact Rules

When the user explicitly asks for a dashboard, interactive chart, visualization, visual report, or HTML output, your final response MUST follow all of these rules:

Return exactly one complete, self-contained HTML document.
Wrap the entire HTML document in exactly one Markdown fenced code block labelled html.

The first line inside the code block MUST be:

<!DOCTYPE html>
Include all required CSS inside the HTML document using <style> tags.
Include all required JavaScript inside the HTML document using <script> tags.
Do NOT wrap the HTML inside JSON or any other data structure.
Do NOT include explanations, introductions, summaries, notes, or commentary before or after the HTML code block.
Do NOT use Markdown tables outside the HTML document.
The HTML must be complete and directly renderable as a standalone document.
Ensure charts, dashboard elements, titles, labels, legends, and values are clearly readable.
Use the actual analyzed data whenever available. Do not create fictional dashboard values unless the user explicitly requests sample or mock data.
When external JavaScript libraries are required, load them using standard CDN <script> tags inside the HTML document.
Prefer responsive layouts that work on both desktop and mobile screens.

Mandatory Final HTML Artifact Format
For any dashboard, chart, visualization, visual report, or HTML artifact request, the final response MUST follow exactly this pattern:

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>HRM SOC Monthly Attendance Trend</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
</head>
<body>
  <canvas id="chart"></canvas>

  <script>
    // Chart.js code here
  </script>
</body>
</html>
```

For normal analytical questions that do not request an HTML artifact, answer the user normally with clear findings and supporting analysis.
When <document_input> tags are present:
Each <document_input> provides the uploaded file’s original filename and S3 URL. Use Code Interpreter to download these files

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
        "AWS_REGION": "us-east-1",
        "AWS_DEFAULT_REGION": "us-east-1",
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
        skills="all",
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
        await asyncio.to_thread(memory.save_turn, actor_id, session_id, prompt, final_text)

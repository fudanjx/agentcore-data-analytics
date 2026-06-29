import os

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

from app.db import get_schema
from app.tools import sql_server

_SYSTEM_PROMPT_TEMPLATE = """You are a data analyst assistant. The connected database has the following schema:

{schema}

Use the execute_sql tool to answer user questions with accurate data from the database.
Always explain your findings clearly. Only run SELECT queries — never modify data."""


def _build_prompt(messages: list[dict]) -> str:
    """Convert an OpenAI messages list into a single prompt string.

    Prior conversation turns are included as labeled context so the agent
    understands the history. The last user message is the active question.
    """
    if not messages:
        return ""

    if len(messages) == 1:
        return messages[-1]["content"]

    lines = ["Conversation history:"]
    for msg in messages[:-1]:
        role = msg["role"].capitalize()
        lines.append(f"{role}: {msg['content']}")

    lines.append("")
    lines.append(f"Current question: {messages[-1]['content']}")
    return "\n".join(lines)


async def run(messages: list[dict]) -> str:
    """Run the agent for one stateless request and return the final text."""
    try:
        schema = get_schema()
    except Exception as e:
        return str(e)

    system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(schema=schema)
    prompt = _build_prompt(messages)

    # Tell the claude subprocess to use Bedrock via IAM (no API key needed).
    # CLAUDE_CODE_USE_BEDROCK=1 switches the CLI from Anthropic API to Bedrock.
    # The inference profile is in us-east-1; Bedrock calls must target that region.
    bedrock_env = {
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "AWS_REGION": "us-east-1",
        "AWS_DEFAULT_REGION": "us-east-1",
    }

    options = ClaudeAgentOptions(
        tools=[],
        allowed_tools=["execute_sql"],
        system_prompt=system_prompt,
        mcp_servers={"db": sql_server},
        permission_mode="bypassPermissions",
        max_turns=10,
        model="arn:aws:bedrock:us-east-1:964340114883:application-inference-profile/ji5jakx5lho3",
        env=bedrock_env,
    )

    result_text = ""
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, ResultMessage):
            result_text = message.result or ""
            break

    return result_text

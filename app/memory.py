"""
AgentCore Memory client for the poc agent.

- retrieve_context(): pull the top-5 semantic facts relevant to the user's query
  from the shared harness memory, plus the most recent session summary. Formatted
  as a markdown block to append to the system prompt.
- save_turn(): fire-and-forget save of a user/assistant turn pair.

Memory namespace layout:
  /actors/{actor_id}/facts/                 — semantic strategy (cross-session facts)
  /actors/{actor_id}/summaries/{sess_id}/   — summarization strategy (per-conversation)
"""

import logging
from datetime import datetime, timezone

import boto3

logger = logging.getLogger(__name__)

REGION = "ap-southeast-1"
MEMORY_ID = "memory_agentcore_dev-X9UlwN6fTM"

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = boto3.client("bedrock-agentcore", region_name=REGION)
    return _client


def retrieve_context(actor_id: str, session_id: str, query: str) -> str:
    """Return a markdown block of relevant prior context, or '' if none."""
    if not actor_id or len(query.strip()) < 20:
        return ""

    client = _get_client()
    lines: list[str] = []

    try:
        facts = client.retrieve_memory_records(
            memoryId=MEMORY_ID,
            namespace=f"/actors/{actor_id}/facts/",
            searchCriteria={"searchQuery": query, "topK": 5},
        )
        for rec in facts.get("memoryRecordSummaries", []):
            content = rec.get("content", {}).get("text", "") or ""
            if content.strip():
                lines.append(f"- {content.strip()}")
    except Exception as e:
        logger.warning("Memory: retrieve facts failed: %s", e)

    if not lines:
        return ""

    return (
        "\n\n---\n\n## Relevant context from prior conversations with this user\n\n"
        + "\n".join(lines)
    )


def save_turn(actor_id: str, session_id: str, user_msg: str, assistant_msg: str) -> None:
    """Persist a user/assistant turn to memory. Errors are logged, not raised."""
    if not actor_id or not session_id:
        return
    if not user_msg and not assistant_msg:
        return

    client = _get_client()
    try:
        client.create_event(
            memoryId=MEMORY_ID,
            actorId=actor_id,
            sessionId=session_id,
            eventTimestamp=datetime.now(timezone.utc),
            payload=[
                {"conversational": {"role": "USER",
                                    "content": {"text": user_msg[:8000]}}},
                {"conversational": {"role": "ASSISTANT",
                                    "content": {"text": assistant_msg[:8000]}}},
            ],
        )
        logger.info("Memory: saved turn for actor=%s session=%s", actor_id, session_id)
    except Exception as e:
        logger.warning("Memory: save_turn failed: %s", e)

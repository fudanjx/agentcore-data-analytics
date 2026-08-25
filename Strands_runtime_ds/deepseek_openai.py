"""DeepSeek V4 Flash adapter for the OpenAI-compatible Responses API."""

from __future__ import annotations

from typing import Any

import httpx
from strands.models.openai_responses import OpenAIResponsesModel


DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL_ID = "deepseek-v4-flash"
PLACEHOLDER_API_KEY = "PENDING_REPLACE_ME"
_REASONING_EFFORTS = {"low", "high", "max"}
_MIN_OUTPUT_TOKENS = 1_024
_MAX_OUTPUT_TOKENS = 384_000


def validate_api_key(value: str | None) -> None:
    """Reject an absent or deployment-placeholder key without exposing it."""
    normalized = (value or "").strip()
    if not normalized or normalized.casefold() == PLACEHOLDER_API_KEY.casefold():
        raise ValueError(
            "DEEPSEEK_API_KEY is not configured; replace PENDING_REPLACE_ME "
            "in the AgentCore Runtime environment"
        )


def validate_reasoning_effort(value: str) -> str:
    normalized = value.strip().lower()
    if normalized not in _REASONING_EFFORTS:
        raise ValueError(
            "DEEPSEEK_REASONING_EFFORT must be 'low', 'high', or 'max'"
        )
    return normalized


def validate_max_output_tokens(value: str | int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError("DEEPSEEK_MAX_OUTPUT_TOKENS must be an integer") from error
    if not _MIN_OUTPUT_TOKENS <= parsed <= _MAX_OUTPUT_TOKENS:
        raise ValueError(
            "DEEPSEEK_MAX_OUTPUT_TOKENS must be between "
            f"{_MIN_OUTPUT_TOKENS} and {_MAX_OUTPUT_TOKENS}"
        )
    return parsed


def validate_base_url(value: str) -> str:
    """Keep this runtime pinned to DeepSeek's official API endpoint."""
    normalized = value.strip().rstrip("/")
    if normalized != DEFAULT_BASE_URL:
        raise ValueError(f"DEEPSEEK_BASE_URL must be {DEFAULT_BASE_URL}")
    return normalized


class DeepSeekResponsesModel(OpenAIResponsesModel):
    """Stateless Responses model with DeepSeek-specific usage normalization."""

    def __init__(
        self,
        *,
        model_id: str,
        api_key: str,
        base_url: str,
        reasoning_effort: str,
        max_output_tokens: int,
        connect_timeout_seconds: int,
        read_timeout_seconds: int,
        max_retries: int,
    ) -> None:
        validate_api_key(api_key)
        if model_id != DEFAULT_MODEL_ID:
            raise ValueError(f"MODEL_ID must be {DEFAULT_MODEL_ID}")
        normalized_base_url = validate_base_url(base_url)
        normalized_effort = validate_reasoning_effort(reasoning_effort)
        normalized_output_tokens = validate_max_output_tokens(max_output_tokens)
        self.reasoning_tokens = 0
        super().__init__(
            model_id=model_id,
            client_args={
                "api_key": api_key,
                "base_url": normalized_base_url,
                "timeout": httpx.Timeout(
                    timeout=read_timeout_seconds,
                    connect=connect_timeout_seconds,
                ),
                "max_retries": max_retries,
            },
            params={
                "reasoning": {"effort": normalized_effort},
                "max_output_tokens": normalized_output_tokens,
            },
            stateful=False,
        )

    @classmethod
    def _format_request_messages(cls, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Preserve DeepSeek reasoning items when continuing a tool loop.

        Strands stores streamed reasoning as a ``reasoningContent`` block. The
        generic Responses adapter currently drops that block on the next model
        turn, while DeepSeek explicitly accepts a stateless ``reasoning`` input
        item. Replaying it keeps the reasoning/tool-call sequence coherent.
        """
        formatted_messages: list[dict[str, Any]] = []
        for message in messages:
            role = message["role"]
            contents = message["content"]

            for content in contents:
                reasoning = content.get("reasoningContent")
                if not isinstance(reasoning, dict):
                    continue
                reasoning_text = reasoning.get("reasoningText")
                text = (
                    reasoning_text.get("text")
                    if isinstance(reasoning_text, dict)
                    else None
                )
                if isinstance(text, str) and text:
                    formatted_messages.append(
                        {
                            "type": "reasoning",
                            "content": [{"type": "reasoning_text", "text": text}],
                        }
                    )

            formatted_contents = [
                cls._format_request_message_content(content, role=role)
                for content in contents
                if not any(
                    block_type in content
                    for block_type in ("toolResult", "toolUse", "reasoningContent")
                )
            ]
            if formatted_contents:
                if role == "assistant":
                    text = "\n".join(
                        part["text"]
                        for part in formatted_contents
                        if part.get("type") == "output_text"
                    )
                    if text:
                        formatted_messages.append(
                            {"role": "assistant", "content": text}
                        )
                else:
                    formatted_messages.append(
                        {"role": role, "content": formatted_contents}
                    )

            formatted_messages.extend(
                cls._format_request_message_tool_call(content["toolUse"])
                for content in contents
                if "toolUse" in content
            )
            formatted_messages.extend(
                cls._format_request_tool_message(content["toolResult"])
                for content in contents
                if "toolResult" in content
            )

        return [
            item
            for item in formatted_messages
            if item.get("content")
            or item.get("type")
            in {"reasoning", "function_call", "function_call_output"}
        ]

    def _format_chunk(self, event: dict[str, Any]) -> dict[str, Any]:
        chunk = super()._format_chunk(event)
        if event.get("chunk_type") != "metadata":
            return chunk

        usage = chunk.get("metadata", {}).get("usage")
        provider_usage = event.get("data")
        if not isinstance(usage, dict) or provider_usage is None:
            return chunk

        reported_input = getattr(provider_usage, "input_tokens", 0)
        input_details = getattr(provider_usage, "input_tokens_details", None)
        cached_input = getattr(input_details, "cached_tokens", 0)
        if not isinstance(reported_input, int) or isinstance(reported_input, bool):
            reported_input = 0
        if not isinstance(cached_input, int) or isinstance(cached_input, bool):
            cached_input = 0
        cached_input = max(0, min(reported_input, cached_input))
        usage["inputTokens"] = max(0, reported_input - cached_input)
        if cached_input:
            usage["cacheReadInputTokens"] = cached_input
        else:
            usage.pop("cacheReadInputTokens", None)

        output_details = getattr(provider_usage, "output_tokens_details", None)
        reasoning_tokens = getattr(output_details, "reasoning_tokens", 0)
        if isinstance(reasoning_tokens, int) and not isinstance(reasoning_tokens, bool):
            self.reasoning_tokens += max(0, reasoning_tokens)
        return chunk

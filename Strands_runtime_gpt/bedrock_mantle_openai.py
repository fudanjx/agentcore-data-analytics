"""OpenAI Responses model adapter for Amazon Bedrock Mantle.

The upstream Strands provider already handles short-lived Bedrock bearer tokens,
the Mantle endpoint, streaming, and function-tool calls.  This small adapter adds
Luna's explicit prompt-cache breakpoint to the stable developer prompt and maps
cache-write usage when Mantle returns it.
"""

from __future__ import annotations

import hashlib
from typing import Any

from strands.models.openai_responses import OpenAIResponsesModel


class BedrockMantleOpenAIResponsesModel(OpenAIResponsesModel):
    """Responses API model with an optional explicit Luna prompt-cache point."""

    def __init__(
        self,
        *,
        prompt_cache_enabled: bool,
        prompt_cache_key_prefix: str,
        **model_config: Any,
    ) -> None:
        super().__init__(**model_config)
        self._prompt_cache_enabled = prompt_cache_enabled
        self._prompt_cache_key_prefix = prompt_cache_key_prefix

    def _format_request(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        request = super()._format_request(*args, **kwargs)
        instructions = request.pop("instructions", None)
        if not instructions:
            return request

        # A cache point is deliberately attached only to the developer/system
        # prompt. User messages, chat history, tool inputs, and tool results are
        # never marked cacheable by this runtime.
        content: dict[str, Any] = {"type": "input_text", "text": instructions}
        if self._prompt_cache_enabled:
            content["prompt_cache_breakpoint"] = {"mode": "explicit"}
            digest = hashlib.sha256(instructions.encode("utf-8")).hexdigest()[:32]
            request["prompt_cache_key"] = f"{self._prompt_cache_key_prefix}:{digest}"
            request["prompt_cache_options"] = {"mode": "explicit"}

        request["input"] = [
            {"role": "developer", "content": [content]},
            *request["input"],
        ]
        return request

    def _format_chunk(self, event: dict[str, Any]) -> dict[str, Any]:
        chunk = super()._format_chunk(event)
        if event.get("chunk_type") != "metadata":
            return chunk

        usage = chunk.get("metadata", {}).get("usage")
        data = event.get("data")
        if not isinstance(usage, dict) or data is None:
            return chunk

        # Mantle has used both a top-level field and an input-token-details
        # field across compatible Responses payloads. Read either safely.
        cache_write = getattr(data, "cache_write_tokens", None)
        if cache_write is None:
            details = getattr(data, "input_tokens_details", None)
            cache_write = getattr(details, "cache_write_tokens", None)
        if isinstance(cache_write, int) and not isinstance(cache_write, bool) and cache_write > 0:
            usage["cacheWriteInputTokens"] = cache_write

        # Responses reports input_tokens as the complete input count, including
        # any cache-read or cache-write tokens.  The Runtime MODEL_USAGE schema
        # defines inputTokens as the non-cached remainder, matching the Claude
        # telemetry contract used by the existing Strands runtime.
        reported_input = usage.get("inputTokens", 0)
        cache_read = usage.get("cacheReadInputTokens", 0)
        cache_write = usage.get("cacheWriteInputTokens", 0)
        if all(
            isinstance(value, int) and not isinstance(value, bool)
            for value in (reported_input, cache_read, cache_write)
        ):
            usage["inputTokens"] = max(0, reported_input - cache_read - cache_write)
        return chunk

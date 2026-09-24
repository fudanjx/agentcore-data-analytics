"""OpenAI Responses adapter for the Amazon Bedrock Runtime endpoint.

The upstream Strands Responses provider supports arbitrary OpenAI-compatible
clients, but its built-in Bedrock helper currently targets Bedrock Mantle. This
adapter points the same provider at Bedrock Runtime, refreshes the short-lived
Bedrock bearer token for every request, and adds GPT-5.6 explicit prompt
caching to the stable developer prompt.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from strands.models.openai_responses import OpenAIResponsesModel


_REGION_PATTERN = re.compile(r"^[a-z0-9-]+-\d+$")
_BASE_URL_TEMPLATE = "https://bedrock-runtime.{region}.amazonaws.com/openai/v1"


class BedrockRuntimeOpenAIResponsesModel(OpenAIResponsesModel):
    """Responses API model routed through the Bedrock Runtime endpoint."""

    def __init__(
        self,
        *,
        region: str,
        prompt_cache_enabled: bool,
        prompt_cache_key_prefix: str,
        prompt_cache_ttl: str,
        **model_config: Any,
    ) -> None:
        normalized_region = region.strip().lower()
        if not _REGION_PATTERN.fullmatch(normalized_region):
            raise ValueError(f"Invalid Bedrock Runtime region: {region!r}")
        if prompt_cache_enabled and prompt_cache_ttl != "30m":
            raise ValueError("Bedrock Runtime GPT Responses caching requires a 30m TTL")

        client_args = dict(model_config.pop("client_args", {}) or {})
        conflicting = [key for key in ("api_key", "base_url") if key in client_args]
        if conflicting:
            raise ValueError(
                "client_args must not contain "
                f"{conflicting} for bedrock_runtime_openai; authentication and the "
                "base URL are derived from the runtime region"
            )

        super().__init__(client_args=client_args, **model_config)
        self._region = normalized_region
        self._prompt_cache_enabled = prompt_cache_enabled
        self._prompt_cache_key_prefix = prompt_cache_key_prefix
        self._prompt_cache_ttl = prompt_cache_ttl

    def _resolve_client_args(self) -> dict[str, Any]:
        """Mint/refresh a Bedrock bearer token and build OpenAI client args."""
        try:
            from aws_bedrock_token_generator import provide_token
        except ImportError as error:
            raise ImportError(
                "bedrock_runtime_openai requires aws-bedrock-token-generator"
            ) from error

        try:
            token = provide_token(region=self._region)
        except Exception as error:
            raise RuntimeError(
                "Failed to mint a Bedrock bearer token for "
                f"region {self._region!r}; verify the execution-role credentials "
                "and bedrock:CallWithBearerToken permission"
            ) from error

        client_args = dict(self.client_args)
        client_args["base_url"] = _BASE_URL_TEMPLATE.format(region=self._region)
        client_args["api_key"] = token
        return client_args

    def _format_request(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        request = super()._format_request(*args, **kwargs)
        instructions = request.pop("instructions", None)
        if not instructions:
            return request

        # Top-level instructions cannot carry a Responses cache breakpoint, so
        # express the stable system prompt as the first developer input item.
        content: dict[str, Any] = {"type": "input_text", "text": instructions}
        if self._prompt_cache_enabled:
            content["prompt_cache_breakpoint"] = {"mode": "explicit"}
            digest = hashlib.sha256(instructions.encode("utf-8")).hexdigest()[:32]
            request["prompt_cache_key"] = (
                f"{self._prompt_cache_key_prefix}:{digest}"
            )
            request["prompt_cache_options"] = {
                "mode": "explicit",
                "ttl": self._prompt_cache_ttl,
            }

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

        cache_write = getattr(data, "cache_write_tokens", None)
        if cache_write is None:
            details = getattr(data, "input_tokens_details", None)
            cache_write = getattr(details, "cache_write_tokens", None)
        if (
            isinstance(cache_write, int)
            and not isinstance(cache_write, bool)
            and cache_write > 0
        ):
            usage["cacheWriteInputTokens"] = cache_write

        # Responses input_tokens includes cache reads and writes. Convert it to
        # the non-cached input count used by the existing MODEL_USAGE contract.
        reported_input = usage.get("inputTokens", 0)
        cache_read = usage.get("cacheReadInputTokens", 0)
        cache_write = usage.get("cacheWriteInputTokens", 0)
        if all(
            isinstance(value, int) and not isinstance(value, bool)
            for value in (reported_input, cache_read, cache_write)
        ):
            usage["inputTokens"] = max(
                0, reported_input - cache_read - cache_write
            )
        return chunk

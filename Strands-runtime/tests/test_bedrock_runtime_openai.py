"""Unit tests for the Bedrock Runtime OpenAI Responses adapter."""

from __future__ import annotations

import importlib
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace


RUNTIME_DIR = Path(__file__).resolve().parents[1]
if str(RUNTIME_DIR) not in sys.path:
    sys.path.insert(0, str(RUNTIME_DIR))


class _FakeOpenAIResponsesModel:
    def __init__(self, client_args=None, **model_config):
        self.client_args = client_args or {}
        self.config = model_config

    def _format_request(self, *_args, **_kwargs):
        return {
            "model": self.config["model_id"],
            "instructions": "Stable developer instructions",
            "input": [{"role": "user", "content": "question"}],
            "stream": True,
        }

    def _format_chunk(self, event):
        if event.get("chunk_type") != "metadata":
            return {"event": event}
        data = event["data"]
        details = getattr(data, "input_tokens_details", None)
        cached = getattr(details, "cached_tokens", 0) if details else 0
        usage = {
            "inputTokens": data.input_tokens,
            "outputTokens": data.output_tokens,
            "totalTokens": data.total_tokens,
        }
        if cached:
            usage["cacheReadInputTokens"] = cached
        return {"metadata": {"usage": usage}}


fake_openai_responses = types.ModuleType("strands.models.openai_responses")
fake_openai_responses.OpenAIResponsesModel = _FakeOpenAIResponsesModel
sys.modules["strands.models.openai_responses"] = fake_openai_responses

token_calls: list[str] = []
fake_token_generator = types.ModuleType("aws_bedrock_token_generator")


def _provide_token(*, region):
    token_calls.append(region)
    return f"token-for-{region}"


fake_token_generator.provide_token = _provide_token
sys.modules["aws_bedrock_token_generator"] = fake_token_generator

import bedrock_runtime_openai


class BedrockRuntimeOpenAIResponsesModelTests(unittest.TestCase):
    def setUp(self) -> None:
        token_calls.clear()
        self.module = importlib.reload(bedrock_runtime_openai)

    def make_model(self, *, cache_enabled: bool = True):
        return self.module.BedrockRuntimeOpenAIResponsesModel(
            model_id="us.openai.gpt-5.6-luna",
            region="us-east-1",
            client_args={"max_retries": 2},
            stateful=False,
            prompt_cache_enabled=cache_enabled,
            prompt_cache_key_prefix="test-runtime",
            prompt_cache_ttl="30m",
        )

    def test_resolves_runtime_endpoint_and_refreshes_bearer_token(self) -> None:
        model = self.make_model()

        first = model._resolve_client_args()
        second = model._resolve_client_args()

        self.assertEqual(
            first["base_url"],
            "https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1",
        )
        self.assertEqual(first["api_key"], "token-for-us-east-1")
        self.assertEqual(first["max_retries"], 2)
        self.assertEqual(second["api_key"], "token-for-us-east-1")
        self.assertEqual(token_calls, ["us-east-1", "us-east-1"])

    def test_adds_explicit_cache_breakpoint_to_developer_prompt(self) -> None:
        request = self.make_model()._format_request([], [], "ignored")

        developer = request["input"][0]
        content = developer["content"][0]
        self.assertEqual(developer["role"], "developer")
        self.assertEqual(content["text"], "Stable developer instructions")
        self.assertEqual(
            content["prompt_cache_breakpoint"], {"mode": "explicit"}
        )
        self.assertEqual(
            request["prompt_cache_options"],
            {"mode": "explicit", "ttl": "30m"},
        )
        self.assertTrue(request["prompt_cache_key"].startswith("test-runtime:"))
        self.assertNotIn("instructions", request)

    def test_disabled_cache_keeps_developer_prompt_without_cache_fields(self) -> None:
        request = self.make_model(cache_enabled=False)._format_request([], [], "ignored")

        self.assertEqual(request["input"][0]["role"], "developer")
        self.assertNotIn("prompt_cache_breakpoint", request["input"][0]["content"][0])
        self.assertNotIn("prompt_cache_options", request)
        self.assertNotIn("prompt_cache_key", request)

    def test_normalizes_inclusive_responses_usage(self) -> None:
        data = SimpleNamespace(
            input_tokens=100,
            output_tokens=12,
            total_tokens=112,
            input_tokens_details=SimpleNamespace(
                cached_tokens=40,
                cache_write_tokens=20,
            ),
        )

        chunk = self.make_model()._format_chunk(
            {"chunk_type": "metadata", "data": data}
        )
        usage = chunk["metadata"]["usage"]

        self.assertEqual(usage["inputTokens"], 40)
        self.assertEqual(usage["cacheReadInputTokens"], 40)
        self.assertEqual(usage["cacheWriteInputTokens"], 20)
        self.assertEqual(usage["outputTokens"], 12)

    def test_rejects_non_responses_cache_ttl(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires a 30m TTL"):
            self.module.BedrockRuntimeOpenAIResponsesModel(
                model_id="us.openai.gpt-5.6-luna",
                region="us-east-1",
                stateful=False,
                prompt_cache_enabled=True,
                prompt_cache_key_prefix="test-runtime",
                prompt_cache_ttl="5m",
            )


if __name__ == "__main__":
    unittest.main()

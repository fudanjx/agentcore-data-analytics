from __future__ import annotations

import importlib
import sys
import types
import unittest
from types import SimpleNamespace


class _FakeOpenAIResponsesModel:
    def __init__(self, client_args=None, **model_config):
        self.client_args = client_args or {}
        self.config = dict(model_config)

    def _format_request(
        self,
        messages,
        tool_specs=None,
        system_prompt=None,
        tool_choice=None,
        model_state=None,
    ):
        request = {
            "model": self.config["model_id"],
            "input": messages,
            "stream": True,
            **self.config.get("params", {}),
            "store": self.config.get("stateful", False),
        }
        if system_prompt:
            request["instructions"] = system_prompt
        return request

    def _format_chunk(self, event):
        if event.get("chunk_type") != "metadata":
            return {"event": event}
        usage = event["data"]
        cached = getattr(getattr(usage, "input_tokens_details", None), "cached_tokens", 0)
        return {
            "metadata": {
                "usage": {
                    "inputTokens": getattr(usage, "input_tokens", 0),
                    "outputTokens": getattr(usage, "output_tokens", 0),
                    "totalTokens": getattr(usage, "total_tokens", 0),
                    "cacheReadInputTokens": cached,
                }
            }
        }

    @classmethod
    def _format_request_message_content(cls, content, *, role="user"):
        return {
            "type": "output_text" if role == "assistant" else "input_text",
            "text": content["text"],
        }

    @classmethod
    def _format_request_message_tool_call(cls, tool_use):
        return {
            "type": "function_call",
            "call_id": tool_use["toolUseId"],
            "name": tool_use["name"],
            "arguments": "{}",
        }

    @classmethod
    def _format_request_tool_message(cls, tool_result):
        return {
            "type": "function_call_output",
            "call_id": tool_result["toolUseId"],
            "output": "tool output",
        }


def _load_module():
    strands = types.ModuleType("strands")
    models = types.ModuleType("strands.models")
    responses = types.ModuleType("strands.models.openai_responses")
    responses.OpenAIResponsesModel = _FakeOpenAIResponsesModel
    sys.modules["strands"] = strands
    sys.modules["strands.models"] = models
    sys.modules["strands.models.openai_responses"] = responses
    sys.modules.pop("deepseek_openai", None)
    return importlib.import_module("deepseek_openai")


class DeepSeekConfigurationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _load_module()

    def test_api_key_rejects_missing_and_placeholder_values(self):
        for value in (None, "", "   ", "PENDING_REPLACE_ME", "pending_replace_me"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "DEEPSEEK_API_KEY"):
                    self.module.validate_api_key(value)

    def test_api_key_accepts_configured_value_without_returning_or_logging_it(self):
        self.assertIsNone(self.module.validate_api_key("sk-test-value"))

    def test_reasoning_effort_accepts_only_supported_values(self):
        for value in ("low", "high", "max"):
            with self.subTest(value=value):
                self.assertEqual(self.module.validate_reasoning_effort(value), value)
        for value in ("", "medium", "xhigh", "none"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "DEEPSEEK_REASONING_EFFORT"):
                    self.module.validate_reasoning_effort(value)

    def test_max_output_tokens_is_bounded(self):
        self.assertEqual(self.module.validate_max_output_tokens("32768"), 32768)
        for value in ("0", "1023", "384001", "not-an-int"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "DEEPSEEK_MAX_OUTPUT_TOKENS"):
                    self.module.validate_max_output_tokens(value)

    def test_model_request_contains_reasoning_and_output_limits(self):
        model = self.module.DeepSeekResponsesModel(
            model_id="deepseek-v4-flash",
            api_key="sk-test-value",
            base_url="https://api.deepseek.com",
            reasoning_effort="high",
            max_output_tokens=32768,
            connect_timeout_seconds=10,
            read_timeout_seconds=900,
            max_retries=2,
        )

        request = model._format_request(
            [{"role": "user", "content": "hello"}],
            system_prompt="system guidance",
        )

        self.assertEqual(request["model"], "deepseek-v4-flash")
        self.assertEqual(request["reasoning"], {"effort": "high"})
        self.assertEqual(request["max_output_tokens"], 32768)
        self.assertFalse(request["store"])
        self.assertEqual(model.client_args["api_key"], "sk-test-value")
        self.assertEqual(model.client_args["base_url"], "https://api.deepseek.com")

    def test_reasoning_is_replayed_before_a_tool_call(self):
        model = self.module.DeepSeekResponsesModel(
            model_id="deepseek-v4-flash",
            api_key="sk-test-value",
            base_url="https://api.deepseek.com",
            reasoning_effort="high",
            max_output_tokens=32768,
            connect_timeout_seconds=10,
            read_timeout_seconds=900,
            max_retries=2,
        )
        formatted = model._format_request_messages(
            [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "reasoningContent": {
                                "reasoningText": {"text": "Need the database tool."}
                            }
                        },
                        {
                            "toolUse": {
                                "toolUseId": "call-1",
                                "name": "nuh_query",
                                "input": {},
                            }
                        },
                    ],
                }
            ]
        )

        self.assertEqual(formatted[0]["type"], "reasoning")
        self.assertEqual(
            formatted[0]["content"][0]["text"], "Need the database tool."
        )
        self.assertEqual(formatted[1]["type"], "function_call")


class DeepSeekUsageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _load_module()

    def test_usage_normalizes_cached_input_and_tracks_reasoning_tokens(self):
        model = self.module.DeepSeekResponsesModel(
            model_id="deepseek-v4-flash",
            api_key="sk-test-value",
            base_url="https://api.deepseek.com",
            reasoning_effort="high",
            max_output_tokens=32768,
            connect_timeout_seconds=10,
            read_timeout_seconds=900,
            max_retries=2,
        )
        usage = SimpleNamespace(
            input_tokens=1000,
            input_tokens_details=SimpleNamespace(cached_tokens=700),
            output_tokens=250,
            output_tokens_details=SimpleNamespace(reasoning_tokens=175),
            total_tokens=1250,
        )

        chunk = model._format_chunk({"chunk_type": "metadata", "data": usage})

        self.assertEqual(
            chunk["metadata"]["usage"],
            {
                "inputTokens": 300,
                "outputTokens": 250,
                "totalTokens": 1250,
                "cacheReadInputTokens": 700,
            },
        )
        self.assertEqual(model.reasoning_tokens, 175)

    def test_reasoning_tokens_accumulate_across_model_turns(self):
        model = self.module.DeepSeekResponsesModel(
            model_id="deepseek-v4-flash",
            api_key="sk-test-value",
            base_url="https://api.deepseek.com",
            reasoning_effort="high",
            max_output_tokens=32768,
            connect_timeout_seconds=10,
            read_timeout_seconds=900,
            max_retries=2,
        )
        for count in (50, 75):
            usage = SimpleNamespace(
                input_tokens=10,
                input_tokens_details=SimpleNamespace(cached_tokens=0),
                output_tokens=count,
                output_tokens_details=SimpleNamespace(reasoning_tokens=count),
                total_tokens=10 + count,
            )
            model._format_chunk({"chunk_type": "metadata", "data": usage})

        self.assertEqual(model.reasoning_tokens, 125)


if __name__ == "__main__":
    unittest.main()

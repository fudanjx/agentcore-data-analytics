from __future__ import annotations

import ast
import unittest
from pathlib import Path


RUNTIME_DIR = Path(__file__).resolve().parents[1]
AGENT_SOURCE = (RUNTIME_DIR / "agent.py").read_text(encoding="utf-8")


class AgentDeepSeekConfigurationTests(unittest.TestCase):
    def test_agent_uses_deepseek_adapter_without_bedrock_model_transport(self):
        self.assertIn("deepseek_openai.DeepSeekResponsesModel", AGENT_SOURCE)
        self.assertNotIn("BedrockModel", AGENT_SOURCE)
        self.assertNotIn("CacheConfig", AGENT_SOURCE)
        self.assertNotIn("BotocoreConfig", AGENT_SOURCE)

    def test_model_validation_precedes_managed_resource_acquisition(self):
        tree = ast.parse(AGENT_SOURCE)
        prepare = next(
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "_prepare"
        )
        prepare_source = ast.get_source_segment(AGENT_SOURCE, prepare)
        self.assertIsNotNone(prepare_source)
        self.assertLess(
            prepare_source.index("model = _make_model()"),
            prepare_source.index("memory.create_session_manager"),
        )
        self.assertLess(
            prepare_source.index("model = _make_model()"),
            prepare_source.index("code_interpreter.start_session"),
        )

    def test_usage_contract_includes_deepseek_cache_and_reasoning_fields(self):
        for expected in (
            '"provider": "deepseek"',
            '"cache_read_input_tokens"',
            '"reasoning_tokens"',
            '"reasoning_effort"',
            '"max_output_tokens"',
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, AGENT_SOURCE)


if __name__ == "__main__":
    unittest.main()

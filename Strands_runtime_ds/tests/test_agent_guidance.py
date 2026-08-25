"""Tests for stable Code Interpreter result-contract guidance."""

from __future__ import annotations

import importlib
import sys
import types
import unittest
from pathlib import Path


RUNTIME_DIR = Path(__file__).resolve().parents[1]
if str(RUNTIME_DIR) not in sys.path:
    sys.path.insert(0, str(RUNTIME_DIR))


def _tool_decorator(**_kwargs):
    def decorate(function):
        return function

    return decorate


fake_strands = types.ModuleType("strands")
fake_strands.tool = _tool_decorator
sys.modules.setdefault("strands", fake_strands)

import code_interpreter


class AgentGuidanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = importlib.reload(code_interpreter)

    def test_semantic_mode_guidance_contains_contract_without_request_data(self) -> None:
        self.module.RESULT_MODE = "semantic"
        guidance = self.module.system_guidance()

        self.assertIn("AGENTCORE_RESULT_JSON", guidance)
        self.assertIn("30", guidance)
        self.assertIn("20", guidance)
        self.assertIn("artifact", guidance.lower())
        self.assertIn("full dataframes", guidance)
        self.assertNotIn("s3://", guidance)
        self.assertNotIn("actor_id", guidance)
        self.assertNotIn("session_id", guidance)

    def test_legacy_mode_omits_semantic_guidance(self) -> None:
        self.module.RESULT_MODE = "legacy"
        self.assertEqual(self.module.system_guidance(), "")

    def test_agent_preparation_consumes_stable_guidance(self) -> None:
        agent_source = (RUNTIME_DIR / "agent.py").read_text(encoding="utf-8")
        self.assertIn("code_interpreter.system_guidance()", agent_source)


if __name__ == "__main__":
    unittest.main()

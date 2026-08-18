"""Wrapper tests for Code Interpreter semantic and legacy result modes."""

from __future__ import annotations

import asyncio
import importlib
import json
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


class _FakeClient:
    def __init__(self, stream=None, error: Exception | None = None) -> None:
        self.stream = stream or []
        self.error = error

    def invoke_code_interpreter(self, **_kwargs):
        if self.error is not None:
            raise self.error
        return {"stream": iter(self.stream)}


class CodeInterpreterWrapperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = importlib.reload(code_interpreter)
        self.module.CODE_INTERPRETER_ID = "test-code-interpreter"

    def test_semantic_mode_returns_declared_contract(self) -> None:
        self.module.RESULT_MODE = "semantic"
        self.module.SEMANTIC_MAX_RESULT_CHARS = 10_000
        contract = {"ok": True, "summary": "Completed compactly."}
        self.module.get_client = lambda: _FakeClient(
            [{"result": {"content": [{"text": "AGENTCORE_RESULT_JSON=" + json.dumps(contract)}]}}]
        )

        rendered = self.module._invoke_and_collect("session", "executeCode", {})
        payload = json.loads(rendered)

        self.assertEqual(payload["source"], "declared")
        self.assertTrue(payload["ok"])
        self.assertFalse(self.module._tool_result_is_error(rendered))

    def test_legacy_mode_returns_raw_event_list(self) -> None:
        self.module.RESULT_MODE = "legacy"
        self.module.MAX_RESULT_CHARS = 10_000
        self.module.get_client = lambda: _FakeClient(
            [{"result": {"content": [{"text": "legacy output"}]}}]
        )

        rendered = self.module._invoke_and_collect("session", "executeCode", {})
        self.assertIsInstance(json.loads(rendered), list)

    def test_semantic_runtime_error_is_bounded_json(self) -> None:
        self.module.RESULT_MODE = "semantic"
        self.module.SEMANTIC_MAX_RESULT_CHARS = 300
        self.module.get_client = lambda: _FakeClient(error=RuntimeError("connection failed"))

        rendered = asyncio.run(self.module._invoke_tool("session", "executeCode", {}))
        payload = json.loads(rendered)

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["source"], "runtime_error")
        self.assertIn("connection failed", payload["error"])

    def test_semantic_success_and_failure_are_both_detected(self) -> None:
        self.assertFalse(
            self.module._tool_result_is_error(
                json.dumps({"contract_version": 1, "ok": True, "summary": "done"})
            )
        )
        self.assertTrue(
            self.module._tool_result_is_error(
                json.dumps({"contract_version": 1, "ok": False, "summary": "failed"})
            )
        )


if __name__ == "__main__":
    unittest.main()

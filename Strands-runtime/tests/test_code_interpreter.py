"""Wrapper tests for Code Interpreter semantic and legacy result modes."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


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

    def test_semantic_call_emits_safe_contract_shape_log(self) -> None:
        self.module.RESULT_MODE = "semantic"
        self.module.SEMANTIC_MAX_RESULT_CHARS = 10_000
        sensitive_summary = "Do not log this patient-specific result."
        self.module.get_client = lambda: _FakeClient(
            [
                {
                    "result": {
                        "content": [
                            {
                                "text": "AGENTCORE_RESULT_JSON="
                                + json.dumps(
                                    {
                                        "ok": True,
                                        "summary": sensitive_summary,
                                        "artifacts": [
                                            {
                                                "s3_uri": "s3://private/report.html",
                                                "filename": "report.html",
                                            }
                                        ],
                                    }
                                )
                            }
                        ]
                    }
                }
            ]
        )

        with self.assertLogs("code_interpreter", level="INFO") as captured:
            rendered = asyncio.run(self.module._invoke_tool("session", "executeCode", {}))

        record = next(
            message
            for message in captured.output
            if "CODE_INTERPRETER_RESULT" in message
        )
        payload = json.loads(record.split("CODE_INTERPRETER_RESULT ", 1)[1])
        self.assertEqual(json.loads(rendered)["source"], "declared")
        self.assertEqual(payload["tool"], "executeCode")
        self.assertEqual(payload["source"], "declared")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["artifact_count"], 1)
        self.assertGreaterEqual(payload["duration_ms"], 0)
        self.assertNotIn(sensitive_summary, record)
        self.assertNotIn("s3://private/report.html", record)

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

    def test_encrypted_document_download_accepts_only_s3_or_https(self) -> None:
        https = self.module._document_download_command(
            "https://files.example.test/document", "/tmp/document.encrypted"
        )
        s3 = self.module._document_download_command(
            "s3://private-bucket/document", "/tmp/document.encrypted"
        )

        self.assertIn("curl --fail", https)
        self.assertIn("aws s3 cp", s3)
        with self.assertRaises(ValueError):
            self.module._document_download_command(
                "http://169.254.169.254/latest/meta-data", "/tmp/document.encrypted"
            )

    def test_encrypted_document_decryption_code_validates_envelope(self) -> None:
        code = self.module._document_decryption_code(
            "/tmp/source.encrypted", "/tmp/contract.pdf", b"k" * 32
        )

        self.assertIn("b'CLARAENC'", code)
        self.assertIn("AESGCM(key).decrypt", code)
        self.assertIn("raw[21:]", code)
        self.assertIn("os.remove(source)", code)
        self.assertNotIn((b"k" * 32).decode("ascii"), code)

    def test_decryption_tool_requires_private_key_configuration(self) -> None:
        metadata = [
            {
                "stored_name": "clara-123-contract.pdf",
                "original_name": "contract.pdf",
                "wrapped_key": "wrapped",
            }
        ]
        with patch.dict(os.environ, {"CLARA_FILE_DECRYPTION_PRIVATE_KEY": ""}):
            disabled_tools = self.module.build_tools(
                "session", encrypted_documents=metadata
            )
            self.assertFalse(self.module.document_decryption_enabled())

        with patch.dict(
            os.environ, {"CLARA_FILE_DECRYPTION_PRIVATE_KEY": "configured"}
        ):
            enabled_tools = self.module.build_tools(
                "session", encrypted_documents=metadata
            )
            self.assertTrue(self.module.document_decryption_enabled())

        self.assertNotIn(
            "stage_encrypted_document", [tool.__name__ for tool in disabled_tools]
        )
        self.assertIn(
            "stage_encrypted_document", [tool.__name__ for tool in enabled_tools]
        )


if __name__ == "__main__":
    unittest.main()

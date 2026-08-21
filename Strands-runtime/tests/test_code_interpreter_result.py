"""Unit tests for the bounded AgentCore Code Interpreter result contract."""

from __future__ import annotations

import json
import math
import sys
import unittest
from pathlib import Path


RUNTIME_DIR = Path(__file__).resolve().parents[1]
if str(RUNTIME_DIR) not in sys.path:
    sys.path.insert(0, str(RUNTIME_DIR))

import code_interpreter_result as result_contract


def _event(
    text: str = "",
    *,
    is_error: bool = False,
    exit_code: int | None = None,
) -> dict:
    result: dict = {"content": [{"text": text}]}
    if is_error:
        result["isError"] = True
    if exit_code is not None:
        result["structuredContent"] = {"exitCode": exit_code}
    return {"result": result}


class SemanticResultTests(unittest.TestCase):
    def render(self, events: list[dict], max_chars: int = 10_000) -> dict:
        rendered = result_contract.render_semantic_events(events, max_chars=max_chars)
        self.assertLessEqual(len(rendered), max_chars)
        return json.loads(rendered)

    def test_declared_contract_is_preserved_without_raw_event_noise(self) -> None:
        contract = {
            "ok": True,
            "summary": "Aggregated monthly visits by clinical department.",
            "row_count": 18_420,
            "columns": ["month", "department", "visit_count"],
            "metrics": {"period": "2023-01 to 2026-06", "departments": 18},
            "sample_rows": [
                {
                    "month": "2026-06",
                    "department": "Cardiology",
                    "visit_count": 245,
                }
            ],
            "artifacts": [
                {
                    "s3_uri": "s3://example/outputs/report.html",
                    "filename": "report.html",
                    "content_type": "text/html",
                }
            ],
            "warnings": [],
            "ignored": "must not be returned",
        }
        payload = self.render([_event("starting\nAGENTCORE_RESULT_JSON=" + json.dumps(contract))])

        self.assertEqual(payload["contract_version"], 1)
        self.assertEqual(payload["source"], "declared")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["row_count"], 18_420)
        self.assertEqual(payload["sample_rows"][0]["department"], "Cardiology")
        self.assertNotIn("ignored", payload)
        self.assertNotIn("AGENTCORE_RESULT_JSON", json.dumps(payload))

    def test_last_valid_marker_wins_when_marker_is_split_between_events(self) -> None:
        first = {"ok": True, "summary": "first"}
        final = {"ok": True, "summary": "final"}
        events = [
            _event("AGENTCORE_RESULT_JSON=" + json.dumps(first) + "\npartial "),
            _event("AGENTCORE_RESULT_JSON={\"ok\":true,"),
            _event("\"summary\":\"final\"}\n"),
        ]
        payload = self.render(events)
        self.assertEqual(payload["source"], "declared")
        self.assertEqual(payload["summary"], final["summary"])

    def test_missing_or_invalid_marker_uses_bounded_fallback_preview(self) -> None:
        huge = "head-" + ("x" * 100_000) + "-tail"
        payload = self.render([_event(huge + "\nAGENTCORE_RESULT_JSON={not-json}")])

        self.assertEqual(payload["source"], "fallback")
        self.assertTrue(payload["ok"])
        self.assertIn("did not emit", payload["warnings"][0])
        self.assertIn("head-", payload["stdout_preview"])
        self.assertIn("-tail", payload["stdout_preview"])

    def test_error_event_has_actionable_bounded_error_contract(self) -> None:
        payload = self.render(
            [_event("permission denied", is_error=True, exit_code=1)]
        )

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["exit_code"], 1)
        self.assertIn("failed", payload["summary"].lower())
        self.assertIn("permission denied", payload["error"])
        self.assertTrue(result_contract.result_is_error(json.dumps(payload)))

    def test_declared_contract_is_sanitized_and_bounded(self) -> None:
        contract = {
            "ok": True,
            "summary": "summary " * 1_000,
            "row_count": 123,
            "columns": [f"column_{number}" for number in range(40)],
            "metrics": {
                f"metric_{number}": (math.nan if number == 0 else number)
                for number in range(30)
            },
            "sample_rows": [
                {f"column_{number}": number for number in range(30)}
                for _ in range(40)
            ],
            "artifacts": [
                {
                    "s3_uri": f"s3://bucket/output-{number}.html",
                    "filename": f"output-{number}.html",
                    "content_type": "text/html",
                    "untrusted": "removed",
                }
                for number in range(30)
            ],
            "warnings": [f"warning {number}" for number in range(30)],
        }
        payload = self.render(
            [_event("AGENTCORE_RESULT_JSON=" + json.dumps(contract, allow_nan=True))],
            max_chars=2_000,
        )

        self.assertLessEqual(len(payload["columns"]), 20)
        self.assertLessEqual(len(payload["metrics"]), 20)
        self.assertLessEqual(len(payload["sample_rows"]), 30)
        self.assertLessEqual(len(payload["sample_rows"][0]), 20)
        self.assertLessEqual(len(payload["artifacts"]), 20)
        self.assertLessEqual(len(payload["warnings"]), 20)
        self.assertNotIn("untrusted", payload["artifacts"][0])
        self.assertFalse(any(math.isnan(value) for value in payload["metrics"].values() if isinstance(value, float)))

    def test_tiny_limit_keeps_required_fields(self) -> None:
        payload = self.render(
            [_event("AGENTCORE_RESULT_JSON=" + json.dumps({"ok": True, "summary": "a" * 1_000}))],
            max_chars=120,
        )

        self.assertEqual(payload["contract_version"], 1)
        self.assertIn(payload["source"], {"declared", "fallback"})
        self.assertTrue(payload["ok"])
        self.assertIn("summary", payload)

    def test_legacy_rendering_and_error_detection_remain_compatible(self) -> None:
        events = [_event("hello"), _event("failed", is_error=True, exit_code=2)]
        rendered = result_contract.render_legacy_events(events, max_chars=10_000)
        parsed = json.loads(rendered)

        self.assertIsInstance(parsed, list)
        self.assertEqual(len(parsed), 2)
        self.assertTrue(result_contract.result_is_error(rendered))

    def test_result_metadata_is_useful_without_retaining_contract_values(self) -> None:
        summary = "Patient-level detail must never enter logs."
        s3_uri = "s3://private-bucket/outputs/secret-report.html"
        rendered = result_contract.render_semantic_events(
            [
                _event(
                    "AGENTCORE_RESULT_JSON="
                    + json.dumps(
                        {
                            "ok": True,
                            "summary": summary,
                            "columns": ["patient_id", "visit_count"],
                            "metrics": {"patients": 3},
                            "sample_rows": [{"patient_id": "P123", "visit_count": 2}],
                            "artifacts": [{"s3_uri": s3_uri, "filename": "secret-report.html"}],
                            "warnings": ["sensitive warning"],
                        }
                    )
                )
            ]
        )

        metadata = result_contract.result_metadata(
            rendered, mode="semantic", max_chars=10_000
        )
        serialized = json.dumps(metadata)

        self.assertEqual(metadata["source"], "declared")
        self.assertTrue(metadata["ok"])
        self.assertEqual(metadata["column_count"], 2)
        self.assertEqual(metadata["metric_count"], 1)
        self.assertEqual(metadata["sample_row_count"], 1)
        self.assertEqual(metadata["artifact_count"], 1)
        self.assertNotIn(summary, serialized)
        self.assertNotIn(s3_uri, serialized)
        self.assertNotIn("P123", serialized)

    def test_legacy_result_metadata_does_not_retain_event_content(self) -> None:
        rendered = result_contract.render_legacy_events(
            [_event("private raw output")], max_chars=10_000
        )

        metadata = result_contract.result_metadata(
            rendered, mode="legacy", max_chars=10_000
        )

        self.assertEqual(metadata["source"], "legacy")
        self.assertTrue(metadata["ok"])
        self.assertEqual(metadata["event_count"], 1)
        self.assertNotIn("private raw output", json.dumps(metadata))


if __name__ == "__main__":
    unittest.main()

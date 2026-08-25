"""Focused tests for bounded direct S3 Tables results and exports."""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import handler  # noqa: E402


def _row(*values):
    return {"Data": [{"VarCharValue": value} for value in values]}


class _Paginator:
    def __init__(self, pages):
        self.pages = pages

    def paginate(self, **kwargs):
        return iter(self.pages)


class HandlerTests(unittest.TestCase):
    def setUp(self):
        self.athena = MagicMock()
        self.athena.start_query_execution.return_value = {"QueryExecutionId": "qid-123"}
        self.athena.get_query_execution.return_value = {"QueryExecution": {
            "Status": {"State": "SUCCEEDED"},
            "ResultConfiguration": {"OutputLocation": "s3://agentcore-tmp-964340114883/athena-results/qid-123.csv"},
            "Statistics": {"DataScannedInBytes": 123, "EngineExecutionTimeInMillis": 45},
        }}

    def _set_rows(self, rows):
        self.athena.get_paginator.return_value = _Paginator([{"ResultSet": {"Rows": [_row("month", "visits"), *rows]}}])

    def test_small_direct_result_is_returned(self):
        self._set_rows([_row("2026-01", "10"), _row("2026-02", "12")])
        with patch.object(handler, "athena", self.athena):
            result = handler.execute_sql({"query": "SELECT month, visits FROM soc", "source": "nuh"})
        self.assertEqual(result, [{"month": "2026-01", "visits": "10"}, {"month": "2026-02", "visits": "12"}])
        self.assertEqual(self.athena.start_query_execution.call_args.kwargs["WorkGroup"], handler.SOURCES["nuh"]["workgroup"])

    def test_direct_result_over_limit_fails_closed(self):
        self._set_rows([_row(str(index), "1") for index in range(handler.MAX_DIRECT_ROWS + 1)])
        with patch.object(handler, "athena", self.athena):
            with self.assertRaisesRegex(ValueError, "Use execute_sql_export"):
                handler.execute_sql({"query": "SELECT month, visits FROM soc"})

    def test_export_returns_metadata_not_rows(self):
        with patch.object(handler, "athena", self.athena):
            result = handler.execute_sql_export({"query": "WITH x AS (SELECT 1) SELECT * FROM x", "source": "ah", "export": True})
        self.assertEqual(result["query_execution_id"], "qid-123")
        self.assertEqual(result["source"], "ah")
        self.assertEqual(result["result_s3_uri"], "s3://agentcore-tmp-964340114883/athena-results/qid-123.csv")
        self.assertNotIn("rows", result)
        self.athena.get_paginator.assert_not_called()

    def test_export_marker_routes_to_export_tool(self):
        self.assertEqual(handler._infer_tool({"query": "SELECT 1", "export": True}), "execute_sql_export")


if __name__ == "__main__":
    unittest.main()

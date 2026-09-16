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
        self.s3tables = MagicMock()
        self.athena.start_query_execution.return_value = {"QueryExecutionId": "qid-123"}
        self.athena.get_query_execution.return_value = {"QueryExecution": {
            "Status": {"State": "SUCCEEDED"},
            "ResultConfiguration": {"OutputLocation": "s3://agentcore-tmp-964340114883/athena-results/qid-123.csv"},
            "Statistics": {"DataScannedInBytes": 123, "EngineExecutionTimeInMillis": 45},
        }}
        self.s3tables.list_namespaces.return_value = {
            "namespaces": [{"namespace": ["nuh"]}],
        }
        self.athena_patch = patch.object(handler, "athena", self.athena)
        self.s3tables_patch = patch.object(handler, "s3tables", self.s3tables)
        self.athena_patch.start()
        self.s3tables_patch.start()
        self.addCleanup(self.athena_patch.stop)
        self.addCleanup(self.s3tables_patch.stop)
        handler._account_id_cache = "964340114883"

    def _set_rows(self, rows):
        self.athena.get_paginator.return_value = _Paginator([{"ResultSet": {"Rows": [_row("month", "visits"), *rows]}}])

    def test_small_direct_result_is_returned(self):
        self._set_rows([_row("2026-01", "10"), _row("2026-02", "12")])
        result = handler.execute_sql({
            "query": "SELECT month, visits FROM soc",
            "s3_bucket_name": "nuh-analytics",
        })
        self.assertEqual(result, [{"month": "2026-01", "visits": "10"}, {"month": "2026-02", "visits": "12"}])
        call = self.athena.start_query_execution.call_args.kwargs
        self.assertEqual(call["WorkGroup"], handler.ATHENA_WORKGROUP)
        self.assertEqual(call["QueryExecutionContext"], {
            "Catalog": "s3tablescatalog/nuh-analytics",
            "Database": "nuh",
        })
        self.s3tables.list_namespaces.assert_called_once_with(
            tableBucketARN="arn:aws:s3tables:ap-southeast-1:964340114883:bucket/nuh-analytics"
        )

    def test_direct_result_over_limit_fails_closed(self):
        self._set_rows([_row(str(index), "1") for index in range(handler.MAX_DIRECT_ROWS + 1)])
        with self.assertRaisesRegex(ValueError, "Use execute_sql_export"):
            handler.execute_sql({
                "query": "SELECT month, visits FROM soc",
                "s3_bucket_name": "nuh-analytics",
            })

    def test_export_returns_metadata_not_rows(self):
        result = handler.execute_sql_export({
            "query": "WITH x AS (SELECT 1) SELECT * FROM x",
            "s3_bucket_name": "nuh-analytics",
            "export": True,
        })
        self.assertEqual(result["query_execution_id"], "qid-123")
        self.assertEqual(result["s3_bucket_name"], "nuh-analytics")
        self.assertEqual(result["namespace"], "nuh")
        self.assertEqual(result["result_s3_uri"], "s3://agentcore-tmp-964340114883/athena-results/qid-123.csv")
        self.assertNotIn("rows", result)
        self.athena.get_paginator.assert_not_called()

    def test_bucket_with_multiple_namespaces_is_rejected_as_ambiguous(self):
        self.s3tables.list_namespaces.return_value = {
            "namespaces": [
                {"namespace": ["finance"]},
                {"namespace": ["operations"]},
            ],
        }
        with self.assertRaisesRegex(ValueError, "multiple namespaces"):
            handler.execute_sql({
                "query": "SELECT 1",
                "s3_bucket_name": "shared-analytics",
            })
        self.athena.start_query_execution.assert_not_called()

    def test_bucket_name_is_required(self):
        with self.assertRaisesRegex(ValueError, "s3_bucket_name is required"):
            handler.execute_sql({"query": "SELECT 1"})
        self.s3tables.list_namespaces.assert_not_called()

    def test_export_marker_routes_to_export_tool(self):
        self.assertEqual(handler._infer_tool({"query": "SELECT 1", "export": True}), "execute_sql_export")


if __name__ == "__main__":
    unittest.main()

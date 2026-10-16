"""Exercise Glue helpers without importing the AWS Glue/Spark entry point."""
import ast
from pathlib import Path
import unittest
from unittest.mock import MagicMock


SCRIPT = Path(__file__).parents[1] / "glue_job.py"


def helpers(*names):
    tree = ast.parse(SCRIPT.read_text())
    selected = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    namespace = {}
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(SCRIPT), "exec"), namespace)
    return namespace


class SnapshotStateTests(unittest.TestCase):
    def setUp(self):
        self.ns = helpers("_snapshot_state", "_fresh_snapshot_state", "_run_ingestion")
        self.spark = MagicMock()
        self.table = self.spark._jvm.org.apache.iceberg.spark.Spark3Util.loadIcebergTable.return_value
        self.ns.update(spark=self.spark, F=MagicMock(), TARGET="catalog.nuh.sc")
        # Rollback retains the newer append snapshot in snapshot history.
        latest = {"snapshot_id": 2, "summary": {"total-records": "20232"}}
        self.spark.table.return_value.orderBy.return_value.select.return_value.first.return_value = latest
        self.current(1, 11180)

    def current(self, snapshot_id, rows):
        snapshot = MagicMock()
        snapshot.snapshotId.return_value = snapshot_id
        snapshot.summary.return_value = {"total-records": str(rows)}
        self.table.currentSnapshot.return_value = snapshot

    def test_rollback_uses_current_snapshot_instead_of_newest_retained_snapshot(self):
        self.assertEqual(self.ns["_snapshot_state"](), ("1", 11180))
        self.table.refresh.assert_called_once()

    def test_reupload_after_rollback_reconciles_and_records_current_baseline(self):
        manifest = {
            "schema": [{"name": "id", "type": "STRING"}],
            "prepared_contract_types": True, "deduplication_mode": "none",
            "incoming_row_count": 9052, "prepared_row_count": 9052,
        }
        incoming = MagicMock()

        def append():
            self.current(3, 20232)
            self.spark.table.return_value.orderBy.return_value.select.return_value.first.return_value = {
                "snapshot_id": 3, "summary": {"total-records": "20232"},
            }

        incoming.writeTo.return_value.append.side_effect = append
        record = MagicMock(return_value={"event_id": "event"})
        import json
        import time
        self.ns.update(
            MODE="append", ARGS={"MANIFEST_URI": "manifest"}, json=json, time=time,
            _read_json=lambda uri: manifest, _exists=lambda: True,
            _project_incoming=lambda *args, **kwargs: (incoming, 0), _record_event=record,
        )
        result = self.ns["_run_ingestion"]()
        self.assertEqual(result["target_before_rows"], 11180)
        self.assertEqual(result["target_after_rows"], 20232)
        self.assertEqual(result["previous_snapshot_id"], "1")
        self.assertEqual(result["new_snapshot_id"], "3")
        incoming.writeTo.return_value.append.assert_called_once()
        self.assertEqual(record.call_args.kwargs["status"], "SUCCESS")
        self.assertEqual(record.call_args.kwargs["previous_snapshot_id"], "1")

    def test_empty_table_has_no_current_snapshot(self):
        self.table.currentSnapshot.return_value = None
        self.assertEqual(self.ns["_snapshot_state"](), (None, 0))

    def test_missing_current_row_metric_fails_closed(self):
        self.table.currentSnapshot.return_value.summary.return_value = {}
        with self.assertRaisesRegex(ValueError, "total-records"):
            self.ns["_snapshot_state"]()


if __name__ == "__main__":
    unittest.main()

import tempfile
import unittest
import io
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from s3tables_uploader_v2.worker_analysis import raw_key_impact_metrics
from s3tables_uploader_v2.worker import process_compat_work
from s3tables_uploader_v2.config import WorkerSettings
from s3tables_uploader_v2.job_store import S3JobStore


class FakeS3:
    def __init__(self): self.items = {}
    def put_object(self, Bucket, Key, Body, **kwargs):
        self.items[Key] = bytes(Body); return {"ETag": "etag"}
    def get_object(self, Bucket, Key): return {"Body": io.BytesIO(self.items[Key]), "ETag": "etag"}
    def download_file(self, Bucket, Key, Filename, ExtraArgs=None): Path(Filename).write_bytes(self.items[Key])


class WorkerAnalysisTests(unittest.TestCase):
    def test_key_impact_uses_v1_exact_duplicate_and_conflict_semantics(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.parquet"
            pq.write_table(pa.table({"key": ["a", "a", "b", "b"], "value": ["x", "x", "x", "y"]}), source)
            result = raw_key_impact_metrics([(source, "source.parquet")], ["key"])
        self.assertEqual(result, {
            "incoming_rows": 4, "unique_composite_keys": 2, "exact_duplicate_rows": 1,
            "conflicting_key_groups": 1, "rows_in_conflicting_key_groups": 2,
            "expected_retained_rows": 1, "expected_skipped_rows": 3,
        })

    def test_key_work_updates_durable_session_with_real_metrics(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.parquet"
            pq.write_table(pa.table({"key": ["a", "a", "b", "b"], "value": ["x", "x", "x", "y"]}), source)
            s3 = FakeS3(); s3.items["landing/raw.parquet"] = source.read_bytes()
            store = S3JobStore(s3, "landing", "prefix")
            store.put_compat_session({
                "session_id": "session", "owner_user_id": "user", "phase": "KEY_ANALYSING", "mode": "create",
                "table_bucket_arn": "arn", "namespace": "pilot", "table": "target", "files": [{"name": "source.parquet", "sha256": "x", "source_key": "landing/raw.parquet"}],
                "key_analysis_request": {"deduplication_columns": ["key"], "type_overrides": {}},
            })
            settings = WorkerSettings(region="ap-southeast-1", landing_bucket="landing", landing_prefix="prefix", glue_job_name="job", contract_bucket="contracts", contract_prefix="contracts")
            process_compat_work("key:session", settings, s3)
            session = store.get_compat_session("session")
        self.assertEqual(session["phase"], "READY_FOR_ACKNOWLEDGEMENT")
        self.assertEqual(session["key_impact"]["metrics"]["incoming_rows"], 4)
        self.assertEqual(session["key_impact"]["metrics"]["conflicting_key_groups"], 1)

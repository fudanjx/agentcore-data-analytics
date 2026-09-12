import tempfile
import unittest
import io
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from s3tables_uploader_v2.worker_analysis import profile_files, raw_key_impact_metrics
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
    def test_profile_rejects_multi_file_column_or_type_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first.parquet"
            second = Path(directory) / "second.parquet"
            pq.write_table(pa.table({"id": pa.array([1], type=pa.int64()), "value": ["a"]}), first)
            pq.write_table(pa.table({"id": pa.array([2], type=pa.int64()), "other": ["b"]}), second)
            result = profile_files(
                [(first, "first.parquet", "first"), (second, "second.parquet", "second")],
                "create", "arn", "pilot", "target",
            )
        self.assertFalse(result["accepted"])
        self.assertTrue(result["multi_file_schema"]["enforced"])
        self.assertIn("multi-file upload column mismatch", result["files"][1]["rejection_reasons"][0])

    def test_profile_accepts_multi_file_matching_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first.parquet"
            second = Path(directory) / "second.parquet"
            pq.write_table(pa.table({"id": pa.array([1], type=pa.int64()), "value": ["a"]}), first)
            pq.write_table(pa.table({"id": pa.array([2], type=pa.int64()), "value": ["b"]}), second)
            result = profile_files(
                [(first, "first.parquet", "first"), (second, "second.parquet", "second")],
                "create", "arn", "pilot", "target",
            )
        self.assertTrue(result["accepted"])
        self.assertEqual(result["multi_file_schema"]["reference_file"], "first.parquet")

    def test_profile_accepts_cross_file_type_drift_and_uses_string_for_new_table(self):
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first.parquet"
            second = Path(directory) / "second.parquet"
            pq.write_table(pa.table({"event_time": pa.array([1_726_000_000_000_000], type=pa.timestamp("us")), "id": [1]}), first)
            pq.write_table(pa.table({"event_time": pa.array(["2024-09-01"], type=pa.large_string()), "id": [2]}), second)
            result = profile_files(
                [(first, "first.parquet", "first"), (second, "second.parquet", "second")],
                "create", "arn", "pilot", "target",
            )
        self.assertTrue(result["accepted"])
        self.assertEqual(next(field["type"] for field in result["target_schema"] if field["name"] == "event_time"), "STRING")
        self.assertEqual(result["multi_file_schema"]["type_conflicts_stored_as_string"]["event_time"], ["large_string", "timestamp[us]"])

    def test_profile_derives_the_available_subset_of_the_locked_key_without_candidates(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.parquet"
            pq.write_table(pa.table({"c": ["1"], "d": ["2"], "f": ["3"], "value": ["x"]}), source)
            result = profile_files(
                [(source, "source.parquet", "digest")], "append", "arn", "pilot", "target",
                {"schema": [{"name": "c", "type": "STRING"}, {"name": "d", "type": "STRING"}, {"name": "f", "type": "STRING"}, {"name": "value", "type": "STRING"}],
                 "deduplication_columns": ["a", "b", "c", "d", "f"]},
            )
        self.assertEqual(result["deduplication_locked_columns"], ["a", "b", "c", "d", "f"])
        self.assertEqual(result["deduplication_columns"], ["c", "d", "f"])
        self.assertEqual(result["deduplication_candidates"], [])

    def test_profile_shows_safe_examples_and_masks_protected_columns(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.parquet"
            pq.write_table(pa.table({
                "description": ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot"],
                "PAT_ENC_CSN_ID": ["100", "101", "102", "103", "104", "105"],
            }), source)
            result = profile_files(
                [(source, "source.parquet", "digest")], "create", "arn", "pilot", "target",
            )

        candidates = {item["column"]: item for item in result["deduplication_candidates"]}
        safe = candidates["description"]
        self.assertFalse(safe["samples_masked"])
        self.assertEqual(safe["non_null_count"], 6)
        self.assertEqual(len(safe["sample_values"]), 5)
        self.assertTrue(set(safe["sample_values"]).issubset({"alpha", "bravo", "charlie", "delta", "echo", "foxtrot"}))

        protected = candidates["pat_enc_csn_id"]
        self.assertTrue(protected["samples_masked"])
        self.assertEqual(protected["sample_values"], [])
        self.assertIsNone(protected["non_null_count"])

        manual = {item["column"]: item for item in result["sanitization_review"]["manual_encryption_candidates"]}
        self.assertIn("description", manual)
        self.assertEqual(manual["description"]["sample_values"], safe["sample_values"])
        self.assertNotIn("pat_enc_csn_id", manual)

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

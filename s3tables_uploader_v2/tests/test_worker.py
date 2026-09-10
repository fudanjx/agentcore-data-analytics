import tempfile
import unittest
from unittest.mock import patch
from datetime import time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pandas as pd

from s3tables_uploader_v2.worker import _history_prefix, _iceberg_type, _save_lease, _write_create_contract, _write_prepared_parquet, process_job
from s3tables_uploader_v2.config import WorkerSettings
from s3tables_uploader_v2.models import Destination, JobRequest, JobSource
from s3tables_uploader_v2.job_store import S3JobStore


class WorkerTests(unittest.TestCase):
    def test_worker_heartbeat_preserves_a_newer_api_lease_binding(self):
        class FakeS3:
            def __init__(self): self.items = {}
            def put_object(self, Bucket, Key, Body, **kwargs):
                self.items[Key] = bytes(Body)
                return {"ETag": "etag"}
            def get_object(self, Bucket, Key):
                import io
                return {"Body": io.BytesIO(self.items[Key]), "ETag": "etag"}

        store = S3JobStore(FakeS3(), "landing", "prefix")
        lease = {
            "lease_id": "lease", "owner_user_id": "owner", "state": "AWAITING_UPLOAD",
            "worker_size": "BASE", "session_id": None, "expires_at": "2026-09-10T00:10:00+00:00",
        }
        store.put_lease(lease, create_only=True)
        stale_worker_copy = store.get_lease("lease")
        store.update_lease("lease", {"session_id": "new-session", "expires_at": "2026-09-10T00:30:00+00:00"})

        _save_lease(store, stale_worker_copy, state="AWAITING_UPLOAD", message="Worker heartbeat")

        restored = store.get_lease("lease")
        self.assertEqual(restored["session_id"], "new-session")
        self.assertEqual(restored["expires_at"], "2026-09-10T00:30:00+00:00")
        self.assertEqual(restored["state_version"], 2)

    def test_worker_prepares_matching_multi_file_session_into_one_manifest(self):
        class FakeS3:
            def __init__(self): self.items = {}
            def put_object(self, Bucket, Key, Body, **kwargs): self.items[Key] = bytes(Body); return {"ETag": "etag"}
            def get_object(self, Bucket, Key):
                import io
                return {"Body": io.BytesIO(self.items[Key]), "ETag": "etag"}
            def download_file(self, Bucket, Key, Filename, ExtraArgs=None): Path(Filename).write_bytes(self.items[Key])
            def upload_file(self, Filename, Bucket, Key, ExtraArgs=None): self.items[Key] = Path(Filename).read_bytes()
        class FakeGlue:
            def __init__(self): self.calls = []
            def start_job_run(self, **kwargs): self.calls.append(kwargs); return {"JobRunId": "jr-multiple"}

        with tempfile.TemporaryDirectory() as directory:
            first, second = Path(directory) / "first.parquet", Path(directory) / "second.parquet"
            pq.write_table(pa.table({"id": [1], "value": ["first"]}), first)
            pq.write_table(pa.table({"id": [2], "value": ["second"]}), second)
            s3, glue = FakeS3(), FakeGlue()
            s3.items["prefix/uploads/session/raw/first.parquet"] = first.read_bytes()
            s3.items["prefix/uploads/session/raw/second.parquet"] = second.read_bytes()
            settings = WorkerSettings(region="ap-southeast-1", landing_bucket="landing", landing_prefix="prefix", glue_job_name="job", contract_bucket="contracts", contract_prefix="contracts")
            store = S3JobStore(s3, "landing", "prefix")
            store.put_compat_session({
                "session_id": "session", "table_bucket_arn": "arn", "namespace": "ah", "table": "target",
                "preflight": {"table_bucket_arn": "arn", "namespace": "ah", "table": "target", "target_schema": [{"name": "id", "type": "BIGINT"}, {"name": "value", "type": "STRING"}], "files": [{}, {}]},
            })
            request = JobRequest(
                job_id="job", session_id="session", owner_user_id="owner", operation="create",
                destination=Destination(table_bucket_arn="arn", namespace="ah", table="target"),
                source_key="prefix/uploads/session/raw/first.parquet", source_version_id="version-1", source_size_bytes=1,
                source_files=[
                    JobSource(name="first.parquet", source_key="prefix/uploads/session/raw/first.parquet", source_version_id="version-1", source_size_bytes=1),
                    JobSource(name="second.parquet", source_key="prefix/uploads/session/raw/second.parquet", source_version_id="version-2", source_size_bytes=1),
                ],
            )
            store.put_request(request)
            with patch("s3tables_uploader_v2.worker.encryption_key", return_value=b"x" * 32):
                process_job("job", settings, s3, glue)
            import json
            manifest = json.loads(s3.items["prefix/jobs/job/prepared/manifest.json"])
        self.assertEqual(len(manifest["files"]), 2)
        self.assertEqual(manifest["prepared_row_count"], 2)
        self.assertEqual(glue.calls[0]["Arguments"]["--FILENAMES_JSON"], '["first.parquet", "second.parquet"]')
        self.assertEqual(glue.calls[0]["Arguments"]["--LOCK_BUCKET"], "landing")
        self.assertTrue(glue.calls[0]["Arguments"]["--LOCK_KEY"].startswith("prefix/table-locks/"))

    def test_worker_uses_v1_per_table_history_prefix(self):
        arn = "arn:aws:s3tables:ap-southeast-1:964340114883:bucket/ah-soc-delta-pilot"
        scope = __import__("hashlib").sha256(f"{arn}|pilot".encode()).hexdigest()[:16]
        self.assertEqual(_history_prefix(arn, "pilot", "target"), f"temp_s3_update/web_ingest/upload_history/{scope}/target/")

    def test_glue_rollback_verifies_a_fresh_iceberg_table_snapshot(self):
        script = Path(__file__).parents[1] / "glue_job.py"
        source = script.read_text()
        self.assertIn("def _fresh_snapshot_state", source)
        self.assertIn("Spark3Util.loadIcebergTable", source)
        self.assertIn("after_snapshot != snapshot_id", source)
    def test_parquet_is_processed_in_bounded_batches_and_sanitised(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / "source.parquet", Path(directory) / "output.parquet"
            pq.write_table(pa.table({"PATIENT_NAME": ["Alice", "Bob"], "PAT_ENC_CSN_ID": ["1", "2"], "AGE": [45, 90]}), source)
            schema, rows, audit = _write_prepared_parquet(source, output, b"x" * 32)
            self.assertEqual(rows, 2)
            self.assertNotIn("PATIENT_NAME", schema.names)
            self.assertIn("pat_enc_csn_id", schema.names)
            self.assertEqual(audit["age_banded_columns"], ["AGE"])

    def test_manifest_types_use_ingestion_contract_names(self):
        self.assertEqual(_iceberg_type(pa.field("id", pa.int64())), "BIGINT")
        self.assertEqual(_iceberg_type(pa.field("when", pa.timestamp("us"))), "TIMESTAMP")
        self.assertEqual(_iceberg_type(pa.field("text", pa.string())), "STRING")

    def test_manual_encryption_choice_is_applied_in_worker_preparation(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / "source.parquet", Path(directory) / "output.parquet"
            pq.write_table(pa.table({"free_text_id": ["A-1"]}), source)
            schema, _, audit = _write_prepared_parquet(source, output, b"x" * 32, ["free_text_id"])
        self.assertEqual(schema.field("free_text_id").type, pa.string())
        self.assertEqual(audit["manual_encryption_columns"], ["free_text_id"])

    def test_excel_is_prepared_by_the_large_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / "source.xlsx", Path(directory) / "output.parquet"
            pd.DataFrame({"PATIENT_NAME": ["Alice"], "PAT_ENC_CSN_ID": ["1"]}).to_excel(source, index=False)
            schema, rows, _ = _write_prepared_parquet(source, output, b"x" * 32, filename="source.xlsx")
        self.assertEqual(rows, 1)
        self.assertNotIn("PATIENT_NAME", schema.names)
        self.assertIn("pat_enc_csn_id", schema.names)

    def test_time_only_values_are_staged_as_strings_for_glue(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / "source.parquet", Path(directory) / "output.parquet"
            pq.write_table(pa.table({"visit_time": pa.array([time(9, 30)], type=pa.time64("us"))}), source)
            schema, _, _ = _write_prepared_parquet(source, output, b"x" * 32)
        self.assertEqual(schema.field("visit_time").type, pa.string())

    def test_nanosecond_timestamps_are_staged_as_microseconds_for_glue(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / "source.parquet", Path(directory) / "output.parquet"
            pq.write_table(pa.table({"visit_at": pa.array([1_726_000_000_123_456_789], type=pa.timestamp("ns"))}), source)
            schema, _, _ = _write_prepared_parquet(source, output, b"x" * 32)
        self.assertEqual(schema.field("visit_at").type, pa.timestamp("us"))

    def test_raw_row_selection_happens_before_sanitisation(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / "source.parquet", Path(directory) / "output.parquet"
            # The rows become equal after PATIENT_NAME is removed, but they are
            # a raw-key conflict and must both be excluded under the V1 rule.
            pq.write_table(pa.table({"PAT_ENC_CSN_ID": ["1", "1", "2", "2"], "PATIENT_NAME": ["Alice", "Bob", "Cara", "Cara"]}), source)
            from s3tables_uploader_v2.worker_analysis import raw_key_row_selection
            selections, metrics = raw_key_row_selection([(source, "source.parquet")], ["pat_enc_csn_id"])
            schema, rows, _ = _write_prepared_parquet(source, output, b"x" * 32, row_indices=selections[0])
        self.assertEqual(metrics["within_upload_key_conflicts"], 2)
        self.assertEqual(metrics["duplicate_rows_within_upload"], 1)
        self.assertEqual(metrics["rows_retained_after_local_deduplication"], 1)
        self.assertEqual(rows, 1)
        self.assertIn("pat_enc_csn_id", schema.names)

    def test_prepared_parquet_uses_the_reviewed_v1_target_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / "source.parquet", Path(directory) / "output.parquet"
            pq.write_table(pa.table({"Visit Date": ["2026-09-01"], "count": [1]}), source)
            schema, rows, _ = _write_prepared_parquet(
                source, output, b"x" * 32,
                target_schema=[
                    {"name": "visit_date", "type": "DATE"},
                    {"name": "count", "type": "BIGINT"},
                    {"name": "missing", "type": "STRING"},
                ],
            )
            staged = pq.read_table(output)
        self.assertEqual(rows, 1)
        self.assertEqual(schema.names, ["visit_date", "count", "missing"])
        self.assertEqual(schema.field("visit_date").type, pa.date32())
        self.assertEqual(schema.field("count").type, pa.int64())
        self.assertEqual(staged["missing"].null_count, 1)

    def test_create_writes_the_v1_append_contract(self):
        class FakeS3:
            def __init__(self): self.written = {}
            def put_object(self, Bucket, Key, Body, **kwargs): self.written[(Bucket, Key)] = json.loads(Body); return {}
        import json
        request = JobRequest(job_id="job", session_id="session", owner_user_id="owner", operation="create", destination=Destination(table_bucket_arn="arn", namespace="ah", table="target"), source_key="prefix/uploads/session/raw/input.parquet", source_version_id="version", source_size_bytes=1, deduplication_mode="keyed", deduplication_columns=["id"])
        settings = WorkerSettings(region="ap-southeast-1", landing_bucket="landing", landing_prefix="prefix", glue_job_name="job", contract_bucket="contracts", contract_prefix="contracts")
        client = FakeS3()
        _write_create_contract(client, settings, request, [{"name": "id", "type": "STRING"}], {"encrypted_columns": [], "postal_columns": [], "age_banded_columns": []})
        record = next(iter(client.written.values()))
        self.assertEqual(record["schema"], [{"name": "id", "type": "STRING"}])
        self.assertEqual(record["deduplication_columns"], ["id"])

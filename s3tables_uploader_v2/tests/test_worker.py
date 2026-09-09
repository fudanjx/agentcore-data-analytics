import tempfile
import unittest
from datetime import time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pandas as pd

from s3tables_uploader_v2.worker import _iceberg_type, _write_create_contract, _write_prepared_parquet
from s3tables_uploader_v2.config import WorkerSettings
from s3tables_uploader_v2.models import Destination, JobRequest


class WorkerTests(unittest.TestCase):
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

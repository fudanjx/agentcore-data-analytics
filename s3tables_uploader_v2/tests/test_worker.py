import tempfile
import unittest
from datetime import time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pandas as pd

from s3tables_uploader_v2.worker import _iceberg_type, _write_prepared_parquet


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

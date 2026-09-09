import tempfile
import unittest
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

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

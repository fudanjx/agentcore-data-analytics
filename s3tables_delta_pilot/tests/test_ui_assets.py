import unittest
from tempfile import TemporaryDirectory
from pathlib import Path
from unittest.mock import patch

import pyarrow as pa
import pyarrow.parquet as pq

from s3tables_delta_pilot.pilot import NAMESPACE, TABLE_BUCKET_ARN
from s3tables_delta_pilot.webapp import IngestionRequest, _current_user, _make_glue_compatible_parquet, _require_scope


STATIC = Path(__file__).parents[1] / "static"


class UiAssetTests(unittest.TestCase):
    def test_unselected_table_card_has_a_dark_text_colour(self):
        css = (STATIC / "style.css").read_text()
        self.assertIn(".table {", css)
        self.assertIn("background: #fff; color: #14213d;", css)

    def test_hyphenated_new_table_name_is_canonicalised_for_s3_tables(self):
        request = IngestionRequest(
            mode="append", table="nuh-surgery", request_id="test",
            table_bucket_arn=TABLE_BUCKET_ARN, namespace=NAMESPACE,
        )
        self.assertEqual("nuh_surgery", request.table)

    def test_ui_explains_the_destination_required_before_review(self):
        html = (STATIC / "index.html").read_text()
        javascript = (STATIC / "app.js").read_text()
        self.assertIn('id="destination-help"', html)
        self.assertIn('Select one existing table', javascript)

    def test_staging_parquet_suffixes_case_colliding_columns(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.parquet"
            pq.write_table(
                pa.table({"Accident_type": ["first"], "Accident_Type": ["second"]}),
                source,
            )
            staged, transformed = _make_glue_compatible_parquet(source, "source.parquet")

            self.assertTrue(transformed)
            self.assertEqual(
                ["accident_type", "accident_type_01"],
                pq.ParquetFile(staged).schema_arrow.names,
            )

    def test_csv_is_staged_as_parquet(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.csv"
            source.write_text("Visit Date,Count\n2026-08-01,3\n")
            staged, transformed = _make_glue_compatible_parquet(source, "source.csv")

            self.assertTrue(transformed)
            self.assertEqual(["visit_date", "count"], pq.ParquetFile(staged).schema_arrow.names)

    def test_parquet_gzip_filename_is_accepted_as_parquet(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.parquet.gzip"
            pq.write_table(pa.table({"value": [1]}), source)
            staged, transformed = _make_glue_compatible_parquet(source, "source.parquet.gzip")

            self.assertFalse(transformed)
            self.assertEqual(source, staged)
            self.assertEqual(1, pq.ParquetFile(staged).metadata.num_rows)

    def test_xlsx_is_staged_as_parquet(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.xlsx"
            import pandas as pd
            pd.DataFrame({"Visit Date": ["2026-08-01"], "Count": [3]}).to_excel(source, index=False)
            staged, transformed = _make_glue_compatible_parquet(source, "source.xlsx")

            self.assertTrue(transformed)
            self.assertEqual(["visit_date", "count"], pq.ParquetFile(staged).schema_arrow.names)

    def test_placeholder_user_scope_is_limited_to_its_configured_bucket(self):
        access = '{"analyst":{"is_admin":false,"buckets":[{"table_bucket_arn":"arn:test:one","namespace":"pilot"}]}}'
        with patch.dict("os.environ", {"PILOT_USER_ACCESS_JSON": access}, clear=False):
            user = _current_user("analyst")
            self.assertFalse(user.is_admin)
            self.assertEqual("arn:test:one", _require_scope(user, "arn:test:one", "pilot").table_bucket_arn)
            with self.assertRaises(Exception):
                _require_scope(user, "arn:test:two", "pilot")


if __name__ == "__main__":
    unittest.main()

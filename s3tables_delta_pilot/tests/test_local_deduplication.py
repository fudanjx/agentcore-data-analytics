import unittest

import polars as pl

from s3tables_delta_pilot.local_deduplication import keyed_deduplicate


class LocalDeduplicationTests(unittest.TestCase):
    def test_exact_duplicates_keep_one_and_conflicting_keys_are_all_skipped(self):
        frame = pl.DataFrame({
            "case": ["A", "A", "B", "B", "C"],
            "ward": ["1", "1", "2", "2", "3"],
            "value": ["same", "same", "old", "new", "only"],
        })
        retained, metrics = keyed_deduplicate(frame, ["case", "ward"])
        self.assertEqual(["A", "C"], retained["case"].to_list())
        self.assertEqual(1, metrics["duplicate_rows_within_upload"])
        self.assertEqual(2, metrics["within_upload_key_conflicts"])
        self.assertEqual(1, metrics["within_upload_conflict_keys"])

    def test_missing_key_components_are_preserved_as_explicit_components(self):
        frame = pl.DataFrame({"a": ["A", None, "A"], "b": [None, "B", "B"], "value": [1, 2, 3]})
        retained, metrics = keyed_deduplicate(frame, ["a", "b"])
        self.assertEqual(3, retained.height)
        self.assertEqual(0, metrics["within_upload_key_conflicts"])

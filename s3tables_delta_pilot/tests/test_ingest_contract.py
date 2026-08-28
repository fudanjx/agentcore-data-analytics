import unittest

import pyarrow as pa

from s3tables_delta_pilot.ingest_contract import compare_schema, normalise_name, schema_from_arrow


class IngestContractTests(unittest.TestCase):
    def test_schema_normalisation_and_types(self):
        schema, warnings = schema_from_arrow(pa.schema([("Visit Date", pa.timestamp("us")), ("Count", pa.int32())]))
        self.assertEqual([], warnings)
        self.assertEqual(
            [
                {"name": "visit_date", "type": "TIMESTAMP", "source_name": "Visit Date"},
                {"name": "count", "type": "BIGINT", "source_name": "Count"},
            ],
            schema,
        )

    def test_extra_is_ignored_missing_is_null_and_cast_is_visible(self):
        source = pa.schema([("value", pa.string()), ("unused", pa.string())])
        result = compare_schema(source, [{"name": "value", "type": "BIGINT"}, {"name": "required", "type": "STRING"}])
        self.assertEqual(["unused"], result["extra_columns"])
        self.assertEqual(["required"], result["missing_columns"])
        self.assertEqual("value", result["type_conversions"][0]["column"])

    def test_name_collision_gets_a_deterministic_suffix(self):
        schema, _ = schema_from_arrow(pa.schema([("BILL_NUM", pa.string()), ("Bill_Num", pa.string()), ("bill num", pa.string())]))
        self.assertEqual(["bill_num", "bill_num_01", "bill_num_02"], [field["name"] for field in schema])

    def test_nanosecond_timestamp_requires_explicit_confirmation(self):
        _, warnings = schema_from_arrow(pa.schema([("created_at", pa.timestamp("ns"))]))
        self.assertEqual(1, len(warnings))
        self.assertIn("nanosecond timestamps", warnings[0])


if __name__ == "__main__":
    unittest.main()

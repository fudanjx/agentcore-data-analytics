import importlib.util
from pathlib import Path
import sys
import unittest

import pandas as pd
import pyarrow as pa


SCRIPT_PATH = Path(__file__).parents[1] / "infra" / "etl_nuh_analytics.py"
SPEC = importlib.util.spec_from_file_location("etl_nuh_analytics", SCRIPT_PATH)
etl = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = etl
SPEC.loader.exec_module(etl)


class EmdEventDateRuleTests(unittest.TestCase):
    def test_day_first_source_exception_and_iso_values_become_timestamps(self):
        frame = pd.DataFrame(
            {
                "PERIOD": ["Apr 2026", "Jun 2026", "May 2026", "May 2026", None],
                "EVENT_ED_TO_EDTU_DATE": [
                    "05/04/2026",
                    "07/06/2026",
                    "2026-05-08 09:10:11",
                    None,
                    None,
                ],
            }
        )

        actual = etl.apply_emd_event_date_rule(frame)

        self.assertEqual(str(actual.loc[0, "EVENT_ED_TO_EDTU_DATE"]), "2026-04-05 00:00:00")
        self.assertEqual(str(actual.loc[1, "EVENT_ED_TO_EDTU_DATE"]), "2026-06-07 00:00:00")
        self.assertEqual(str(actual.loc[2, "EVENT_ED_TO_EDTU_DATE"]), "2026-05-08 09:10:11")
        self.assertTrue(pd.isna(actual.loc[3, "EVENT_ED_TO_EDTU_DATE"]))
        self.assertTrue(pd.isna(actual.loc[4, "EVENT_ED_TO_EDTU_DATE"]))

    def test_unapproved_slash_period_fails(self):
        frame = pd.DataFrame(
            {"PERIOD": ["May 2026"], "EVENT_ED_TO_EDTU_DATE": ["05/06/2026"]}
        )

        with self.assertRaises(etl.SourceValidationError):
            etl.apply_emd_event_date_rule(frame)

    def test_unnamed_columns_are_dropped_and_whitespace_is_normalized(self):
        schema = pa.schema(
            [
                pa.field("Patient Name", pa.string()),
                pa.field("Unnamed: 0", pa.int64()),
                pa.field("Visit Date", pa.timestamp("ns")),
            ]
        )

        specs, dropped = etl.build_column_specs(schema, "emd")

        self.assertEqual([spec.target_name for spec in specs], ["Patient_Name", "Visit_Date"])
        self.assertEqual([spec.pg_type for spec in specs], ["TEXT", "TIMESTAMP WITHOUT TIME ZONE"])
        self.assertEqual(dropped, ["Unnamed: 0"])


if __name__ == "__main__":
    unittest.main()

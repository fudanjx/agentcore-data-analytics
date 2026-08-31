import unittest

from s3tables_delta_pilot.contract import SOURCE_COLUMNS, TARGET_COLUMNS, assert_source_columns, target_name


class SocContractTests(unittest.TestCase):
    def test_contract_has_51_ordered_columns(self):
        self.assertEqual(51, len(SOURCE_COLUMNS))
        self.assertEqual(51, len(TARGET_COLUMNS))

    def test_existing_ah_name_normalisation_is_preserved(self):
        self.assertEqual("sub_specialty_id", target_name("Sub-Specialty_ID"))
        self.assertEqual("prc_sub_specialty", target_name("PRC_SUB-SPECIALTY"))
        self.assertEqual("appt_request_dttm", target_name("APPT_REQUEST_DTTM"))

    def test_schema_drift_is_rejected(self):
        with self.assertRaises(ValueError):
            assert_source_columns(list(SOURCE_COLUMNS[:-1]))


if __name__ == "__main__":
    unittest.main()

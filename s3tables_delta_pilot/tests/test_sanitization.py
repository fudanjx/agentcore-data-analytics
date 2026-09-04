import unittest
from base64 import b64encode

import pandas as pd
import pyarrow as pa
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

from s3tables_delta_pilot.sanitization import ENCRYPTED_PREFIX, _build_iv, age_band, sanitise_table, sanitised_schema


KEY = b"R92oGhcdhyxFbicuopsdataAIO2701211"


class SanitizationTests(unittest.TestCase):
    def setUp(self):
        self.table = pa.table({
            "Patient_Name": ["Jane Tan"],
            "DOB": ["1980-01-01"],
            "Mobile": ["81234567"],
            "Home Address": ["1 Example Street"],
            "Ext_Pat_ID": [12345],
            "PAT_ENC_CSN_ID": [67890],
            "Postal_Code": ["123456"],
            "Age": [38],
            "Telehealth Visit Mode": ["Video"],
        })

    def test_schema_removes_direct_identifiers_and_converts_sensitive_types(self):
        schema, plan = sanitised_schema(self.table.schema)
        self.assertEqual(["Patient_Name", "DOB", "Mobile", "Home Address"], list(plan.drop_columns))
        self.assertEqual(pa.string(), schema.field("Ext_Pat_ID").type)
        self.assertEqual(pa.string(), schema.field("Postal_Code").type)
        self.assertEqual(pa.string(), schema.field("Age").type)
        self.assertIn("Telehealth Visit Mode", schema.names)

    def test_sanitization_encrypts_masks_and_bands(self):
        result, audit = sanitise_table(self.table, KEY)
        self.assertNotIn("Patient_Name", result.schema.names)
        self.assertTrue(result["Ext_Pat_ID"][0].as_py().startswith(ENCRYPTED_PREFIX))
        self.assertNotIn("12345", result["Ext_Pat_ID"][0].as_py())
        self.assertEqual("120000", result["Postal_Code"][0].as_py())
        self.assertEqual("35-39", result["Age"][0].as_py())
        self.assertEqual("Video", result["Telehealth Visit Mode"][0].as_py())
        self.assertEqual(2, audit["newly_encrypted_values"])

    def test_second_sanitization_does_not_encrypt_identifier_again(self):
        first, _ = sanitise_table(self.table, KEY)
        second, audit = sanitise_table(first, KEY)
        self.assertEqual(first["Ext_Pat_ID"][0].as_py(), second["Ext_Pat_ID"][0].as_py())
        self.assertEqual(2, audit["already_encrypted_values"])
        self.assertEqual(0, audit["newly_encrypted_values"])

    def test_age_banding_is_idempotent_for_existing_approved_bands(self):
        self.assertEqual("35-39", age_band("35-39"))
        self.assertEqual("90+", age_band("90+"))
        self.assertEqual("35-39", age_band("38"))
        self.assertTrue(pd.isna(age_band("36-39")))

    def test_legacy_unmarked_cbc_identifier_is_labelled_without_reencryption(self):
        legacy = b64encode(
            AES.new(KEY[:32], AES.MODE_CBC, _build_iv(KEY.decode())).encrypt(pad(b"12345", AES.block_size))
        ).decode()
        source = pa.table({"Ext_Pat_ID": [legacy]})
        result, audit = sanitise_table(source, KEY)

        self.assertEqual(ENCRYPTED_PREFIX + legacy, result["Ext_Pat_ID"][0].as_py())
        self.assertEqual(1, audit["already_encrypted_values"])
        self.assertEqual(0, audit["newly_encrypted_values"])


if __name__ == "__main__":
    unittest.main()

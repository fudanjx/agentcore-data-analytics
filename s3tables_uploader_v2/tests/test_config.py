import unittest

from s3tables_uploader_v2.config import ConfigurationError, Settings


class SettingsTests(unittest.TestCase):
    def valid(self):
        return {
            "AWS_REGION": "ap-southeast-1", "S3_UPLOADER_V2_LANDING_BUCKET": "private-landing",
            "S3_UPLOADER_V2_QUEUE_URL": "https://sqs.example/123/jobs", "S3_UPLOADER_V2_LOGIN_PASSWORD": "password",
            "S3_UPLOADER_V2_LOGIN_SECRET": "x" * 32, "S3_UPLOADER_V2_API_BASE_URL": "https://s3-uploader-v2.bot-alex.com",
        }

    def test_requires_deployment_resource_ids_and_secrets(self):
        env = self.valid(); del env["S3_UPLOADER_V2_LANDING_BUCKET"]
        with self.assertRaisesRegex(ConfigurationError, "LANDING_BUCKET"):
            Settings.from_environ(env)

    def test_production_requires_secure_cookie(self):
        env = self.valid(); env["S3_UPLOADER_V2_COOKIE_SECURE"] = "false"
        with self.assertRaisesRegex(ConfigurationError, "COOKIE_SECURE"):
            Settings.from_environ(env)

    def test_defaults_to_short_raw_retention(self):
        self.assertEqual(Settings.from_environ(self.valid()).raw_retention_days, 1)

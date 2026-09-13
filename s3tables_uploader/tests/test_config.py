import unittest

from s3tables_uploader.config import ConfigurationError, Settings


class SettingsTests(unittest.TestCase):
    def valid(self):
        return {
            "AWS_REGION": "ap-southeast-1", "S3_UPLOADER_LANDING_BUCKET": "private-landing",
            "S3_UPLOADER_BASE_QUEUE_URL": "https://sqs.example/123/base",
            "S3_UPLOADER_LARGE_QUEUE_URL": "https://sqs.example/123/large",
            "S3_UPLOADER_MUTATION_QUEUE_URL": "https://sqs.example/123/mutations",
            "S3_UPLOADER_LOGIN_PASSWORD": "password", "S3_UPLOADER_LOGIN_SECRET": "x" * 32,
            "S3_UPLOADER_LANDING_PREFIX": "s3-uploader",
            "S3_UPLOADER_API_BASE_URL": "https://s3-uploader-v2.bot-alex.com", "S3_UPLOADER_GLUE_JOB_NAME": "s3-uploader-ingest",
            "S3_UPLOADER_CONTRACT_BUCKET": "ah-data-analytics",
            "S3_UPLOADER_CONTRACT_PREFIX": "temp_s3_update/web_ingest/table_contracts",
        }

    def test_requires_deployment_resource_ids_and_secrets(self):
        env = self.valid(); del env["S3_UPLOADER_LANDING_BUCKET"]
        with self.assertRaisesRegex(ConfigurationError, "LANDING_BUCKET"):
            Settings.from_environ(env)

    def test_production_requires_secure_cookie(self):
        env = self.valid(); env["S3_UPLOADER_COOKIE_SECURE"] = "false"
        with self.assertRaisesRegex(ConfigurationError, "COOKIE_SECURE"):
            Settings.from_environ(env)

    def test_defaults_to_short_raw_retention(self):
        self.assertEqual(Settings.from_environ(self.valid()).raw_retention_days, 1)

    def test_requires_all_production_queues(self):
        for name in ("S3_UPLOADER_BASE_QUEUE_URL", "S3_UPLOADER_LARGE_QUEUE_URL", "S3_UPLOADER_MUTATION_QUEUE_URL"):
            env = self.valid(); del env[name]
            with self.assertRaisesRegex(ConfigurationError, name):
                Settings.from_environ(env)

    def test_requires_neutral_storage_and_contract_configuration(self):
        for name in ("S3_UPLOADER_LANDING_PREFIX", "S3_UPLOADER_CONTRACT_BUCKET", "S3_UPLOADER_CONTRACT_PREFIX"):
            env = self.valid(); del env[name]
            with self.assertRaisesRegex(ConfigurationError, name):
                Settings.from_environ(env)

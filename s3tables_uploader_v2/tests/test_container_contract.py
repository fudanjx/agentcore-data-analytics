import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]


class ContainerContractTests(unittest.TestCase):
    def test_api_and_worker_have_separate_non_root_entrypoints(self):
        api = (ROOT / "Dockerfile.api").read_text()
        worker = (ROOT / "Dockerfile.worker").read_text()
        self.assertIn("USER appuser", api)
        self.assertIn('"uvicorn", "s3tables_uploader_v2.entrypoint:app"', api)
        self.assertIn("EXPOSE 8090", api)
        self.assertIn("USER appuser", worker)
        self.assertIn('"python", "-m", "s3tables_uploader_v2.worker"', worker)
        self.assertNotIn("s3tables_delta_pilot", api + worker)

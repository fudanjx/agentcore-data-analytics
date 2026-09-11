import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]


class ContainerContractTests(unittest.TestCase):
    def test_api_and_worker_use_distroless_non_root_runtime_entrypoints(self):
        api = (ROOT / "Dockerfile.api").read_text()
        worker = (ROOT / "Dockerfile.worker").read_text()
        for dockerfile in (api, worker):
            self.assertIn("FROM python:3.12-slim AS builder", dockerfile)
            self.assertIn("FROM mcr.microsoft.com/azurelinux/distroless/python:3.12", dockerfile)
            self.assertIn("USER 65532", dockerfile)
            self.assertIn("PYTHONPATH=/app/dependencies:/app", dockerfile)
        self.assertIn('"python3", "-m", "uvicorn", "s3tables_uploader_v2.entrypoint:app"', api)
        self.assertIn("EXPOSE 8090", api)
        self.assertIn('"python3", "-m", "s3tables_uploader_v2.worker"', worker)
        self.assertNotIn("s3tables_delta_pilot", api + worker)

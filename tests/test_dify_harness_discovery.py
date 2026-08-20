import importlib.util
import pathlib
import sys
import time
import unittest
from unittest.mock import patch


PROXY_DIR = pathlib.Path(__file__).parents[1] / "dify-proxy"
MODULE_PATH = PROXY_DIR / "dify-server.py"
sys.path.insert(0, str(PROXY_DIR))
SPEC = importlib.util.spec_from_file_location("dify_server_for_tests", MODULE_PATH)
dify_server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dify_server)


class FakePaginator:
    def __init__(self, pages):
        self.pages = pages

    def paginate(self):
        return iter(self.pages)


class FakeControlClient:
    def __init__(self, pages):
        self.pages = pages

    def get_paginator(self, operation):
        if operation != "list_harnesses":
            raise AssertionError(f"Unexpected operation: {operation}")
        return FakePaginator(self.pages)


class DifyHarnessDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.original_harnesses = dict(dify_server.DIFY_HARNESSES)
        self.original_enabled = dify_server.DIFY_HARNESS_DISCOVERY_ENABLED
        self.original_attempted = dify_server._harness_discovery_attempted
        self.original_refreshed_at = (
            dify_server._harness_discovery_refreshed_at
        )

        dify_server.DIFY_HARNESS_DISCOVERY_ENABLED = True
        dify_server._harness_discovery_attempted = False
        dify_server._harness_discovery_refreshed_at = 0.0

    def tearDown(self):
        dify_server.DIFY_HARNESSES.clear()
        dify_server.DIFY_HARNESSES.update(self.original_harnesses)
        dify_server.DIFY_HARNESS_DISCOVERY_ENABLED = self.original_enabled
        dify_server._harness_discovery_attempted = self.original_attempted
        dify_server._harness_discovery_refreshed_at = (
            self.original_refreshed_at
        )

    def test_discovers_ready_harnesses_across_pages(self):
        dify_server.DIFY_HARNESSES.clear()
        dify_server.DIFY_HARNESSES.update({"removed": "arn:old"})
        client = FakeControlClient(
            [
                {
                    "harnesses": [
                        {
                            "harnessName": "finance",
                            "arn": "arn:finance",
                            "status": "READY",
                        },
                        {
                            "harnessName": "still-creating",
                            "arn": "arn:creating",
                            "status": "CREATING",
                        },
                    ]
                },
                {
                    "harnesses": [
                        {
                            "harnessName": "dify",
                            "arn": "arn:discovered",
                            "status": "READY",
                        }
                    ]
                },
            ]
        )

        with patch.object(
            dify_server,
            "get_agentcore_control_client",
            return_value=client,
        ):
            harnesses = dify_server.refresh_dify_harnesses(force=True)

        self.assertEqual(harnesses["finance"], "arn:finance")
        self.assertEqual(harnesses["dify"], "arn:discovered")
        self.assertNotIn("still-creating", harnesses)
        self.assertNotIn("removed", harnesses)

    def test_discovery_failure_preserves_cached_harnesses(self):
        cached = {"cached": "arn:cached"}
        dify_server.DIFY_HARNESSES.clear()
        dify_server.DIFY_HARNESSES.update(cached)

        with patch.object(
            dify_server,
            "get_agentcore_control_client",
            side_effect=RuntimeError("access denied"),
        ):
            harnesses = dify_server.refresh_dify_harnesses(force=True)

        self.assertEqual(harnesses, cached)
        self.assertEqual(dify_server.DIFY_HARNESSES, cached)

    def test_initial_discovery_failure_has_no_backends(self):
        dify_server.DIFY_HARNESSES.clear()

        with patch.object(
            dify_server,
            "get_agentcore_control_client",
            side_effect=RuntimeError("access denied"),
        ):
            harnesses = dify_server.refresh_dify_harnesses(force=True)

        self.assertEqual(harnesses, {})

    def test_fresh_cache_does_not_call_control_plane(self):
        dify_server.DIFY_HARNESSES["cached"] = "arn:cached"
        dify_server._harness_discovery_attempted = True
        dify_server._harness_discovery_refreshed_at = time.monotonic()

        with patch.object(
            dify_server,
            "get_agentcore_control_client",
            side_effect=AssertionError("control plane should not be called"),
        ):
            arn = dify_server.get_dify_harness_arn("cached")

        self.assertEqual(arn, "arn:cached")


if __name__ == "__main__":
    unittest.main()

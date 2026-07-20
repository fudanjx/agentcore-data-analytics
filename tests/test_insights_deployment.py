import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
DEPLOY_PATH = ROOT / "openwebui-insights" / "deploy_compose.py"
FRAGMENT_PATH = ROOT / "openwebui-insights" / "compose-service.yml"


def load_deploy_module():
    spec = importlib.util.spec_from_file_location(
        "insights_deploy_compose_test",
        DEPLOY_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class InsightsDeploymentTests(unittest.TestCase):
    def test_compose_merge_is_additive_and_idempotent(self):
        module = load_deploy_module()
        existing = """services:
  postgres:
    image: postgres:15
  open-webui:
    image: ghcr.io/open-webui/open-webui:v0.6.15

volumes:
  open-webui:
    external: true
"""
        fragment = FRAGMENT_PATH.read_text()

        merged = module.merge_compose(existing, fragment)

        self.assertIn("image: postgres:15", merged)
        self.assertIn("open-webui/open-webui:v0.6.15", merged)
        self.assertIn("open-webui/open-webui:v0.10.2-slim@", merged)
        self.assertIn("open-webui-insights-data:", merged)
        self.assertIn("\n  open-webui:\n    external: true", merged)
        self.assertEqual(module.merge_compose(merged, fragment), merged)

    def test_insights_service_disables_local_embedding_and_is_memory_limited(self):
        fragment = FRAGMENT_PATH.read_text()

        self.assertIn('BYPASS_EMBEDDING_AND_RETRIEVAL: "true"', fragment)
        self.assertIn('ENABLE_OLLAMA_API: "false"', fragment)
        self.assertIn("DEFAULT_USER_ROLE: pending", fragment)
        self.assertIn("mem_limit: 900m", fragment)
        self.assertIn("openwebui-insights-964340114883", fragment)


if __name__ == "__main__":
    unittest.main()

import importlib.util
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
DEPLOY_PATH = ROOT / "openwebui-insights" / "deploy_compose.py"
FRAGMENT_PATH = ROOT / "openwebui-insights" / "compose-service.yml"
OFFICE_DEPLOY_PATH = ROOT / "openwebui-insights" / "deploy_office_provider.py"


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
        self.assertIn("open-webui-insights-upload-proxy:", merged)
        self.assertIn('"3001:8080"', merged)
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
        self.assertIn("/insights-office/v1", fragment)
        self.assertIn('"prefix_id":"agentcore-office"', fragment)
        self.assertIn("/strands/v1", fragment)
        self.assertIn("/gmio-pcr-dev/v1", fragment)
        self.assertIn("AGENTCORE_PROXY_BASE_URL", fragment)

    def test_proxy_runtime_registry_contains_only_approved_agents(self):
        registry = (
            ROOT / "proxy" / "k8s" / "runtime-routes-configmap.yaml"
        ).read_text()
        deployment = (ROOT / "proxy" / "k8s" / "deployment.yaml").read_text()

        self.assertIn('"strands"', registry)
        self.assertIn('"insights-office"', registry)
        self.assertIn('"gmio-pcr-dev"', registry)
        self.assertNotIn('"dify"', registry.lower())
        self.assertIn("AGENTCORE_RUNTIME_ROUTES_JSON", deployment)
        self.assertIn("agentcore-proxy-runtime-routes", deployment)
        self.assertNotIn("DIFY_", deployment)
        self.assertNotIn("INSIGHTS_OFFICE_HARNESS_ARN", deployment)

    def test_existing_insights_service_is_migrated_behind_upload_proxy(self):
        module = load_deploy_module()
        existing = """services:
  open-webui-insights:
    image: ghcr.io/open-webui/open-webui:v0.10.2-slim
    ports:
      - \"3001:8080\"
    environment:
      ENABLE_SIGNUP: \"true\"

volumes:
  open-webui-insights-data:
    name: app_open-webui-insights-data
"""

        migrated = module.merge_compose(existing, FRAGMENT_PATH.read_text())

        proxy_start = migrated.index("  open-webui-insights-upload-proxy:")
        service_start = migrated.index("  open-webui-insights:\n")
        service_end = migrated.index("\nvolumes:\n")
        self.assertLess(proxy_start, service_start)
        self.assertIn('"3001:8080"', migrated[proxy_start:service_start])
        self.assertNotIn('"3001:8080"', migrated[service_start:service_end])
        self.assertEqual(module.merge_compose(migrated, FRAGMENT_PATH.read_text()), migrated)

    def test_upload_proxy_forces_process_false_and_only_matches_file_uploads(self):
        caddyfile = (ROOT / "openwebui-insights" / "Caddyfile").read_text()
        smoke_test = (ROOT / "openwebui-insights" / "e2e_smoke.py").read_text()

        self.assertIn("method POST", caddyfile)
        self.assertIn("path /api/v1/files /api/v1/files/", caddyfile)
        self.assertIn("uri @chat_file_upload query process false", caddyfile)
        self.assertIn("reverse_proxy open-webui-insights:8080", caddyfile)
        self.assertIn("--verify-upload-bypass", smoke_test)
        self.assertIn("--token-env", smoke_test)
        self.assertIn("--token-file", smoke_test)
        self.assertIn("/api/v1/files/?process=true", smoke_test)
        self.assertIn("upload_processing=not_started", smoke_test)

    def test_runtime_provider_update_is_idempotent(self):
        spec = importlib.util.spec_from_file_location(
            "insights_office_deploy_test", OFFICE_DEPLOY_PATH
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        original = (
            "services:\n"
            "  open-webui-insights:\n"
            "    environment:\n"
            + module.OFFICE_BASE_URL
            + module.OFFICE_KEYS
            + "      OPENAI_API_CONFIGS: >-\n"
            + module.OFFICE_CONFIG
        )
        with tempfile.TemporaryDirectory() as directory:
            compose = Path(directory) / "docker-compose.yml"
            compose.write_text(original)
            # Exercise pure replacement logic here; Docker validation belongs
            # to the EC2 deployment smoke path.
            updated = module.replace_any_or_verify(
                compose.read_text(),
                (module.LEGACY_BASE_URL, module.OFFICE_BASE_URL),
                module.RUNTIME_BASE_URLS,
                "base",
            )
            updated = module.replace_any_or_verify(
                updated,
                (module.LEGACY_KEYS, module.OFFICE_KEYS),
                module.RUNTIME_KEYS,
                "key",
            )
            updated = module.replace_any_or_verify(
                updated,
                (module.LEGACY_CONFIG, module.OFFICE_CONFIG),
                module.RUNTIME_CONFIG,
                "config",
            )
            self.assertIn("/strands/v1", updated)
            self.assertIn("/insights-office/v1", updated)
            self.assertIn("/gmio-pcr-dev/v1", updated)
            self.assertEqual(
                module.replace_any_or_verify(
                    updated,
                    (module.LEGACY_BASE_URL, module.OFFICE_BASE_URL),
                    module.RUNTIME_BASE_URLS,
                    "base",
                ),
                updated,
            )


if __name__ == "__main__":
    unittest.main()

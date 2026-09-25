"""Environment-matrix regression tests.

Locks in the LOCAL / DEV / STG / PRD feature matrix so a future refactor
cannot accidentally register a frontend-only route in hardened mode (or
disable one in LOCAL).

Matrix under test:

| Feature                          | LOCAL frontend | LOCAL API-only | DEV | STG | PRD |
|----------------------------------|:--------------:|:--------------:|:---:|:---:|:---:|
| Static + / + cookie login        | ✓              | ✗              | ✓   | ✗   | ✗   |
| /api/v3/dev/identity-profiles    | ✓              | ✗              | ✓   | ✗   | ✗   |
| /api/v3/identity                 | ✓              | ✗              | ✓   | ✗   | ✗   |
| docs_url / redoc_url             | ✓              | ✓              | ✗   | ✗   | ✗   |
"""

from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from s3tables_uploader.app.factory import create_app
from s3tables_uploader.config import Environment, Settings
from s3tables_uploader.services.auth.bearer import BearerAuthService
from s3tables_uploader.services.secret_manager import InMemorySecretSource


_FAKE_CLIENTS = {
    "s3": object(),
    "sqs": object(),
    "glue": object(),
    "s3tables": object(),
    "secrets_manager": object(),
}


def _settings(**overrides) -> Settings:
    base = dict(
        region="ap-southeast-1",
        landing_bucket="private-landing",
        landing_prefix="s3-uploader",
        base_worker_queue_url="https://sqs.example/base",
        large_worker_queue_url="https://sqs.example/large",
        mutation_queue_url="https://sqs.example/mutations",
        login_password="password",
        login_secret="x" * 32,
        cookie_secure=False,
        session_ttl_seconds=43200,
        raw_retention_days=1,
        api_base_url="https://example",
        glue_job_name="job",
        contract_bucket="ah-data-analytics",
        contract_prefix="temp/contracts",
        environment=Environment.LOCAL,
    )
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _bearer_service(secret_arn: str) -> BearerAuthService:
    return BearerAuthService(
        InMemorySecretSource({secret_arn: "xyz"}),
        secret_arn,
        cache_ttl_seconds=3600,
        refresh_min_interval_seconds=300,
    )


def _client(environment: Environment, *, frontend: bool = True) -> TestClient:
    """Build an app for the given env and return a live TestClient."""
    hardened = environment in {Environment.STG, Environment.PRD} or (
        environment is Environment.LOCAL and not frontend
    )
    settings = _settings(
        environment=environment,
        serve_local_frontend=frontend if environment is Environment.LOCAL else True,
        cookie_secure=hardened,
        bearer_secret_arn="arn:aws:secretsmanager::0:secret/test",
    )
    return TestClient(
        create_app(
            settings,
            lifespan_clients=_FAKE_CLIENTS,
            lifespan_bearer_auth=_bearer_service(settings.bearer_secret_arn),
        )
    )


# ---------------------------------------------------------------------------
# Static frontend surface
# ---------------------------------------------------------------------------


class StaticSurfaceGateTests(unittest.TestCase):
    """/, /login, /logout, /static/{asset} — frontend-only."""

    def _expect_static(self, env: Environment, *, frontend: bool, served: bool):
        with _client(env, frontend=frontend) as client:
            # /login is unauthenticated even under the cookie gate.
            response = client.get("/login")
        if served:
            self.assertEqual(response.status_code, 200)
        else:
            self.assertEqual(response.status_code, 404, response.text)

    def test_local_frontend_serves_login(self):
        self._expect_static(Environment.LOCAL, frontend=True, served=True)

    def test_local_api_only_does_not_serve_login(self):
        self._expect_static(Environment.LOCAL, frontend=False, served=False)

    def test_dev_serves_login(self):
        self._expect_static(Environment.DEV, frontend=True, served=True)

    def test_stg_does_not_serve_login(self):
        self._expect_static(Environment.STG, frontend=True, served=False)

    def test_prd_does_not_serve_login(self):
        self._expect_static(Environment.PRD, frontend=True, served=False)


# ---------------------------------------------------------------------------
# /api/v3/dev/identity-profiles + /api/v3/identity
# ---------------------------------------------------------------------------


class FrontendApiGateTests(unittest.TestCase):
    """/api/v3/dev/identity-profiles and /api/v3/identity — frontend-only."""

    def _expect_route(
        self, env: Environment, path: str, *, frontend: bool, registered: bool
    ):
        with _client(env, frontend=frontend) as client:
            headers = (
                {"Authorization": "Bearer xyz", "User-ID": "svc@example.com"}
                if env in {Environment.STG, Environment.PRD}
                or (env is Environment.LOCAL and not frontend)
                else {}
            )
            response = client.get(path, headers=headers)
        if registered:
            self.assertNotEqual(response.status_code, 404, response.text)
        else:
            self.assertEqual(response.status_code, 404, response.text)

    def test_dev_profiles_available_in_local_frontend(self):
        self._expect_route(
            Environment.LOCAL,
            "/api/v3/dev/identity-profiles",
            frontend=True,
            registered=True,
        )

    def test_dev_profiles_hidden_in_local_api_only(self):
        self._expect_route(
            Environment.LOCAL,
            "/api/v3/dev/identity-profiles",
            frontend=False,
            registered=False,
        )

    def test_dev_profiles_available_in_dev(self):
        self._expect_route(
            Environment.DEV,
            "/api/v3/dev/identity-profiles",
            frontend=True,
            registered=True,
        )

    def test_dev_profiles_hidden_in_stg(self):
        self._expect_route(
            Environment.STG,
            "/api/v3/dev/identity-profiles",
            frontend=True,
            registered=False,
        )

    def test_dev_profiles_hidden_in_prd(self):
        self._expect_route(
            Environment.PRD,
            "/api/v3/dev/identity-profiles",
            frontend=True,
            registered=False,
        )

    def test_identity_hidden_in_hardened(self):
        self._expect_route(
            Environment.PRD, "/api/v3/identity", frontend=True, registered=False
        )
        self._expect_route(
            Environment.STG, "/api/v3/identity", frontend=True, registered=False
        )
        self._expect_route(
            Environment.LOCAL,
            "/api/v3/identity",
            frontend=False,
            registered=False,
        )


# ---------------------------------------------------------------------------
# docs / redoc
# ---------------------------------------------------------------------------


class DocsGateTests(unittest.TestCase):
    """/docs and /redoc: LOCAL-only (independent of the frontend switch)."""

    def test_docs_served_in_local_frontend(self):
        with _client(Environment.LOCAL, frontend=True) as client:
            response = client.get("/docs")
        self.assertEqual(response.status_code, 200)

    def test_docs_served_in_local_api_only(self):
        with _client(Environment.LOCAL, frontend=False) as client:
            response = client.get(
                "/docs",
                headers={"Authorization": "Bearer xyz", "User-ID": "svc@example.com"},
            )
        # Docs endpoint is unauthenticated in the middleware exempt list,
        # so it renders without a bearer even in LOCAL API-only.
        self.assertEqual(response.status_code, 200)

    def test_docs_missing_in_dev(self):
        with _client(Environment.DEV, frontend=True) as client:
            response = client.get("/docs")
        self.assertEqual(response.status_code, 404)

    def test_docs_missing_in_hardened(self):
        for env in (Environment.STG, Environment.PRD):
            with _client(env, frontend=True) as client:
                response = client.get(
                    "/docs",
                    headers={"Authorization": "Bearer xyz", "User-ID": "svc@example.com"},
                )
            self.assertEqual(response.status_code, 404, msg=str(env))


if __name__ == "__main__":
    unittest.main()

"""End-to-end wiring tests for the new app/ factory.

These tests exercise:

- Lifespan state population + ``request.state`` propagation.
- Environment-driven feature gates (docs/frontend cookie/bearer).
- Identity resolution for both frontend (X-Pilot-User-Id) and hardened
    (User-ID email) modes.
- Bearer auth enforcement via ``require_bearer``.
- Ownership enforcement helper.
- Global exception handlers translating typed errors into JSON.
"""

from __future__ import annotations

import unittest

from fastapi import APIRouter, Depends
from fastapi.testclient import TestClient

from s3tables_uploader.app.dependencies import (
    UserDep,
    enforce_ownership,
    require_bearer,
)
from s3tables_uploader.app.factory import create_app
from s3tables_uploader.config import Environment, Settings
from s3tables_uploader.core.exceptions import OwnershipViolation, UploaderError
from s3tables_uploader.services.auth.bearer import BearerAuthService
from s3tables_uploader.services.secret_manager import InMemorySecretSource


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = dict(
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


_FAKE_CLIENTS = {
    "s3": object(),
    "sqs": object(),
    "glue": object(),
    "s3tables": object(),
    "secrets_manager": object(),
}


class HealthzAndDocsTests(unittest.TestCase):
    def test_healthz_is_public(self):
        app = create_app(_settings(), lifespan_clients=_FAKE_CLIENTS)
        with TestClient(app) as client:
            response = client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_docs_enabled_only_in_local(self):
        for env, expected in [
            (Environment.LOCAL, 200),
            (Environment.DEV, 404),
            (Environment.STG, 404),
            (Environment.PRD, 404),
        ]:
            hardened = env in {Environment.STG, Environment.PRD}
            settings = _settings(
                environment=env,
                bearer_secret_arn="arn:aws:secretsmanager::0:secret/test"
                if hardened
                else None,
                cookie_secure=hardened,
            )
            bearer = None
            if hardened:
                bearer = BearerAuthService(
                    InMemorySecretSource({settings.bearer_secret_arn: "x"}),
                    settings.bearer_secret_arn,
                    cache_ttl_seconds=3600,
                    refresh_min_interval_seconds=300,
                )
            app = create_app(
                settings,
                lifespan_clients=_FAKE_CLIENTS,
                lifespan_bearer_auth=bearer,
            )
            with TestClient(app) as client:
                response = client.get("/docs")
            self.assertEqual(response.status_code, expected, msg=f"env={env}")


class FrontendCookieGateTests(unittest.TestCase):
    def test_api_requests_get_json_401_without_cookie(self):
        settings = _settings()  # LOCAL frontend enabled
        app = create_app(settings, lifespan_clients=_FAKE_CLIENTS)

        router = APIRouter()

        @router.get("/api/v3/echo")
        def echo(user: UserDep) -> dict[str, str]:
            return {"user_id": user.user_id}

        app.include_router(router)
        with TestClient(app) as client:
            response = client.get("/api/v3/echo")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "LOGIN_REQUIRED")

    def test_browser_gets_303_redirect_without_cookie(self):
        settings = _settings()
        app = create_app(settings, lifespan_clients=_FAKE_CLIENTS)
        with TestClient(app) as client:
            response = client.get("/", follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/login")


class FrontendIdentityTests(unittest.TestCase):
    def test_frontend_user_dep_resolves_profile(self):
        app = create_app(_settings(), lifespan_clients=_FAKE_CLIENTS)

        router = APIRouter()

        @router.get("/api/v3/who")
        def who(user: UserDep) -> dict[str, object]:
            return {"user_id": user.user_id, "is_admin": user.is_admin}

        app.include_router(router)
        # Bypass the cookie gate by hitting an exempt path? We can't — but
        # the cookie gate returns JSON 401 for /api/*, so we forge a valid
        # cookie via the existing helper.
        from s3tables_uploader.services.auth.cookie import (
            COOKIE_NAME,
            login_cookie,
        )
        cookie_value = login_cookie(_settings())
        with TestClient(app) as client:
            response = client.get(
                "/api/v3/who",
                headers={"X-Pilot-User-Id": "local-editor"},
                cookies={COOKIE_NAME: cookie_value},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"user_id": "local-editor", "is_admin": False})


class HardenedBearerTests(unittest.TestCase):
    def _hardened_settings(self) -> Settings:
        return _settings(
            environment=Environment.STG,
            cookie_secure=True,
            bearer_secret_arn="arn:aws:secretsmanager::0:secret/test",
        )

    def _prepare_app(self):
        settings = self._hardened_settings()
        secret_source = InMemorySecretSource({settings.bearer_secret_arn: "abcd1234"})
        service = BearerAuthService(
            secret_source,
            settings.bearer_secret_arn,
            cache_ttl_seconds=3600,
            refresh_min_interval_seconds=300,
        )
        app = create_app(
            settings,
            lifespan_clients=_FAKE_CLIENTS,
            lifespan_bearer_auth=service,
        )

        router = APIRouter(dependencies=[Depends(require_bearer)])

        @router.get("/api/v3/protected")
        def protected(user: UserDep) -> dict[str, str]:
            return {"user_id": user.user_id}

        app.include_router(router)
        return app

    def test_missing_bearer_rejected(self):
        with TestClient(self._prepare_app()) as client:
            response = client.get(
                "/api/v3/protected", headers={"User-ID": "svc@example.com"}
            )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "BEARER_AUTH_REQUIRED")

    def test_bad_bearer_rejected(self):
        with TestClient(self._prepare_app()) as client:
            response = client.get(
                "/api/v3/protected",
                headers={
                    "Authorization": "Bearer wrong",
                    "User-ID": "svc@example.com",
                },
            )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "BEARER_AUTH_FAILED")

    def test_valid_bearer_and_email_pass(self):
        with TestClient(self._prepare_app()) as client:
            response = client.get(
                "/api/v3/protected",
                headers={
                    "Authorization": "Bearer abcd1234",
                    "User-ID": "svc@example.com",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"user_id": "svc@example.com"})

    def test_non_email_user_id_rejected(self):
        with TestClient(self._prepare_app()) as client:
            response = client.get(
                "/api/v3/protected",
                headers={
                    "Authorization": "Bearer abcd1234",
                    "User-ID": "not-an-email",
                },
            )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "IDENTITY_INVALID")

    def test_missing_user_id_header_rejected(self):
        with TestClient(self._prepare_app()) as client:
            response = client.get(
                "/api/v3/protected",
                headers={"Authorization": "Bearer abcd1234"},
            )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "IDENTITY_REQUIRED")


class OwnershipHelperTests(unittest.TestCase):
    def test_matching_owner_passes(self):
        from s3tables_uploader.protocols.auth import UserContext

        user = UserContext(
            user_id="alice", is_admin=False,
            can_view_upload_history=True, can_rollback_uploads=False,
        )
        enforce_ownership("alice", user)  # does not raise

    def test_mismatched_owner_raises(self):
        from s3tables_uploader.protocols.auth import UserContext

        user = UserContext(
            user_id="alice", is_admin=False,
            can_view_upload_history=True, can_rollback_uploads=False,
        )
        with self.assertRaises(OwnershipViolation):
            enforce_ownership("bob", user)


class ExceptionHandlerTests(unittest.TestCase):
    def test_uploader_error_translates_to_json(self):
        app = create_app(_settings(), lifespan_clients=_FAKE_CLIENTS)

        router = APIRouter()

        @router.get("/api/v3/boom")
        def boom() -> None:
            raise UploaderError("nope", error_code="CUSTOM")

        app.include_router(router)
        from s3tables_uploader.services.auth.cookie import COOKIE_NAME, login_cookie
        cookie_value = login_cookie(_settings())
        with TestClient(app) as client:
            response = client.get(
                "/api/v3/boom",
                headers={"X-Pilot-User-Id": "local-admin"},
                cookies={COOKIE_NAME: cookie_value},
            )
        self.assertEqual(response.status_code, 500)
        body = response.json()
        self.assertEqual(body["code"], "CUSTOM")
        self.assertEqual(body["detail"], "nope")


if __name__ == "__main__":
    unittest.main()

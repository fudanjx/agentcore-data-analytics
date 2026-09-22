"""End-to-end wiring tests for the new app/ factory.

These tests exercise:

- Lifespan state population + ``request.state`` propagation.
- Environment-driven feature gates (docs / frontend cookie surface).
- Identity resolution for both frontend (X-Pilot-User-Id) and hardened
  (User-ID email) modes.
- The unified ``require_auth`` guard: bearer works everywhere, cookie is
  accepted only when the frontend surface is on.
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
    require_auth,
)
from s3tables_uploader.app.factory import create_app
from s3tables_uploader.config import Environment, Settings
from s3tables_uploader.core.exceptions import OwnershipViolation, UploaderError
from s3tables_uploader.services.auth.bearer import BearerAuthService
from s3tables_uploader.services.auth.cookie import COOKIE_NAME, login_cookie
from s3tables_uploader.services.secret_manager import InMemorySecretSource


_TEST_BEARER_ARN = "arn:aws:secretsmanager::0:secret/test"
_TEST_BEARER_TOKEN = "abcd1234"


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
        bearer_secret_arn=_TEST_BEARER_ARN,
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


def _bearer_service(settings: Settings, token: str = _TEST_BEARER_TOKEN) -> BearerAuthService:
    return BearerAuthService(
        InMemorySecretSource({settings.bearer_secret_arn: token}),
        settings.bearer_secret_arn,
        cache_ttl_seconds=3600,
        refresh_min_interval_seconds=300,
    )


def _make_app(settings: Settings, *, token: str = _TEST_BEARER_TOKEN):
    return create_app(
        settings,
        lifespan_clients=_FAKE_CLIENTS,
        lifespan_bearer_auth=_bearer_service(settings, token),
    )


class HealthzAndDocsTests(unittest.TestCase):
    def test_healthz_is_public(self):
        app = _make_app(_settings())
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
            settings = _settings(environment=env, cookie_secure=hardened)
            app = _make_app(settings)
            with TestClient(app) as client:
                response = client.get("/docs")
            self.assertEqual(response.status_code, expected, msg=f"env={env}")


class FrontendCookieGateTests(unittest.TestCase):
    def test_api_requests_get_json_401_without_cookie(self):
        settings = _settings()  # LOCAL frontend enabled
        app = _make_app(settings)

        router = APIRouter(dependencies=[Depends(require_auth)])

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
        app = _make_app(settings)
        with TestClient(app) as client:
            response = client.get("/", follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/login")

    def test_cookie_gate_skips_when_authorization_header_present(self):
        """Bearer request in a frontend env: cookie gate must NOT block."""
        settings = _settings()
        app = _make_app(settings)

        router = APIRouter(dependencies=[Depends(require_auth)])

        @router.get("/api/v3/echo")
        def echo(user: UserDep) -> dict[str, str]:
            return {"user_id": user.user_id}

        app.include_router(router)
        with TestClient(app) as client:
            response = client.get(
                "/api/v3/echo",
                headers={
                    "Authorization": f"Bearer {_TEST_BEARER_TOKEN}",
                    "User-ID": "svc@example.com",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"user_id": "svc@example.com"})


class FrontendIdentityTests(unittest.TestCase):
    def test_frontend_user_dep_resolves_profile(self):
        settings = _settings()
        app = _make_app(settings)

        router = APIRouter(dependencies=[Depends(require_auth)])

        @router.get("/api/v3/who")
        def who(user: UserDep) -> dict[str, object]:
            return {"user_id": user.user_id, "is_admin": user.is_admin}

        app.include_router(router)
        cookie_value = login_cookie(settings)
        with TestClient(app) as client:
            response = client.get(
                "/api/v3/who",
                headers={"X-Pilot-User-Id": "local-editor"},
                cookies={COOKIE_NAME: cookie_value},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"user_id": "local-editor", "is_admin": False})


class BearerEverywhereTests(unittest.TestCase):
    """Bearer must work in every environment now, not only hardened ones."""

    def _app_with_protected_route(self, settings: Settings):
        app = _make_app(settings)

        router = APIRouter(dependencies=[Depends(require_auth)])

        @router.get("/api/v3/protected")
        def protected(user: UserDep) -> dict[str, str]:
            return {"user_id": user.user_id}

        app.include_router(router)
        return app

    def test_dev_accepts_bearer(self):
        settings = _settings(environment=Environment.DEV, cookie_secure=True)
        app = self._app_with_protected_route(settings)
        with TestClient(app) as client:
            response = client.get(
                "/api/v3/protected",
                headers={
                    "Authorization": f"Bearer {_TEST_BEARER_TOKEN}",
                    "User-ID": "svc@example.com",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"user_id": "svc@example.com"})

    def test_dev_accepts_cookie(self):
        settings = _settings(environment=Environment.DEV, cookie_secure=True)
        app = self._app_with_protected_route(settings)
        cookie_value = login_cookie(settings)
        with TestClient(app) as client:
            response = client.get(
                "/api/v3/protected",
                headers={"X-Pilot-User-Id": "local-editor"},
                cookies={COOKIE_NAME: cookie_value},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["user_id"], "local-editor")

    def test_local_frontend_bad_bearer_rejected(self):
        """Invalid bearer must 401 — no silent fallback to cookie."""
        settings = _settings()
        app = self._app_with_protected_route(settings)
        cookie_value = login_cookie(settings)
        with TestClient(app) as client:
            response = client.get(
                "/api/v3/protected",
                headers={
                    "Authorization": "Bearer wrong-token",
                    "User-ID": "svc@example.com",
                },
                cookies={COOKIE_NAME: cookie_value},  # ignored, bearer wins
            )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "BEARER_AUTH_FAILED")


class HardenedBearerTests(unittest.TestCase):
    def _hardened_settings(self) -> Settings:
        return _settings(environment=Environment.STG, cookie_secure=True)

    def _prepare_app(self):
        settings = self._hardened_settings()
        app = _make_app(settings)

        router = APIRouter(dependencies=[Depends(require_auth)])

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
                    "Authorization": f"Bearer {_TEST_BEARER_TOKEN}",
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
                    "Authorization": f"Bearer {_TEST_BEARER_TOKEN}",
                    "User-ID": "not-an-email",
                },
            )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "IDENTITY_INVALID")

    def test_missing_user_id_header_rejected(self):
        with TestClient(self._prepare_app()) as client:
            response = client.get(
                "/api/v3/protected",
                headers={"Authorization": f"Bearer {_TEST_BEARER_TOKEN}"},
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
        settings = _settings()
        app = _make_app(settings)

        router = APIRouter(dependencies=[Depends(require_auth)])

        @router.get("/api/v3/boom")
        def boom() -> None:
            raise UploaderError("nope", error_code="CUSTOM")

        app.include_router(router)
        cookie_value = login_cookie(settings)
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

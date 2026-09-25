"""FastAPI dependencies.

Two layers:

- **State readers**: ``get_settings``, ``get_store``, etc. read singletons
    from ``request.state`` (populated by the ASGI lifespan). No boto3 clients
    are constructed here.

- **Service factories**: ``get_lease_service`` and friends build thin,
    stateless services per request from those singletons. Overriding these
    with ``app.dependency_overrides`` is the standard test-substitution
    path.

Also owns the two identity resolvers (cookie / bearer), their dispatcher,
the router-level ``require_auth`` guard, and the ``enforce_ownership``
helper called at every ownership check site.
"""

from __future__ import annotations

import re
from typing import Annotated, Any

from fastapi import Depends, Header, Request

from ..config import Settings
from ..core.constants import (
    FRONTEND_IDENTITY_HEADER,
    HARDENED_IDENTITY_HEADER,
)
from ..core.exceptions import (
    BearerAuthRequired,
    IdentityHeaderInvalid,
    IdentityHeaderRequired,
    OwnershipViolation,
)
from ..job_store import S3JobStore
from ..protocols.auth import UserContext
from ..services.audit import ScopedS3AuditReader
from ..services.auth.bearer import BearerAuthService
from ..services.contracts import ContractService
from ..services.leases import LeaseService
from ..services.mutations import MutationEnqueuerService
from ..services.profiles import LocalIdentityProfileService
from ..services.tables import TableBucketService


# ---------------------------------------------------------------------------
# State readers
# ---------------------------------------------------------------------------

def get_settings(request: Request) -> Settings:
    return request.state.settings


SettingsDep = Annotated[Settings, Depends(get_settings)]


def get_s3(request: Request) -> Any:
    return request.state.s3


S3Dep = Annotated[Any, Depends(get_s3)]


def get_sqs(request: Request) -> Any:
    return request.state.sqs


SqsDep = Annotated[Any, Depends(get_sqs)]


def get_glue(request: Request) -> Any:
    return request.state.glue


GlueDep = Annotated[Any, Depends(get_glue)]


def get_s3tables(request: Request) -> Any:
    return request.state.s3tables


S3TablesDep = Annotated[Any, Depends(get_s3tables)]


def get_store(request: Request) -> S3JobStore:
    return request.state.store


StoreDep = Annotated[S3JobStore, Depends(get_store)]


def get_bearer_auth(request: Request) -> BearerAuthService:
    return request.state.bearer_auth


BearerAuthDep = Annotated[BearerAuthService, Depends(get_bearer_auth)]


# ---------------------------------------------------------------------------
# Service factories (thin, per-request)
# ---------------------------------------------------------------------------

def get_table_bucket_service(s3tables: S3TablesDep) -> TableBucketService:
    return TableBucketService(s3tables)


TableBucketServiceDep = Annotated[TableBucketService, Depends(get_table_bucket_service)]


def get_contract_service(s3: S3Dep, settings: SettingsDep) -> ContractService:
    return ContractService(s3, settings)


ContractServiceDep = Annotated[ContractService, Depends(get_contract_service)]


def get_lease_service(store: StoreDep, sqs: SqsDep, settings: SettingsDep) -> LeaseService:
    return LeaseService(store, sqs, settings)


LeaseServiceDep = Annotated[LeaseService, Depends(get_lease_service)]


def get_mutation_service(sqs: SqsDep, settings: SettingsDep) -> MutationEnqueuerService:
    return MutationEnqueuerService(sqs, settings)


MutationServiceDep = Annotated[MutationEnqueuerService, Depends(get_mutation_service)]


def get_audit_reader(s3: S3Dep, settings: SettingsDep) -> ScopedS3AuditReader:
    return ScopedS3AuditReader(
        s3,
        history_bucket=settings.history_bucket,
        history_prefix=settings.history_prefix,
    )


AuditReaderDep = Annotated[ScopedS3AuditReader, Depends(get_audit_reader)]


def get_skill_destination(settings: SettingsDep) -> dict[str, str]:
    """Return skill-bundle destination kwargs threaded through to ``skill_bundle``.

    Routes spread this dict into ``skill_bundle`` function calls
    (``**dest``) so the module never has to reach into env vars from
    inside a request.
    """
    return {
        "destination_bucket": settings.skill_bundle_bucket,
        "destination_prefix": settings.skill_bundle_prefix,
    }


SkillDestinationDep = Annotated[dict[str, str], Depends(get_skill_destination)]


def get_profile_service(settings: SettingsDep) -> LocalIdentityProfileService | None:
    """Return the hardcoded profile service, or ``None`` in hardened mode."""
    if not settings.frontend_surface_enabled:
        return None
    return LocalIdentityProfileService()


ProfileServiceDep = Annotated[
    LocalIdentityProfileService | None, Depends(get_profile_service)
]


# ---------------------------------------------------------------------------
# Auth guard (router-level dependency)
# ---------------------------------------------------------------------------

def require_auth(
    request: Request,
    settings: SettingsDep,
    bearer: BearerAuthDep,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Router-level guard accepted by every core router.

    - If the request carries an ``Authorization`` header, it must be a valid
      ``Bearer <token>``. Invalid or malformed values raise 401 — no silent
      fallback to cookie auth.
    - Otherwise, if the environment serves the frontend surface the cookie
      middleware has already validated the session; we tag the request as
      cookie-authed.
    - In bearer-only environments (no frontend surface) a missing header is
      rejected outright.

    The chosen path is stamped on ``request.state.auth_method`` so
    :func:`resolve_user` can pick the matching identity resolver.
    """
    if authorization:
        bearer.verify(authorization)
        request.state.auth_method = "bearer"
        return
    if not settings.frontend_surface_enabled:
        raise BearerAuthRequired("Bearer token required")
    request.state.auth_method = "cookie"


# ---------------------------------------------------------------------------
# Identity resolution
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _resolve_frontend_user(
    user_id: str | None,
    profiles: LocalIdentityProfileService | None,
) -> UserContext:
    """Cookie modes: resolve the profile switcher's identity.

    Missing header defaults to ``local-admin`` — the profile switcher may
    not be set on the browser's first request, and forcing a 401 there
    would break the initial page load. Hardened modes have no such
    fallback: ``User-ID`` is strictly required.
    """
    if profiles is None:
        raise IdentityHeaderInvalid(
            "Frontend identity resolver called in hardened mode"
        )
    return profiles.resolve(user_id or "local-admin")


def _resolve_hardened_user(user_id: str | None) -> UserContext:
    """Hardened modes: validate ``User-ID`` (email); no profile lookup, full permissions."""
    if not user_id:
        raise IdentityHeaderRequired(
            f"Header {HARDENED_IDENTITY_HEADER} is required"
        )
    if not _EMAIL_RE.match(user_id):
        raise IdentityHeaderInvalid(
            f"Header {HARDENED_IDENTITY_HEADER} must be an email address"
        )
    return UserContext(
        user_id=user_id,
        is_admin=True,
        can_view_upload_history=True,
        can_rollback_uploads=True,
        visible_buckets=None,  # None = "see all customer buckets"
    )


def resolve_user(
    request: Request,
    profiles: ProfileServiceDep,
    x_pilot_user_id: Annotated[
        str | None, Header(alias=FRONTEND_IDENTITY_HEADER)
    ] = None,
    hardened_user_id: Annotated[
        str | None, Header(alias=HARDENED_IDENTITY_HEADER)
    ] = None,
) -> UserContext:
    """Dispatcher: pick the resolver based on how ``require_auth`` authenticated.

    ``require_auth`` stamps ``request.state.auth_method`` (``"bearer"`` or
    ``"cookie"``); this dep reads that stamp so a bearer client resolves via
    ``User-ID`` regardless of environment. Handlers depend on ``UserDep`` and
    stay agnostic of both env and transport.
    """
    method = getattr(request.state, "auth_method", None)
    if method == "cookie":
        return _resolve_frontend_user(x_pilot_user_id, profiles)
    return _resolve_hardened_user(hardened_user_id)


UserDep = Annotated[UserContext, Depends(resolve_user)]


# ---------------------------------------------------------------------------
# Ownership check helper
# ---------------------------------------------------------------------------

def enforce_ownership(record_owner_id: str | None, user: UserContext) -> None:
    """Raise :class:`OwnershipViolation` unless ``user`` owns the record.

    Kept as a plain function rather than a FastAPI dependency because most
    ownership checks fire mid-handler after a durable record is read; the
    handler is the only place that knows which record to compare against.
    """
    if record_owner_id != user.user_id:
        raise OwnershipViolation("You do not own this resource")

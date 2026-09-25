"""Typed application exceptions.

Exception handlers in ``app/exception_handlers.py`` (added in Phase 3) map
these to HTTP responses. Raising a typed exception from handlers or services
keeps status-code selection out of business logic.
"""

from __future__ import annotations


class UploaderError(Exception):
    """Base for every exception raised by the uploader API layer."""

    status_code: int = 500
    error_code: str = "UPLOADER_ERROR"

    def __init__(self, message: str = "", *, error_code: str | None = None) -> None:
        super().__init__(message or self.__class__.__name__)
        if error_code is not None:
            self.error_code = error_code


class LoginRequired(UploaderError):
    status_code = 401
    error_code = "LOGIN_REQUIRED"


class BearerAuthRequired(UploaderError):
    status_code = 401
    error_code = "BEARER_AUTH_REQUIRED"


class BearerAuthFailed(UploaderError):
    status_code = 401
    error_code = "BEARER_AUTH_FAILED"


class IdentityHeaderRequired(UploaderError):
    status_code = 401
    error_code = "IDENTITY_REQUIRED"


class IdentityHeaderInvalid(UploaderError):
    status_code = 401
    error_code = "IDENTITY_INVALID"


class OwnershipViolation(UploaderError):
    status_code = 403
    error_code = "FORBIDDEN"


class TableBucketForbidden(UploaderError):
    status_code = 403
    error_code = "TABLE_BUCKET_FORBIDDEN"


class AdminRequired(UploaderError):
    status_code = 403
    error_code = "ADMIN_REQUIRED"


class ControlPlaneError(UploaderError):
    """Wraps a boto3 ClientError from S3 Tables control-plane calls."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 400,
        error_code: str = "CONTROL_PLANE_ERROR",
    ) -> None:
        self.status_code = status_code
        self.error_code = error_code
        super().__init__(message)


class ConfigurationError(UploaderError):
    """Raised when a required deployment setting is absent or unsafe."""

    status_code = 500
    error_code = "CONFIGURATION_ERROR"

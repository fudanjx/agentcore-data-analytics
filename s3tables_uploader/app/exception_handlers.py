"""Global exception handlers.

Handlers here are the *only* place status-code selection happens. Handler
code raises typed :class:`UploaderError` subclasses (or lets a boto3
``ClientError`` propagate) and the handlers translate them into JSON. This
keeps route bodies free of ``HTTPException(...)`` clutter and gives one
place to audit for logging.
"""

from __future__ import annotations

from typing import Any

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ..core.exceptions import ControlPlaneError, UploaderError
from ..core.logger import create_structured_logger


_logger = create_structured_logger("s3tables_uploader.exceptions")


def _response(status_code: int, error_code: str, detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"code": error_code, "detail": detail},
    )


async def uploader_error_handler(_request: Request, exc: UploaderError) -> JSONResponse:
    _logger.info(
        "uploader_error",
        error_code=exc.error_code,
        status_code=exc.status_code,
        detail=str(exc),
    )
    return _response(exc.status_code, exc.error_code, str(exc))


async def control_plane_error_handler(
    _request: Request, exc: ControlPlaneError
) -> JSONResponse:
    _logger.warning(
        "control_plane_error",
        error_code=exc.error_code,
        status_code=exc.status_code,
        detail=str(exc),
    )
    return _response(exc.status_code, exc.error_code, str(exc))


async def client_error_handler(_request: Request, exc: ClientError) -> JSONResponse:
    payload = exc.response.get("Error", {}) if hasattr(exc, "response") else {}
    code = str(payload.get("Code", "AWS_CLIENT_ERROR"))
    message = str(payload.get("Message", "Upstream AWS call failed"))
    if code in {"AccessDenied", "AccessDeniedException"}:
        status = 403
    elif code in {"ResourceNotFoundException", "NotFoundException", "NoSuchKey", "404"}:
        status = 404
    elif code in {"ConflictException", "AlreadyExistsException", "PreconditionFailed"}:
        status = 409
    else:
        status = 502
    _logger.warning("aws_client_error", error_code=code, status_code=status, detail=message)
    return _response(status, code, message)


async def botocore_error_handler(_request: Request, exc: BotoCoreError) -> JSONResponse:
    _logger.error("botocore_error", detail=str(exc))
    return _response(502, "BOTOCORE_ERROR", "Upstream AWS call failed")


async def unhandled_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    _logger.error("unhandled_error", detail=str(exc), exc_info=True)
    return _response(500, "INTERNAL_SERVER_ERROR", "Unexpected server error")


def install_exception_handlers(app: FastAPI) -> None:
    """Register every typed handler on ``app``.

    Order matters: more specific handlers must be registered before more
    general ones — FastAPI dispatches by the first matching class.
    """
    app.add_exception_handler(ControlPlaneError, control_plane_error_handler)
    app.add_exception_handler(UploaderError, uploader_error_handler)
    app.add_exception_handler(ClientError, client_error_handler)
    app.add_exception_handler(BotoCoreError, botocore_error_handler)
    # An unhandled-Exception handler is intentionally NOT registered here so
    # unexpected errors still bubble up during tests. Add one in prod if
    # explicit last-resort masking is required.
    _ = unhandled_error_handler  # keep the helper reachable / documented
    _ = Any  # silence unused-import warnings on Any (kept for future use)

"""Shared helpers used by the compat upload-session / lease routers."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

from ...app.dependencies import enforce_ownership
from ...job_store import MissingRecord, S3JobStore
from ...protocols.auth import UserContext
from ...utils.time import now_iso


def get_compat_session(store: S3JobStore, session_id: str, user: UserContext) -> dict[str, Any]:
    try:
        session = store.get_compat_session(session_id)
    except MissingRecord as error:
        raise HTTPException(
            404, "The upload session does not exist, belongs to another user, or has expired"
        ) from error
    enforce_ownership(session.get("owner_user_id"), user)
    if datetime.fromisoformat(session["expires_at"]) <= datetime.now(timezone.utc):
        raise HTTPException(404, "The upload session has expired")
    return session


def save_compat_session(store: S3JobStore, session: dict[str, Any], **changes: Any) -> dict[str, Any]:
    now = now_iso()
    if "phase" in changes and changes["phase"] != session.get("phase"):
        changes["phase_started_at"] = now
    updated = store.update_compat_session(
        str(session["session_id"]), {**changes, "updated_at": now}
    )
    session.clear()
    session.update(updated)
    return session


def delete_raw_source_versions(store: S3JobStore, session: dict[str, Any]) -> None:
    """Delete only the immutable versions received for a cancelled session."""
    s3 = store.s3
    bucket = store.bucket
    for source in session.get("files", []):
        key = str(source.get("source_key") or "")
        version_id = str(source.get("source_version_id") or "")
        if not key or not version_id:
            continue
        try:
            s3.delete_object(Bucket=bucket, Key=key, VersionId=version_id)
        except Exception as error:  # pragma: no cover
            raise HTTPException(503, "CANCEL_CLEANUP_INCOMPLETE") from error


def safe_compat_session_response(store: S3JobStore, session: dict[str, Any], leases) -> dict[str, Any]:
    value = {**session}
    value["files"] = [
        {key: item[key] for key in ("name", "sha256", "size_bytes")}
        for item in session.get("files", [])
    ]
    lease_id = session.get("worker_lease_id")
    if lease_id:
        try:
            value["worker_lease"] = leases.as_response(store.get_lease(str(lease_id)))
        except MissingRecord:
            value["worker_lease"] = {
                "lease_id": lease_id,
                "worker_state": "FAILED",
                "can_retry_large": False,
                "can_cancel_and_start_over": False,
            }
    return value

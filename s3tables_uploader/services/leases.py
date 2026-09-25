"""Worker lease lifecycle service.

Encapsulates the state transitions and SQS dispatch previously scattered
across ``api.py`` helpers (``_new_lease``, ``_bind_lease``, ``_dispatch_lease``,
``_lease_queue``, ``_lease_response``, ``_lock_lease_for_ingestion``).
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from ..config import Settings
from ..core.constants import ACTIVE_LEASE_MINUTES, PREUPLOAD_LEASE_MINUTES
from ..core.exceptions import (
    ControlPlaneError,
    OwnershipViolation,
    UploaderError,
)
from ..job_store import MissingRecord, RecordStateConflict, S3JobStore
from ..utils.time import now_iso, utc_now
from ..worker_routing import RoutingError, SelectedFile, route_files


_CANCELLABLE_STATES = frozenset(
    {
        "STARTING",
        "AWAITING_UPLOAD",
        "PROFILING",
        "AWAITING_KEY",
        "ANALYSING_KEY",
        "AWAITING_CONFIRMATION",
        "FAILED",
    }
)

_TERMINAL_STATES = frozenset(
    {"CANCELLED", "EXPIRED", "COMPLETED", "RESOURCE_LIMIT_EXCEEDED"}
)


class LeaseService:
    """Create, bind, dispatch and inspect worker leases."""

    def __init__(self, store: S3JobStore, sqs_client: Any, settings: Settings):
        self._store = store
        self._sqs = sqs_client
        self._settings = settings

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def new_lease(self, files: list[dict[str, Any]], user_id: str) -> dict[str, Any]:
        route = self._route_files(files)
        now = utc_now()
        lease = {
            "schema_version": 1,
            "lease_id": uuid.uuid4().hex,
            "owner_user_id": user_id,
            "files": [
                {"name": str(item["name"]), "size_bytes": int(item["size_bytes"])}
                for item in files
            ],
            "worker_size": route.worker_size,
            "routing_score": route.routing_score,
            "routing_reason": route.routing_reason,
            "state": "STARTING",
            "message": "Starting a leased worker for the selected file.",
            "session_id": None,
            "attempt": 1,
            "can_retry_large": False,
            "cancellation_locked_at": None,
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=PREUPLOAD_LEASE_MINUTES)).isoformat(),
        }
        self._store.put_lease(lease, create_only=True)
        self.dispatch(lease)
        return lease

    def dispatch(self, lease: dict[str, Any]) -> None:
        self._sqs.send_message(
            QueueUrl=self.queue_url(str(lease["worker_size"])),
            MessageBody=f"lease:{lease['lease_id']}",
            MessageDeduplicationId=f"lease:{lease['lease_id']}:{lease.get('attempt', 1)}",
            MessageGroupId=str(lease["lease_id"]),
        )

    def queue_url(self, worker_size: str) -> str:
        return (
            self._settings.base_worker_queue_url
            if worker_size == "BASE"
            else self._settings.large_worker_queue_url
        )

    def bind(
        self,
        lease_id: str,
        user_id: str,
        session: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            lease = self._store.get_lease(lease_id)
        except MissingRecord as error:
            raise UploaderError(
                "Worker lease not found", error_code="WORKER_LEASE_NOT_FOUND"
            ).__class__("WORKER_LEASE_NOT_FOUND") from error
        if lease.get("owner_user_id") != user_id:
            raise OwnershipViolation("WORKER_LEASE_FORBIDDEN")
        if lease.get("state") in _TERMINAL_STATES:
            raise UploaderError(
                "Worker lease unavailable", error_code="WORKER_LEASE_UNAVAILABLE"
            )
        expected = [
            (item["name"], int(item["size_bytes"])) for item in lease.get("files", [])
        ]
        received = [
            (item["name"], int(item["size_bytes"])) for item in session.get("files", [])
        ]
        if expected != received:
            raise UploaderError(
                "Worker lease files changed", error_code="WORKER_LEASE_FILES_CHANGED"
            )
        try:
            return self._store.update_lease(
                lease_id,
                {
                    "session_id": session["session_id"],
                    "state": "AWAITING_UPLOAD",
                    "message": "Waiting for the upload to become available.",
                    "updated_at": now_iso(),
                    "expires_at": (
                        utc_now() + timedelta(minutes=ACTIVE_LEASE_MINUTES)
                    ).isoformat(),
                },
                guard=lambda current: current.get("owner_user_id") == user_id
                and current.get("state") not in _TERMINAL_STATES
                and not current.get("cancellation_locked_at"),
            )
        except RecordStateConflict as error:
            raise UploaderError(
                "Worker lease unavailable", error_code="WORKER_LEASE_UNAVAILABLE"
            ) from error

    def lock_for_ingestion(self, session: dict[str, Any], user_id: str) -> None:
        lease_id = session.get("worker_lease_id")
        if not lease_id:
            raise UploaderError(
                "Worker lease required", error_code="WORKER_LEASE_REQUIRED"
            )
        try:
            self._store.update_lease(
                str(lease_id),
                {"cancellation_locked_at": now_iso(), "updated_at": now_iso()},
                guard=lambda lease: lease.get("owner_user_id") == user_id
                and str(lease.get("session_id") or "") == str(session["session_id"])
                and lease.get("state")
                not in {"CANCELLED", "EXPIRED", "FAILED", "RESOURCE_LIMIT_EXCEEDED"}
                and not lease.get("cancellation_locked_at"),
            )
        except RecordStateConflict as error:
            raise ControlPlaneError(
                "Cancel-and-start-over unavailable",
                status_code=409,
                error_code="CANCEL_AND_START_OVER_UNAVAILABLE",
            ) from error

    def as_response(self, lease: dict[str, Any]) -> dict[str, Any]:
        return {
            "lease_id": lease["lease_id"],
            "worker_state": lease["state"],
            "worker_size": lease["worker_size"],
            "routing_score": lease.get("routing_score"),
            "routing_reason": lease.get("routing_reason"),
            "expires_at": lease["expires_at"],
            "can_retry_large": bool(lease.get("can_retry_large")),
            "can_cancel_and_start_over": bool(
                lease.get("state") in _CANCELLABLE_STATES
                and not lease.get("cancellation_locked_at")
            ),
        }

    # ------------------------------------------------------------------
    # Direct-job (non-lease) dispatch used by /api/v3/upload-sessions/complete
    # ------------------------------------------------------------------

    def dispatch_direct_job(self, job_id: str, name: str, size_bytes: int) -> None:
        try:
            route = route_files([SelectedFile(name=name, size_bytes=size_bytes)])
        except RoutingError as error:
            raise UploaderError(str(error), error_code="ROUTING_ERROR") from error
        self._sqs.send_message(
            QueueUrl=self.queue_url(route.worker_size),
            MessageBody=job_id,
            MessageDeduplicationId=job_id,
            MessageGroupId=job_id,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _route_files(self, files: list[dict[str, Any]]):
        try:
            return route_files(
                [
                    SelectedFile(name=str(item["name"]), size_bytes=int(item["size_bytes"]))
                    for item in files
                ]
            )
        except RoutingError as error:
            raise UploaderError(str(error), error_code="ROUTING_ERROR") from error

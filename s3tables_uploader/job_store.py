"""S3-only durable storage for sessions, idempotent jobs, and worker claims."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from botocore.exceptions import ClientError

from .models import JobEvent, JobRequest, JobStatus, MutationCommand, UploadSession


# Historical records remain immutable under this prefix.  The store reads them
# only when a neutral-prefix record does not exist; every new write is neutral.
HISTORICAL_LANDING_PREFIX = "s3-uploader-v2"


class JobAlreadyClaimed(RuntimeError):
    pass


class MissingRecord(KeyError):
    pass


class ConcurrentRecordUpdate(RuntimeError):
    pass


class RecordStateConflict(RuntimeError):
    """A conditional state transition lost to a newer durable record."""

    pass


@dataclass(frozen=True)
class StoredStatus:
    status: JobStatus
    etag: str


class S3JobStore:
    def __init__(
        self,
        s3_client: Any,
        bucket: str,
        prefix: str,
        *,
        historical_prefix: str = HISTORICAL_LANDING_PREFIX,
    ):
        self.s3 = s3_client
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.historical_prefix = historical_prefix.strip("/")

    def _key(self, suffix: str) -> str:
        return f"{self.prefix}/{suffix}"

    def _historical_key(self, suffix: str) -> str:
        return f"{self.historical_prefix}/{suffix}"

    @staticmethod
    def _missing(error: ClientError) -> bool:
        return error.response["Error"].get("Code") in {"NoSuchKey", "404"}

    def _get_record(self, suffix: str, record_id: str) -> dict[str, Any]:
        """Read neutral storage first, then the immutable historical prefix."""
        try:
            return self.s3.get_object(Bucket=self.bucket, Key=self._key(suffix))
        except ClientError as error:
            if not self._missing(error) or self.historical_prefix == self.prefix:
                raise
        try:
            return self.s3.get_object(Bucket=self.bucket, Key=self._historical_key(suffix))
        except ClientError as error:
            if self._missing(error):
                raise MissingRecord(record_id) from error
            raise

    def session_key(self, session_id: str) -> str:
        return self._key(f"sessions/{session_id}.json")

    def request_key(self, job_id: str) -> str:
        return self._key(f"jobs/{job_id}/request.json")

    def status_key(self, job_id: str) -> str:
        return self._key(f"jobs/{job_id}/status.json")

    def mutation_command_key(self, mutation_id: str) -> str:
        return self._key(f"mutations/{mutation_id}/command.json")

    def mutation_request_key(self, owner_user_id: str, request_id: str) -> str:
        digest = hashlib.sha256(f"{owner_user_id}\x1f{request_id}".encode("utf-8")).hexdigest()
        return self._key(f"mutation-requests/{digest}.json")

    # The unchanged v1 browser is a session-oriented client.  These records
    # intentionally live alongside the worker job records, rather than on an
    # ECS task filesystem, so a replacement API task can resume a browser
    # session without losing upload state.
    def compat_session_key(self, session_id: str) -> str:
        return self._key(f"compat-sessions/{session_id}/session.json")

    def lease_key(self, lease_id: str) -> str:
        return self._key(f"worker-leases/{lease_id}/lease.json")

    def put_lease(self, lease: dict[str, Any], *, create_only: bool = False, expected_etag: str | None = None) -> None:
        options = {"IfNoneMatch": "*"} if create_only else {}
        if expected_etag is not None:
            options["IfMatch"] = expected_etag
        self._put_json(self.lease_key(str(lease["lease_id"])), lease, **options)

    def get_lease(self, lease_id: str) -> dict[str, Any]:
        return self.get_lease_with_etag(lease_id)[0]

    def get_lease_with_etag(self, lease_id: str) -> tuple[dict[str, Any], str]:
        try:
            response = self.s3.get_object(Bucket=self.bucket, Key=self.lease_key(lease_id))
            return self._read_json(response), response.get("ETag", "").strip('"')
        except ClientError as error:
            if error.response["Error"].get("Code") in {"NoSuchKey", "404"}:
                raise MissingRecord(lease_id) from error
            raise

    def update_lease(
        self,
        lease_id: str,
        changes: dict[str, Any],
        *,
        attempts: int = 4,
        guard: Callable[[dict[str, Any]], bool] | None = None,
    ) -> dict[str, Any]:
        return self._update_json_record(self.get_lease_with_etag, self.put_lease, lease_id, changes, attempts, guard)

    def put_compat_session(self, session: dict[str, Any], *, create_only: bool = False, expected_etag: str | None = None) -> None:
        options = {"IfNoneMatch": "*"} if create_only else {}
        if expected_etag is not None:
            options["IfMatch"] = expected_etag
        self._put_json(self.compat_session_key(str(session["session_id"])), session, **options)

    def get_compat_session(self, session_id: str) -> dict[str, Any]:
        return self.get_compat_session_with_etag(session_id)[0]

    def get_compat_session_with_etag(self, session_id: str) -> tuple[dict[str, Any], str]:
        response = self._get_record(f"compat-sessions/{session_id}/session.json", session_id)
        return self._read_json(response), response.get("ETag", "").strip('"')

    def update_compat_session(
        self,
        session_id: str,
        changes: dict[str, Any],
        *,
        attempts: int = 4,
        guard: Callable[[dict[str, Any]], bool] | None = None,
    ) -> dict[str, Any]:
        return self._update_json_record(
            self._get_compat_session_for_update,
            self.put_compat_session,
            session_id,
            changes,
            attempts,
            guard,
        )

    def _get_compat_session_for_update(self, session_id: str) -> tuple[dict[str, Any], str]:
        """Materialise a historical browser session before its first new write.

        Historical objects remain untouched.  This only handles an in-flight
        pre-cutover browser session that must record a V3 status transition.
        """
        try:
            response = self.s3.get_object(Bucket=self.bucket, Key=self.compat_session_key(session_id))
            return self._read_json(response), response.get("ETag", "").strip('"')
        except ClientError as error:
            if not self._missing(error):
                raise
        session = self.get_compat_session(session_id)
        try:
            self.put_compat_session(session, create_only=True)
        except ClientError as error:
            if not self._is_precondition_failure(error):
                raise
        response = self.s3.get_object(Bucket=self.bucket, Key=self.compat_session_key(session_id))
        return self._read_json(response), response.get("ETag", "").strip('"')

    @staticmethod
    def _is_precondition_failure(error: ClientError) -> bool:
        return error.response["Error"].get("Code") in {"PreconditionFailed", "ConditionalRequestConflict", "412"}

    def _update_json_record(
        self,
        reader: Any,
        writer: Any,
        record_id: str,
        changes: dict[str, Any],
        attempts: int,
        guard: Callable[[dict[str, Any]], bool] | None,
    ) -> dict[str, Any]:
        for _ in range(attempts):
            current, etag = reader(record_id)
            if guard is not None and not guard(current):
                raise RecordStateConflict(record_id)
            updated = {**current, **changes, "state_version": int(current.get("state_version", 0)) + 1}
            try:
                writer(updated, expected_etag=etag)
                return updated
            except ClientError as error:
                if not self._is_precondition_failure(error):
                    raise
        raise ConcurrentRecordUpdate(f"Concurrent update did not settle for {record_id}")

    def _put_json(self, key: str, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        return self.s3.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=json.dumps(payload, sort_keys=True, default=str).encode("utf-8"),
            ContentType="application/json",
            ServerSideEncryption="aws:kms",
            **kwargs,
        )

    @staticmethod
    def _read_json(response: dict[str, Any]) -> dict[str, Any]:
        return json.loads(response["Body"].read().decode("utf-8"))

    def put_session(self, session: UploadSession) -> None:
        self._put_json(self.session_key(session.session_id), session.model_dump(mode="json"), IfNoneMatch="*")

    def get_session(self, session_id: str) -> UploadSession:
        return UploadSession.model_validate(self._read_json(self._get_record(f"sessions/{session_id}.json", session_id)))

    def put_request(self, request: JobRequest) -> None:
        self._put_json(self.request_key(request.job_id), request.model_dump(mode="json"), IfNoneMatch="*")

    def get_request(self, job_id: str) -> JobRequest:
        return JobRequest.model_validate(self._read_json(self._get_record(f"jobs/{job_id}/request.json", job_id)))

    def put_mutation_command(self, command: MutationCommand) -> None:
        self._put_json(
            self.mutation_command_key(command.mutation_id), command.model_dump(mode="json"), IfNoneMatch="*"
        )

    def get_mutation_command(self, mutation_id: str) -> MutationCommand:
        response = self._get_record(f"mutations/{mutation_id}/command.json", mutation_id)
        return MutationCommand.model_validate(self._read_json(response))

    def put_mutation_request(self, *, owner_user_id: str, request_id: str, mutation_id: str) -> bool:
        """Return false when this owner/request pair already has a command."""
        try:
            self._put_json(
                self.mutation_request_key(owner_user_id, request_id),
                {"owner_user_id": owner_user_id, "request_id": request_id, "mutation_id": mutation_id},
                IfNoneMatch="*",
            )
            return True
        except ClientError as error:
            if self._is_precondition_failure(error):
                return False
            raise

    def get_mutation_request(self, *, owner_user_id: str, request_id: str) -> str:
        try:
            response = self.s3.get_object(
                Bucket=self.bucket, Key=self.mutation_request_key(owner_user_id, request_id)
            )
            return str(self._read_json(response)["mutation_id"])
        except ClientError as error:
            if error.response["Error"].get("Code") in {"NoSuchKey", "404"}:
                raise MissingRecord(request_id) from error
            raise

    def put_status(self, status: JobStatus, expected_etag: str | None = None) -> str:
        options = {} if expected_etag is None else {"IfMatch": expected_etag}
        response = self._put_json(self.status_key(status.job_id), status.model_dump(mode="json"), **options)
        event = JobEvent(job_id=status.job_id, sequence=int(datetime.now(timezone.utc).timestamp() * 1_000_000), status=status)
        self._put_json(self._key(f"jobs/{status.job_id}/events/{event.sequence}.json"), event.model_dump(mode="json"), IfNoneMatch="*")
        return response.get("ETag", "").strip('"')

    def get_status(self, job_id: str) -> StoredStatus:
        response = self._get_record(f"jobs/{job_id}/status.json", job_id)
        return StoredStatus(JobStatus.model_validate(self._read_json(response)), response.get("ETag", "").strip('"'))

    def claim(self, job_id: str, worker_id: str) -> None:
        payload = {"schema_version": 1, "job_id": job_id, "worker_id": worker_id, "claimed_at": datetime.now(timezone.utc).isoformat()}
        try:
            self._put_json(self._key(f"jobs/{job_id}/claim.json"), payload, IfNoneMatch="*")
        except ClientError as error:
            if error.response["Error"].get("Code") in {"PreconditionFailed", "412"}:
                raise JobAlreadyClaimed(job_id) from error
            raise

    @staticmethod
    def object_sha256(head_object: dict[str, Any]) -> str:
        value = head_object.get("Metadata", {}).get("sha256", "").lower()
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise ValueError("raw upload must carry a sha256 metadata value")
        return value

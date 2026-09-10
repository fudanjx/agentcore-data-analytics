"""S3-only durable storage for sessions, idempotent jobs, and worker claims."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from botocore.exceptions import ClientError

from .models import JobEvent, JobRequest, JobStatus, UploadSession


class JobAlreadyClaimed(RuntimeError):
    pass


class MissingRecord(KeyError):
    pass


class ConcurrentRecordUpdate(RuntimeError):
    pass


@dataclass(frozen=True)
class StoredStatus:
    status: JobStatus
    etag: str


class S3JobStore:
    def __init__(self, s3_client: Any, bucket: str, prefix: str):
        self.s3 = s3_client
        self.bucket = bucket
        self.prefix = prefix.strip("/")

    def _key(self, suffix: str) -> str:
        return f"{self.prefix}/{suffix}"

    def session_key(self, session_id: str) -> str:
        return self._key(f"sessions/{session_id}.json")

    def request_key(self, job_id: str) -> str:
        return self._key(f"jobs/{job_id}/request.json")

    def status_key(self, job_id: str) -> str:
        return self._key(f"jobs/{job_id}/status.json")

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

    def update_lease(self, lease_id: str, changes: dict[str, Any], *, attempts: int = 4) -> dict[str, Any]:
        return self._update_json_record(self.get_lease_with_etag, self.put_lease, lease_id, changes, attempts)

    def put_compat_session(self, session: dict[str, Any], *, create_only: bool = False, expected_etag: str | None = None) -> None:
        options = {"IfNoneMatch": "*"} if create_only else {}
        if expected_etag is not None:
            options["IfMatch"] = expected_etag
        self._put_json(self.compat_session_key(str(session["session_id"])), session, **options)

    def get_compat_session(self, session_id: str) -> dict[str, Any]:
        return self.get_compat_session_with_etag(session_id)[0]

    def get_compat_session_with_etag(self, session_id: str) -> tuple[dict[str, Any], str]:
        try:
            response = self.s3.get_object(Bucket=self.bucket, Key=self.compat_session_key(session_id))
            return self._read_json(response), response.get("ETag", "").strip('"')
        except ClientError as error:
            if error.response["Error"].get("Code") in {"NoSuchKey", "404"}:
                raise MissingRecord(session_id) from error
            raise

    def update_compat_session(self, session_id: str, changes: dict[str, Any], *, attempts: int = 4) -> dict[str, Any]:
        return self._update_json_record(self.get_compat_session_with_etag, self.put_compat_session, session_id, changes, attempts)

    @staticmethod
    def _is_precondition_failure(error: ClientError) -> bool:
        return error.response["Error"].get("Code") in {"PreconditionFailed", "ConditionalRequestConflict", "412"}

    def _update_json_record(self, reader: Any, writer: Any, record_id: str, changes: dict[str, Any], attempts: int) -> dict[str, Any]:
        for _ in range(attempts):
            current, etag = reader(record_id)
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
        try:
            return UploadSession.model_validate(self._read_json(self.s3.get_object(Bucket=self.bucket, Key=self.session_key(session_id))))
        except ClientError as error:
            if error.response["Error"].get("Code") in {"NoSuchKey", "404"}:
                raise MissingRecord(session_id) from error
            raise

    def put_request(self, request: JobRequest) -> None:
        self._put_json(self.request_key(request.job_id), request.model_dump(mode="json"), IfNoneMatch="*")

    def get_request(self, job_id: str) -> JobRequest:
        try:
            return JobRequest.model_validate(self._read_json(self.s3.get_object(Bucket=self.bucket, Key=self.request_key(job_id))))
        except ClientError as error:
            if error.response["Error"].get("Code") in {"NoSuchKey", "404"}:
                raise MissingRecord(job_id) from error
            raise

    def put_status(self, status: JobStatus, expected_etag: str | None = None) -> str:
        options = {} if expected_etag is None else {"IfMatch": expected_etag}
        response = self._put_json(self.status_key(status.job_id), status.model_dump(mode="json"), **options)
        event = JobEvent(job_id=status.job_id, sequence=int(datetime.now(timezone.utc).timestamp() * 1_000_000), status=status)
        self._put_json(self._key(f"jobs/{status.job_id}/events/{event.sequence}.json"), event.model_dump(mode="json"), IfNoneMatch="*")
        return response.get("ETag", "").strip('"')

    def get_status(self, job_id: str) -> StoredStatus:
        try:
            response = self.s3.get_object(Bucket=self.bucket, Key=self.status_key(job_id))
            return StoredStatus(JobStatus.model_validate(self._read_json(response)), response.get("ETag", "").strip('"'))
        except ClientError as error:
            if error.response["Error"].get("Code") in {"NoSuchKey", "404"}:
                raise MissingRecord(job_id) from error
            raise

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

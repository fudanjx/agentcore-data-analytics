"""S3 conditional-lease implementation for uploader table mutations.

S3 conditional writes make this safe across local service restarts and future
replicas without adding a DynamoDB dependency.  The object contains only safe
operational metadata, never upload content or personal data.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError


class TableLockError(RuntimeError):
    pass


class TableLockedError(TableLockError):
    def __init__(self, details: dict[str, Any]):
        super().__init__("The selected table is busy with another uploader operation")
        self.details = details


class TableQueueError(RuntimeError):
    pass


@dataclass
class TableLease:
    key: str
    etag: str
    owner_token: str
    payload: dict[str, Any]


@dataclass
class TableQueueEntry:
    key: str
    etag: str
    payload: dict[str, Any]


def target_id(table_bucket_arn: str, namespace: str, table: str) -> str:
    value = "\x1f".join((table_bucket_arn, namespace, table)).encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def is_precondition_failure(error: ClientError) -> bool:
    code = error.response.get("Error", {}).get("Code", "")
    return code in {"PreconditionFailed", "ConditionalRequestConflict", "412"}


class S3TableLockManager:
    def __init__(self, s3_client: Any, bucket: str, prefix: str, lease_minutes: int = 120):
        self.s3 = s3_client
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.lease_minutes = lease_minutes

    def _key(self, table_bucket_arn: str, namespace: str, table: str) -> str:
        return f"{self.prefix}/{target_id(table_bucket_arn, namespace, table)}.json"

    def _body(self, payload: dict[str, Any]) -> bytes:
        return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")

    def _expires_at(self) -> str:
        return (datetime.now(timezone.utc) + timedelta(minutes=self.lease_minutes)).isoformat()

    def _read(self, key: str) -> tuple[dict[str, Any], str]:
        response = self.s3.get_object(Bucket=self.bucket, Key=key)
        return json.loads(response["Body"].read()), response.get("ETag", "").strip('"')

    @staticmethod
    def _expired(payload: dict[str, Any]) -> bool:
        try:
            return datetime.fromisoformat(payload["lease_expires_at"]).astimezone(timezone.utc) <= datetime.now(timezone.utc)
        except (KeyError, TypeError, ValueError):
            return False

    def acquire(self, *, table_bucket_arn: str, namespace: str, table: str, owner_token: str,
                user_id: str, request_id: str, session_id: str | None, operation: str, phase: str) -> TableLease:
        key = self._key(table_bucket_arn, namespace, table)
        payload = {
            "owner_token": owner_token,
            "user_id": user_id,
            "request_id": request_id,
            "session_id": session_id,
            "operation": operation,
            "phase": phase,
            "acquired_at": datetime.now(timezone.utc).isoformat(),
            "lease_expires_at": self._expires_at(),
        }
        try:
            response = self.s3.put_object(
                Bucket=self.bucket, Key=key, Body=self._body(payload), ContentType="application/json",
                ServerSideEncryption="AES256", IfNoneMatch="*",
            )
            return TableLease(key, response.get("ETag", "").strip('"'), owner_token, payload)
        except ClientError as error:
            if not is_precondition_failure(error):
                raise TableLockError("Unable to acquire the table mutation lock") from error
        except BotoCoreError as error:
            raise TableLockError("Unable to acquire the table mutation lock") from error
        try:
            existing, etag = self._read(key)
        except (ClientError, BotoCoreError) as error:
            raise TableLockError("Unable to read the current table mutation lock") from error
        if existing.get("request_id") == request_id and existing.get("owner_token") == owner_token:
            return TableLease(key, etag, owner_token, existing)
        if not self._expired(existing):
            raise TableLockedError(existing)
        payload["acquired_at"] = datetime.now(timezone.utc).isoformat()
        try:
            response = self.s3.put_object(
                Bucket=self.bucket, Key=key, Body=self._body(payload), ContentType="application/json",
                ServerSideEncryption="AES256", IfMatch=etag,
            )
            return TableLease(key, response.get("ETag", "").strip('"'), owner_token, payload)
        except ClientError as error:
            if is_precondition_failure(error):
                raise TableLockedError(existing) from error
            raise TableLockError("Unable to take over the expired table mutation lock") from error
        except BotoCoreError as error:
            raise TableLockError("Unable to take over the expired table mutation lock") from error

    def renew(self, lease: TableLease, phase: str, glue_job_run_id: str | None = None) -> TableLease:
        payload = {**lease.payload, "phase": phase, "lease_expires_at": self._expires_at()}
        if glue_job_run_id:
            payload["glue_job_run_id"] = glue_job_run_id
        try:
            response = self.s3.put_object(
                Bucket=self.bucket, Key=lease.key, Body=self._body(payload), ContentType="application/json",
                ServerSideEncryption="AES256", IfMatch=lease.etag,
            )
        except ClientError as error:
            raise TableLockError("Unable to renew the table mutation lock") from error
        except BotoCoreError as error:
            raise TableLockError("Unable to renew the table mutation lock") from error
        return TableLease(lease.key, response.get("ETag", "").strip('"'), lease.owner_token, payload)

    def release(self, lease: TableLease) -> None:
        try:
            self.s3.delete_object(Bucket=self.bucket, Key=lease.key, IfMatch=lease.etag)
        except ClientError as error:
            if is_precondition_failure(error):
                raise TableLockError("The table mutation lock changed before release") from error
            raise TableLockError("Unable to release the table mutation lock") from error
        except BotoCoreError as error:
            raise TableLockError("Unable to release the table mutation lock") from error

    def list_leases(self) -> list[TableLease]:
        """List only the bounded lock prefix for startup reconciliation."""
        leases: list[TableLease] = []
        try:
            paginator = self.s3.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self.bucket, Prefix=f"{self.prefix}/"):
                for item in page.get("Contents", []):
                    payload, etag = self._read(item["Key"])
                    owner_token = payload.get("owner_token")
                    if isinstance(owner_token, str) and owner_token and etag:
                        leases.append(TableLease(item["Key"], etag, owner_token, payload))
        except (ClientError, BotoCoreError) as error:
            raise TableLockError("Unable to list table mutation locks") from error
        return leases


class S3TableMutationQueue:
    """Durable, per-table FIFO queue held until Glue releases the mutation."""

    def __init__(self, s3_client: Any, bucket: str, prefix: str, expiry_minutes: int = 90):
        self.s3 = s3_client
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.expiry_minutes = expiry_minutes

    def _prefix(self, table_bucket_arn: str, namespace: str, table: str) -> str:
        return f"{self.prefix}/{target_id(table_bucket_arn, namespace, table)}"

    @staticmethod
    def _body(payload: dict[str, Any]) -> bytes:
        return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")

    @staticmethod
    def _expired(payload: dict[str, Any]) -> bool:
        try:
            return datetime.fromisoformat(payload["expires_at"]).astimezone(timezone.utc) <= datetime.now(timezone.utc)
        except (KeyError, TypeError, ValueError):
            return False

    def _read(self, key: str) -> TableQueueEntry:
        response = self.s3.get_object(Bucket=self.bucket, Key=key)
        return TableQueueEntry(key, response.get("ETag", "").strip('"'), json.loads(response["Body"].read()))

    def enqueue(self, *, table_bucket_arn: str, namespace: str, table: str, job_id: str,
                user_id: str, session_id: str | None, operation: str, created_at: str) -> TableQueueEntry:
        prefix = self._prefix(table_bucket_arn, namespace, table)
        sequence = created_at.replace("-", "").replace(":", "").replace("+", "").replace(".", "").replace("T", "-")
        key = f"{prefix}/{sequence}-{job_id}.json"
        payload = {
            "job_id": job_id, "user_id": user_id, "session_id": session_id,
            "operation": operation, "created_at": created_at, "state": "PENDING",
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=self.expiry_minutes)).isoformat(),
        }
        try:
            response = self.s3.put_object(
                Bucket=self.bucket, Key=key, Body=self._body(payload), ContentType="application/json",
                ServerSideEncryption="AES256", IfNoneMatch="*",
            )
            return TableQueueEntry(key, response.get("ETag", "").strip('"'), payload)
        except ClientError as error:
            if not is_precondition_failure(error):
                raise TableQueueError("Unable to queue the table mutation") from error
            try:
                return self._read(key)
            except (ClientError, BotoCoreError) as read_error:
                raise TableQueueError("Unable to read the queued table mutation") from read_error
        except BotoCoreError as error:
            raise TableQueueError("Unable to queue the table mutation") from error

    def mark_ready(self, entry: TableQueueEntry) -> TableQueueEntry:
        payload = {**entry.payload, "state": "READY"}
        return self._write(entry, payload)

    def mark_running(self, entry: TableQueueEntry) -> TableQueueEntry:
        payload = {
            **entry.payload, "state": "RUNNING_GLUE",
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=120)).isoformat(),
        }
        return self._write(entry, payload)

    def _write(self, entry: TableQueueEntry, payload: dict[str, Any]) -> TableQueueEntry:
        try:
            response = self.s3.put_object(
                Bucket=self.bucket, Key=entry.key, Body=self._body(payload), ContentType="application/json",
                ServerSideEncryption="AES256", IfMatch=entry.etag,
            )
            return TableQueueEntry(entry.key, response.get("ETag", "").strip('"'), payload)
        except (ClientError, BotoCoreError) as error:
            raise TableQueueError("Unable to update the table mutation queue") from error

    def position(self, entry: TableQueueEntry) -> int:
        entries: list[TableQueueEntry] = []
        try:
            paginator = self.s3.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self.bucket, Prefix=entry.key.rsplit("/", 1)[0] + "/"):
                for item in page.get("Contents", []):
                    candidate = self._read(item["Key"])
                    if self._expired(candidate.payload):
                        self.release(candidate, ignore_changed=True)
                    else:
                        entries.append(candidate)
        except (ClientError, BotoCoreError) as error:
            raise TableQueueError("Unable to inspect the table mutation queue") from error
        entries.sort(key=lambda candidate: candidate.key)
        for index, candidate in enumerate(entries, start=1):
            if candidate.key == entry.key:
                return index
        raise TableQueueError("The queued table mutation no longer exists")

    def release(self, entry: TableQueueEntry, *, ignore_changed: bool = False) -> None:
        try:
            self.s3.delete_object(Bucket=self.bucket, Key=entry.key, IfMatch=entry.etag)
        except ClientError as error:
            if ignore_changed and is_precondition_failure(error):
                return
            raise TableQueueError("Unable to release the table mutation queue entry") from error
        except BotoCoreError as error:
            raise TableQueueError("Unable to release the table mutation queue entry") from error

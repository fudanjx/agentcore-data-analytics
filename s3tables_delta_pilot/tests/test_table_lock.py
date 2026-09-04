import json
import unittest
from datetime import datetime, timedelta, timezone
from io import BytesIO

from botocore.exceptions import ClientError

from s3tables_delta_pilot.table_lock import S3TableLockManager, TableLockedError


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.serial = 0

    def _error(self, code):
        raise ClientError({"Error": {"Code": code, "Message": code}}, "test")

    def put_object(self, *, Bucket, Key, Body, IfNoneMatch=None, IfMatch=None, **kwargs):
        current = self.objects.get((Bucket, Key))
        if IfNoneMatch == "*" and current:
            self._error("PreconditionFailed")
        if IfMatch is not None and (not current or current[0] != IfMatch):
            self._error("PreconditionFailed")
        self.serial += 1
        etag = f"etag-{self.serial}"
        self.objects[(Bucket, Key)] = (etag, bytes(Body))
        return {"ETag": f'"{etag}"'}

    def get_object(self, *, Bucket, Key):
        try:
            etag, body = self.objects[(Bucket, Key)]
        except KeyError:
            self._error("NoSuchKey")
        return {"ETag": f'"{etag}"', "Body": BytesIO(body)}

    def delete_object(self, *, Bucket, Key, IfMatch=None):
        current = self.objects.get((Bucket, Key))
        if IfMatch is not None and (not current or current[0] != IfMatch):
            self._error("PreconditionFailed")
        self.objects.pop((Bucket, Key), None)
        return {}


class TableLockTests(unittest.TestCase):
    def setUp(self):
        self.s3 = FakeS3()
        self.manager = S3TableLockManager(self.s3, "stage", "locks", lease_minutes=120)
        self.target = dict(table_bucket_arn="arn:bucket/example", namespace="pilot", table="surgery")

    def test_only_one_owner_can_acquire_and_release_is_etag_protected(self):
        lease = self.manager.acquire(**self.target, owner_token="owner-a", user_id="alice", request_id="req-a", session_id="session-a", operation="append", phase="STARTING")
        with self.assertRaises(TableLockedError) as busy:
            self.manager.acquire(**self.target, owner_token="owner-b", user_id="bob", request_id="req-b", session_id="session-b", operation="append", phase="STARTING")
        self.assertEqual("append", busy.exception.details["operation"])
        renewed = self.manager.renew(lease, "GLUE_RUNNING", "jr-123")
        self.assertEqual("GLUE_RUNNING", renewed.payload["phase"])
        self.manager.release(renewed)
        replacement = self.manager.acquire(**self.target, owner_token="owner-b", user_id="bob", request_id="req-b", session_id="session-b", operation="append", phase="STARTING")
        self.assertEqual("owner-b", replacement.owner_token)

    def test_expired_lease_is_conditionally_taken_over(self):
        lease = self.manager.acquire(**self.target, owner_token="owner-a", user_id="alice", request_id="req-a", session_id="session-a", operation="append", phase="STARTING")
        payload = {**lease.payload, "lease_expires_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()}
        self.s3.objects[("stage", lease.key)] = (lease.etag, json.dumps(payload).encode())
        replacement = self.manager.acquire(**self.target, owner_token="owner-b", user_id="bob", request_id="req-b", session_id="session-b", operation="delete", phase="STARTING")
        self.assertEqual("owner-b", replacement.owner_token)
        self.assertEqual("delete", replacement.payload["operation"])

    def test_same_owner_request_is_idempotent(self):
        first = self.manager.acquire(**self.target, owner_token="owner-a", user_id="alice", request_id="req-a", session_id="session-a", operation="append", phase="STARTING")
        second = self.manager.acquire(**self.target, owner_token="owner-a", user_id="alice", request_id="req-a", session_id="session-a", operation="append", phase="STARTING")
        self.assertEqual(first.key, second.key)
        self.assertEqual(first.etag, second.etag)


if __name__ == "__main__":
    unittest.main()

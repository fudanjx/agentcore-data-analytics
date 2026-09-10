import io
import unittest

from botocore.exceptions import ClientError

from s3tables_uploader_v2.table_lock import S3TableLockManager, TableLockedError


class FakeS3:
    def __init__(self):
        self.items = {}
        self.version = 0

    def _error(self, code):
        return ClientError({"Error": {"Code": code}}, "PutObject")

    def put_object(self, Bucket, Key, Body, **kwargs):
        if kwargs.get("IfNoneMatch") == "*" and Key in self.items:
            raise self._error("PreconditionFailed")
        if kwargs.get("IfMatch") and (Key not in self.items or kwargs["IfMatch"] != self.items[Key][1]):
            raise self._error("PreconditionFailed")
        self.version += 1
        etag = f"etag-{self.version}"
        self.items[Key] = (bytes(Body), etag)
        return {"ETag": etag}

    def get_object(self, Bucket, Key):
        body, etag = self.items[Key]
        return {"Body": io.BytesIO(body), "ETag": etag}

    def delete_object(self, Bucket, Key, **kwargs):
        if kwargs.get("IfMatch") != self.items[Key][1]:
            raise self._error("PreconditionFailed")
        del self.items[Key]


class TableLockTests(unittest.TestCase):
    def test_same_table_blocks_second_mutation_until_owner_releases(self):
        manager = S3TableLockManager(FakeS3(), "landing", "prefix/table-locks")
        first = manager.acquire(
            table_bucket_arn="arn", namespace="ns", table="target", owner_token="job-1",
            user_id="user-1", request_id="job-1", session_id="session-1", operation="append", phase="STARTING_GLUE",
        )
        with self.assertRaises(TableLockedError):
            manager.acquire(
                table_bucket_arn="arn", namespace="ns", table="target", owner_token="job-2",
                user_id="user-2", request_id="job-2", session_id="session-2", operation="append", phase="STARTING_GLUE",
            )
        manager.release(first)
        second = manager.acquire(
            table_bucket_arn="arn", namespace="ns", table="target", owner_token="job-2",
            user_id="user-2", request_id="job-2", session_id="session-2", operation="append", phase="STARTING_GLUE",
        )
        self.assertEqual(second.payload["request_id"], "job-2")

    def test_different_tables_do_not_block_each_other(self):
        manager = S3TableLockManager(FakeS3(), "landing", "prefix/table-locks")
        first = manager.acquire(table_bucket_arn="arn", namespace="ns", table="one", owner_token="one", user_id="user", request_id="one", session_id=None, operation="append", phase="STARTING_GLUE")
        second = manager.acquire(table_bucket_arn="arn", namespace="ns", table="two", owner_token="two", user_id="user", request_id="two", session_id=None, operation="append", phase="STARTING_GLUE")
        self.assertNotEqual(first.key, second.key)

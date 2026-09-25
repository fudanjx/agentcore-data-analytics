import io
import unittest
from datetime import datetime, timezone

from botocore.exceptions import ClientError

from s3tables_uploader.job_store import S3JobStore
from s3tables_uploader.models import Destination, JobRequest, JobStatus


class FakeS3:
    def __init__(self): self.objects = {}
    def put_object(self, Bucket, Key, Body, **kwargs):
        if kwargs.get("IfNoneMatch") == "*" and Key in self.objects: raise Exception("duplicate")
        self.objects[Key] = Body if isinstance(Body, bytes) else Body.read()
        return {"ETag": "etag"}
    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "missing"}}, "GetObject")
        return {"Body": io.BytesIO(self.objects[Key]), "ETag": "etag"}


class JobStoreTests(unittest.TestCase):
    def test_writes_immutable_request_and_status(self):
        store = S3JobStore(FakeS3(), "bucket", "prefix")
        job = JobRequest(job_id="job", session_id="session", owner_user_id="owner", operation="create", destination=Destination(table_bucket_arn="arn", namespace="ah", table="admission"), source_key="prefix/uploads/session/raw/input.parquet", source_version_id="version", source_sha256="a" * 64, source_size_bytes=1)
        store.put_request(job)
        store.put_status(JobStatus(job_id="job", phase="QUEUED", message="queued"))
        self.assertEqual(store.get_request("job").source_version_id, "version")
        self.assertEqual(store.get_status("job").status.phase, "QUEUED")

    def test_persists_a_worker_lease_separately_from_job_state(self):
        store = S3JobStore(FakeS3(), "bucket", "prefix")
        lease = {"lease_id": "lease", "owner_user_id": "owner", "state": "STARTING", "worker_size": "BASE"}
        store.put_lease(lease, create_only=True)
        restored = store.get_lease("lease")
        self.assertEqual(restored["worker_size"], "BASE")
        self.assertEqual(restored["owner_user_id"], "owner")

    def test_reads_historical_records_without_writing_to_the_historical_prefix(self):
        s3 = FakeS3()
        historical = S3JobStore(s3, "bucket", "s3-uploader-v2", historical_prefix="")
        request = JobRequest(job_id="historical", session_id="session", owner_user_id="owner", operation="create", destination=Destination(table_bucket_arn="arn", namespace="ah", table="admission"), source_key="s3-uploader-v2/uploads/session/raw/input.parquet", source_version_id="version", source_sha256="a" * 64, source_size_bytes=1)
        historical.put_request(request)
        historical.put_status(JobStatus(job_id="historical", phase="SUCCEEDED", message="complete"))

        store = S3JobStore(s3, "bucket", "s3-uploader")
        self.assertEqual(store.get_request("historical").source_version_id, "version")
        self.assertEqual(store.get_status("historical").status.phase, "SUCCEEDED")
        self.assertFalse(any(key.startswith("s3-uploader/jobs/historical") for key in s3.objects))

    def test_updates_a_historical_browser_session_in_the_neutral_prefix(self):
        s3 = FakeS3()
        historical = S3JobStore(s3, "bucket", "s3-uploader-v2", historical_prefix="")
        historical.put_compat_session({"session_id": "session", "phase": "RECEIVED"}, create_only=True)
        store = S3JobStore(s3, "bucket", "s3-uploader")

        updated = store.update_compat_session("session", {"phase": "FAILED"})

        self.assertEqual(updated["phase"], "FAILED")
        self.assertIn("s3-uploader/compat-sessions/session/session.json", s3.objects)
        self.assertIn("s3-uploader-v2/compat-sessions/session/session.json", s3.objects)

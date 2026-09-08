import io
import unittest
from datetime import datetime, timezone

from s3tables_uploader_v2.job_store import S3JobStore
from s3tables_uploader_v2.models import Destination, JobRequest, JobStatus


class FakeS3:
    def __init__(self): self.objects = {}
    def put_object(self, Bucket, Key, Body, **kwargs):
        if kwargs.get("IfNoneMatch") == "*" and Key in self.objects: raise Exception("duplicate")
        self.objects[Key] = Body if isinstance(Body, bytes) else Body.read()
        return {"ETag": "etag"}
    def get_object(self, Bucket, Key): return {"Body": io.BytesIO(self.objects[Key]), "ETag": "etag"}


class JobStoreTests(unittest.TestCase):
    def test_writes_immutable_request_and_status(self):
        store = S3JobStore(FakeS3(), "bucket", "prefix")
        job = JobRequest(job_id="job", session_id="session", owner_user_id="owner", operation="create", destination=Destination(table_bucket_arn="arn", namespace="ah", table="admission"), source_key="prefix/uploads/session/raw/input.parquet", source_version_id="version", source_sha256="a" * 64, source_size_bytes=1)
        store.put_request(job)
        store.put_status(JobStatus(job_id="job", phase="QUEUED", message="queued"))
        self.assertEqual(store.get_request("job").source_version_id, "version")
        self.assertEqual(store.get_status("job").status.phase, "QUEUED")

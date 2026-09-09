import unittest

from fastapi.testclient import TestClient

from s3tables_uploader_v2.api import create_app
from s3tables_uploader_v2.config import Settings


class FakeS3:
    def __init__(self): self.items = {}; self.parts = {}
    def create_multipart_upload(self, **kwargs): self.create_args = kwargs; return {"UploadId": "upload"}
    def put_object(self, Bucket, Key, Body, **kwargs): self.items[Key] = Body; return {"ETag": "etag"}
    def get_object(self, Bucket, Key):
        import io
        return {"Body": io.BytesIO(self.items[Key]), "ETag": "etag"}
    def generate_presigned_url(self, *args, **kwargs): return "https://s3.example/part"
    def upload_part(self, Bucket, Key, UploadId, PartNumber, Body):
        self.parts[(Key, PartNumber)] = Body
        return {"ETag": f"part-{PartNumber}"}
    def complete_multipart_upload(self, Bucket, Key, UploadId, MultipartUpload):
        self.items[Key] = b"".join(self.parts[(Key, item["PartNumber"])] for item in MultipartUpload["Parts"])
        return {"VersionId": "version"}
    def abort_multipart_upload(self, **kwargs): return {}


class FakeSqs:
    def send_message(self, **kwargs): self.message = kwargs; return {"MessageId": "message"}


class ApiTests(unittest.TestCase):
    def setUp(self):
        env = {"AWS_REGION":"ap-southeast-1", "S3_UPLOADER_V2_LANDING_BUCKET":"landing", "S3_UPLOADER_V2_QUEUE_URL":"queue", "S3_UPLOADER_V2_LOGIN_PASSWORD":"password", "S3_UPLOADER_V2_LOGIN_SECRET":"x" * 32, "S3_UPLOADER_V2_API_BASE_URL":"https://s3-uploader-v2.bot-alex.com", "S3_UPLOADER_V2_GLUE_JOB_NAME":"s3-uploader-v2-ingest", "S3_UPLOADER_V2_ENV":"development", "S3_UPLOADER_V2_COOKIE_SECURE":"false"}
        self.s3 = FakeS3(); self.client = TestClient(create_app(Settings.from_environ(env), self.s3, FakeSqs()))

    def test_login_protects_session_creation_and_keeps_upload_off_api(self):
        self.assertEqual(self.client.post("/api/v2/upload-sessions", json={}).status_code, 401)
        self.assertEqual(self.client.post("/login", json={"password":"password"}).status_code, 200)
        response = self.client.post("/api/v2/upload-sessions", json={"file_name":"source.parquet", "content_type":"application/octet-stream", "source_sha256":"a" * 64})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.s3.create_args["Metadata"]["sha256"], "a" * 64)
        self.assertNotIn("file", response.json())

    def test_v1_multipart_upload_returns_a_durable_review_session(self):
        import io
        import pyarrow as pa
        import pyarrow.parquet as pq

        self.client.post("/login", json={"password":"password"})
        buffer = io.BytesIO(); pq.write_table(pa.table({"PAT_ENC_CSN_ID": ["1"], "AGE": [45]}), buffer)
        response = self.client.post(
            "/api/v2/upload-sessions",
            data={"mode":"create", "table_bucket_arn":"arn:aws:s3tables:ap-southeast-1:964340114883:bucket/ah-soc-delta-pilot", "namespace":"pilot", "table":"test_table"},
            files={"files": ("source.parquet", buffer.getvalue(), "application/octet-stream")},
        )
        self.assertEqual(response.status_code, 201, response.text)
        session = response.json()
        self.assertEqual(session["phase"], "READY_FOR_REVIEW")
        self.assertEqual(session["files"][0]["name"], "source.parquet")
        self.assertTrue(session["preflight"]["accepted"])
        restored = self.client.get(f"/api/v2/upload-sessions/{session['session_id']}")
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertEqual(restored.json()["session_id"], session["session_id"])

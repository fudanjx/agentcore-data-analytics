import unittest

from fastapi.testclient import TestClient

from s3tables_uploader_v2.api import create_app
from s3tables_uploader_v2.config import Settings


class FakeS3:
    def __init__(self): self.items = {}
    def create_multipart_upload(self, **kwargs): self.create_args = kwargs; return {"UploadId": "upload"}
    def put_object(self, Bucket, Key, Body, **kwargs): self.items[Key] = Body; return {"ETag": "etag"}
    def get_object(self, Bucket, Key):
        import io
        return {"Body": io.BytesIO(self.items[Key]), "ETag": "etag"}
    def generate_presigned_url(self, *args, **kwargs): return "https://s3.example/part"


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

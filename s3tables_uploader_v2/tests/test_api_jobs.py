import hashlib
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from s3tables_uploader_v2.api import create_app
from s3tables_uploader_v2.config import Settings
from s3tables_uploader_v2.job_store import S3JobStore
from s3tables_uploader_v2.models import JobStatus


class FakeS3:
    def __init__(self): self.items = {}; self.parts = {}
    def create_multipart_upload(self, **kwargs): self.create_args = kwargs; return {"UploadId": "upload"}
    def put_object(self, Bucket, Key, Body, **kwargs): self.items[Key] = Body; return {"ETag": "etag"}
    def get_object(self, Bucket, Key):
        import io
        return {"Body": io.BytesIO(self.items[Key]), "ETag": "etag"}
    def head_object(self, Bucket, Key):
        if Key not in self.items:
            raise KeyError(Key)
        return {"ETag": "etag"}
    def delete_object(self, Bucket, Key):
        del self.items[Key]
        return {}
    def generate_presigned_url(self, *args, **kwargs): return "https://s3.example/part"
    def upload_part(self, Bucket, Key, UploadId, PartNumber, Body):
        self.parts[(Key, PartNumber)] = Body
        return {"ETag": f"part-{PartNumber}"}
    def complete_multipart_upload(self, Bucket, Key, UploadId, MultipartUpload):
        self.items[Key] = b"".join(self.parts[(Key, item["PartNumber"])] for item in MultipartUpload["Parts"])
        return {"VersionId": "version"}
    def abort_multipart_upload(self, **kwargs): return {}
    def get_paginator(self, operation):
        assert operation == "list_objects_v2"
        client = self
        class Paginator:
            def paginate(self, Bucket, Prefix):
                yield {"Contents": [{"Key": key} for key in sorted(client.items) if key.startswith(Prefix)]}
        return Paginator()


class FakeSqs:
    def __init__(self): self.messages = []
    def send_message(self, **kwargs): self.message = kwargs; self.messages.append(kwargs); return {"MessageId": "message"}


class FakeGlue:
    def __init__(self): self.started = []
    def start_job_run(self, **kwargs):
        self.started.append(kwargs)
        return {"JobRunId": "jr-rollback"}
    def get_job_run(self, **kwargs):
        return {"JobRun": {"JobRunState": "SUCCEEDED"}}


class FakeS3Tables:
    def __init__(self):
        self.bucket_arn = "arn:aws:s3tables:ap-southeast-1:964340114883:bucket/ah-soc-delta-pilot"
        self.buckets = [{"arn": self.bucket_arn, "name": "ah-soc-delta-pilot", "type": "customer"}]
        self.namespaces = {self.bucket_arn: ["pilot"]}
        self.tables = {(self.bucket_arn, "pilot"): [{"name": "test_table"}]}
        self.metadata_location = "s3://example--table-s3/metadata/test.metadata.json"

    def list_table_buckets(self, **kwargs): return {"tableBuckets": self.buckets}
    def create_table_bucket(self, name):
        arn = f"arn:aws:s3tables:ap-southeast-1:964340114883:bucket/{name}"
        self.buckets.append({"arn": arn, "name": name, "type": "customer"})
        self.namespaces[arn] = []
        return {"arn": arn}
    def list_namespaces(self, tableBucketARN, **kwargs): return {"namespaces": [{"namespace": [name]} for name in self.namespaces[tableBucketARN]]}
    def create_namespace(self, tableBucketARN, namespace):
        self.namespaces[tableBucketARN].append(namespace[0])
        return {"tableBucketARN": tableBucketARN, "namespace": namespace}
    def list_tables(self, tableBucketARN, namespace, **kwargs): return {"tables": self.tables.get((tableBucketARN, namespace), [])}
    def get_table(self, tableBucketARN, namespace, name): return {"metadataLocation": self.metadata_location}
    def delete_table(self, tableBucketARN, namespace, name):
        self.tables[(tableBucketARN, namespace)] = [item for item in self.tables.get((tableBucketARN, namespace), []) if item["name"] != name]
        self.deleted = (tableBucketARN, namespace, name)


class ApiTests(unittest.TestCase):
    def setUp(self):
        env = {"AWS_REGION":"ap-southeast-1", "S3_UPLOADER_V2_LANDING_BUCKET":"landing", "S3_UPLOADER_V2_QUEUE_URL":"queue", "S3_UPLOADER_V2_LOGIN_PASSWORD":"password", "S3_UPLOADER_V2_LOGIN_SECRET":"x" * 32, "S3_UPLOADER_V2_API_BASE_URL":"https://s3-uploader-v2.bot-alex.com", "S3_UPLOADER_V2_GLUE_JOB_NAME":"s3-uploader-v2-ingest", "S3_UPLOADER_V2_ENV":"development", "S3_UPLOADER_V2_COOKIE_SECURE":"false"}
        self.s3 = FakeS3(); self.s3tables = FakeS3Tables(); self.glue = FakeGlue(); self.sqs = FakeSqs(); self.client = TestClient(create_app(Settings.from_environ(env), self.s3, self.sqs, self.s3tables, self.glue))

    def _append_session(self, session_id, *, contract_columns, payload_columns=None, preflight_columns=None, key_impact=None, files=None):
        import json
        bucket, namespace, table = self.s3tables.bucket_arn, "pilot", "test_table"
        scope = hashlib.sha256(f"{bucket}|{namespace}".encode()).hexdigest()[:16]
        self.s3.items[f"temp_s3_update/web_ingest/table_contracts/{scope}/{table}.json"] = json.dumps({
            "contract_version": 3, "schema": [{"name": "id", "type": "STRING"}],
            "deduplication_columns": contract_columns,
            "deduplication_mode": "keyed" if contract_columns else "unconfigured",
            "deduplication_policy": "skip-existing-key-report-conflict-v2",
        }).encode()
        store = S3JobStore(self.s3, "landing", "s3-uploader-v2")
        store.put_compat_session({
            "session_id": session_id, "owner_user_id": "local-admin", "expires_at": "2100-01-01T00:00:00+00:00",
            "mode": "append", "table_bucket_arn": bucket, "namespace": namespace, "table": table,
            "phase": "READY_FOR_REVIEW", "files": files or [{"name": "source.parquet", "sha256": "a" * 64,
                "source_key": "s3-uploader-v2/uploads/test/raw/source.parquet", "source_version_id": "version", "size_bytes": 1}],
            "preflight": {"accepted": True, "deduplication_candidates": payload_columns or [], "deduplication_columns": preflight_columns or [], "sanitization_review": {"manual_encryption_candidates": []}},
            "key_impact": key_impact,
        })
        return store, bucket, namespace, table, scope

    def test_v1_history_contract_reads_canonical_audit_projection_and_starts_rollback(self):
        import json
        self.client.post("/login", json={"password":"password"})
        bucket, namespace, table = self.s3tables.bucket_arn, "pilot", "test_table"
        scope = hashlib.sha256(f"{bucket}|{namespace}".encode()).hexdigest()[:16]
        self.s3.items[f"temp_s3_update/web_ingest/table_contracts/{scope}/{table}.json"] = b"{}"
        self.s3.items[f"temp_s3_update/web_ingest/upload_history/{scope}/{table}/UPLOAD-ABCDEF123456.json"] = json.dumps({
            "upload_id": "UPLOAD-ABCDEF123456", "status": "SUCCESS", "uploaded_at": "2026-09-10T00:00:00+00:00",
            "uploaded_by": "shared-operator", "previous_snapshot_id": "123", "target_table": table,
            "namespace": namespace, "table_bucket_arn": bucket, "reporting_month": "202609", "filenames": "[\"source.parquet\"]",
        }).encode()
        history = self.client.get("/api/upload-history", params={"table_bucket_arn": bucket, "namespace": namespace, "table": table})
        self.assertEqual(history.status_code, 200, history.text)
        self.assertEqual(history.json()["latest_rollback_upload_id"], "UPLOAD-ABCDEF123456")
        rollback = self.client.post("/api/rollbacks", json={"table_bucket_arn": bucket, "namespace": namespace, "table": table, "upload_id": "UPLOAD-ABCDEF123456", "confirm": True})
        self.assertEqual(rollback.status_code, 200, rollback.text)
        args = self.glue.started[-1]["Arguments"]
        self.assertEqual(args["--MODE"], "rollback")
        self.assertEqual(args["--ROLLBACK_SNAPSHOT_ID"], "123")
        self.assertEqual(args["--AUDIT_PREFIX"], f"s3://ah-data-analytics/temp_s3_update/web_ingest/upload_history/{scope}/{table}/")

    def test_administrator_can_create_namespace_and_delete_table(self):
        self.client.post("/login", json={"password":"password"})
        bucket = self.client.post("/api/buckets", json={"name": "new-analytics"})
        self.assertEqual(bucket.status_code, 201, bucket.text)
        namespace = self.client.post("/api/namespaces", json={"table_bucket_arn": self.s3tables.bucket_arn, "namespace": "reporting"})
        self.assertEqual(namespace.status_code, 201, namespace.text)
        scope = hashlib.sha256(f"{self.s3tables.bucket_arn}|pilot".encode()).hexdigest()[:16]
        self.s3.items[f"temp_s3_update/web_ingest/table_contracts/{scope}/test_table.json"] = b"{}"
        deleted = self.client.request("DELETE", "/api/tables", json={"table_bucket_arn": self.s3tables.bucket_arn, "namespace": "pilot", "table": "test_table"})
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertEqual(deleted.json()["deleted"], "test_table")
        self.assertEqual(self.s3tables.deleted, (self.s3tables.bucket_arn, "pilot", "test_table"))

    def test_v1_local_identity_emulation_exposes_admin_editor_and_unassigned_profiles(self):
        self.client.post("/login", json={"password":"password"})
        profiles = self.client.get("/api/dev/identity-profiles")
        self.assertEqual(profiles.status_code, 200, profiles.text)
        self.assertEqual([item["user_id"] for item in profiles.json()["profiles"]], ["local-admin", "local-editor", "local-unassigned"])

        editor = self.client.get("/api/identity", headers={"X-Pilot-User-Id": "local-editor"})
        self.assertEqual(editor.status_code, 200, editor.text)
        self.assertFalse(editor.json()["is_admin"])
        self.assertEqual(editor.json()["buckets"][0]["table_bucket_arn"], "arn:aws:s3tables:ap-southeast-1:964340114883:bucket/ah-soc-delta-pilot")
        denied = self.client.get("/api/buckets", headers={"X-Pilot-User-Id": "local-unassigned"})
        self.assertEqual(denied.status_code, 403, denied.text)

    def test_table_cards_restore_v1_iceberg_metadata_row_count(self):
        import json
        self.client.post("/login", json={"password":"password"})
        scope = hashlib.sha256(f"{self.s3tables.bucket_arn}|pilot".encode()).hexdigest()[:16]
        self.s3.items[f"temp_s3_update/web_ingest/table_contracts/{scope}/test_table.json"] = json.dumps({
            "deduplication_columns": ["id"], "deduplication_mode": "keyed",
        }).encode()
        self.s3.items["metadata/test.metadata.json"] = json.dumps({
            "current-snapshot-id": 9,
            "snapshots": [{"snapshot-id": 9, "summary": {"total-records": "42"}}],
        }).encode()
        response = self.client.get("/api/tables", params={"table_bucket_arn": self.s3tables.bucket_arn, "namespace": "pilot"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["tables"][0]["row_count"], 42)
        self.assertEqual(response.json()["tables"][0]["deduplication_columns"], ["id"])

    def test_append_derives_available_columns_from_the_locked_table_key(self):
        self.client.post("/login", json={"password":"password"})
        store, bucket, namespace, table, _ = self._append_session(
            "configured", contract_columns=["a", "b", "c", "d", "f"], preflight_columns=["c", "d", "f"],
        )
        response = self.client.post(f"/api/v2/upload-sessions/configured/ingestions", json={
            "request_id": "configured-key", "deduplication_mode": "keyed", "deduplication_columns": ["other"],
        })
        self.assertEqual(response.status_code, 202, response.text)
        request = store.get_request(response.json()["job_id"])
        self.assertEqual(request.deduplication_mode, "keyed")
        self.assertEqual(request.deduplication_columns, ["c", "d", "f"])

    def test_append_without_any_locked_key_columns_proceeds_without_deduplication(self):
        self.client.post("/login", json={"password":"password"})
        store, _, _, _, _ = self._append_session(
            "no-intersection", contract_columns=["a", "b", "c", "d", "f"], preflight_columns=[],
        )
        response = self.client.post(f"/api/v2/upload-sessions/no-intersection/ingestions", json={
            "request_id": "no-intersection", "deduplication_mode": "keyed", "deduplication_columns": ["other"],
        })
        self.assertEqual(response.status_code, 202, response.text)
        request = store.get_request(response.json()["job_id"])
        self.assertEqual(request.deduplication_mode, "none")
        self.assertEqual(request.deduplication_columns, [])

    def test_matching_multi_file_session_creates_one_immutable_job_manifest(self):
        self.client.post("/login", json={"password":"password"})
        files = [
            {"name": "first.parquet", "sha256": "a" * 64, "source_key": "s3-uploader-v2/uploads/test/raw/first.parquet", "source_version_id": "version-1", "size_bytes": 1},
            {"name": "second.parquet", "sha256": "b" * 64, "source_key": "s3-uploader-v2/uploads/test/raw/second.parquet", "source_version_id": "version-2", "size_bytes": 2},
        ]
        store, _, _, _, _ = self._append_session("multiple", contract_columns=[], payload_columns=[{"column": "id"}], files=files)
        response = self.client.post("/api/v2/upload-sessions/multiple/ingestions", json={
            "request_id": "multi-file", "deduplication_mode": "none", "deduplication_columns": [],
        })
        self.assertEqual(response.status_code, 202, response.text)
        request = store.get_request(response.json()["job_id"])
        self.assertEqual([source.name for source in request.source_files], ["first.parquet", "second.parquet"])
        self.assertEqual([source.source_version_id for source in request.source_files], ["version-1", "version-2"])

    def test_failed_worker_status_replaces_a_stale_queued_session(self):
        self.client.post("/login", json={"password":"password"})
        store, _, _, _, _ = self._append_session("failed-worker", contract_columns=["id"], preflight_columns=["id"])
        started = self.client.post(f"/api/v2/upload-sessions/failed-worker/ingestions", json={
            "request_id": "failed-worker", "deduplication_mode": "keyed", "deduplication_columns": [],
        })
        self.assertEqual(started.status_code, 202, started.text)
        job_id = started.json()["job_id"]
        store.put_status(JobStatus(job_id=job_id, phase="FAILED", message="The selected key columns are not present in the upload: acct_n", error_code="WorkerError"))
        response = self.client.get("/api/v2/upload-sessions/failed-worker")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["phase"], "FAILED")
        self.assertEqual(response.json()["error"]["code"], "WorkerError")

    def test_keyless_append_saves_its_first_user_selected_key(self):
        import json
        self.client.post("/login", json={"password":"password"})
        candidates = [{"column": "id", "deduplication_eligible": True}]
        impact = {"token": "ack", "deduplication_columns": ["id"], "expires_at": "2100-01-01T00:00:00+00:00"}
        _, bucket, namespace, table, scope = self._append_session("activate", contract_columns=[], payload_columns=candidates, key_impact=impact)
        response = self.client.post(f"/api/v2/upload-sessions/activate/ingestions", json={
            "request_id": "activate-key", "deduplication_mode": "keyed", "deduplication_columns": ["id"], "key_analysis_token": "ack",
        })
        self.assertEqual(response.status_code, 202, response.text)
        contract = json.loads(self.s3.items[f"temp_s3_update/web_ingest/table_contracts/{scope}/{table}.json"])
        self.assertEqual(contract["deduplication_columns"], ["id"])
        self.assertEqual(contract["deduplication_mode"], "keyed")

    def test_administrator_discovers_new_table_buckets(self):
        self.client.post("/login", json={"password":"password"})
        bucket = "arn:aws:s3tables:ap-southeast-1:964340114883:bucket/future-bucket"
        self.s3tables.buckets.append({"arn": bucket, "name": "future-bucket", "type": "customer"})
        self.s3tables.namespaces[bucket] = ["future"]
        response = self.client.get("/api/namespaces", params={"table_bucket_arn": bucket})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["namespaces"], ["future"])

    def test_skill_file_upload_download_and_confirmed_delete_use_v1_routes(self):
        from s3tables_uploader_v2 import skill_bundle

        self.client.post("/login", json={"password": "password"})
        bucket = self.s3tables.bucket_arn
        skill = b"---\ndescription: test skill\n---\n# Test\n"
        with patch.object(skill_bundle, "s3", self.s3):
            uploaded = self.client.post(
                "/api/skills/files",
                data={"table_bucket_arn": bucket, "paths_json": '["SKILL.md"]'},
                files={"files": ("SKILL.md", skill, "text/markdown")},
            )
            self.assertEqual(uploaded.status_code, 200, uploaded.text)
            self.assertEqual(uploaded.json()["uploaded_paths"], ["SKILL.md"])
            key = "skills/ah-soc-delta-pilot/SKILL.md"
            self.assertIn(key, self.s3.items)
            self.assertIn(b"name: ah-soc-delta-pilot", self.s3.items[key])

            downloaded = self.client.get("/api/skills/files/download", params={"table_bucket_arn": bucket, "path": "SKILL.md"})
            self.assertEqual(downloaded.status_code, 200, downloaded.text)
            self.assertEqual(downloaded.content, self.s3.items[key])
            self.assertIn("attachment", downloaded.headers["content-disposition"])

            rejected = self.client.request("DELETE", "/api/skills/files", json={"table_bucket_arn": bucket, "path": "SKILL.md", "confirm": False})
            self.assertEqual(rejected.status_code, 422, rejected.text)
            deleted = self.client.request("DELETE", "/api/skills/files", json={"table_bucket_arn": bucket, "path": "SKILL.md", "confirm": True})
            self.assertEqual(deleted.status_code, 200, deleted.text)
            self.assertEqual(deleted.json()["deleted_path"], "SKILL.md")
            self.assertNotIn(key, self.s3.items)

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
        self.assertEqual(session["phase"], "RECEIVED")
        self.assertEqual(session["files"][0]["name"], "source.parquet")
        self.assertIsNone(session["preflight"])
        restored = self.client.get(f"/api/v2/upload-sessions/{session['session_id']}")
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertEqual(restored.json()["session_id"], session["session_id"])

    def test_selected_files_create_and_attach_a_deterministic_worker_lease(self):
        import io
        import pyarrow as pa
        import pyarrow.parquet as pq

        env = {"AWS_REGION":"ap-southeast-1", "S3_UPLOADER_V2_LANDING_BUCKET":"landing", "S3_UPLOADER_V2_QUEUE_URL":"legacy", "S3_UPLOADER_V3_BASE_QUEUE_URL":"base", "S3_UPLOADER_V3_LARGE_QUEUE_URL":"large", "S3_UPLOADER_V3_LEASES_ENABLED":"true", "S3_UPLOADER_V2_LOGIN_PASSWORD":"password", "S3_UPLOADER_V2_LOGIN_SECRET":"x" * 32, "S3_UPLOADER_V2_API_BASE_URL":"https://s3-uploader-v2.bot-alex.com", "S3_UPLOADER_V2_GLUE_JOB_NAME":"s3-uploader-v2-ingest", "S3_UPLOADER_V2_ENV":"development", "S3_UPLOADER_V2_COOKIE_SECURE":"false"}
        s3, sqs = FakeS3(), FakeSqs()
        client = TestClient(create_app(Settings.from_environ(env), s3, sqs, FakeS3Tables()))
        client.post("/login", json={"password":"password"})
        buffer = io.BytesIO(); pq.write_table(pa.table({"id": ["1"]}), buffer)
        payload = buffer.getvalue()
        lease = client.post("/api/v3/worker-leases", json={"files": [{"name": "source.parquet", "size_bytes": len(payload)}]})
        self.assertEqual(lease.status_code, 201, lease.text)
        self.assertEqual(lease.json()["worker_size"], "BASE")
        self.assertEqual(sqs.message["QueueUrl"], "base")
        session = client.post(
            "/api/v2/upload-sessions",
            data={"mode":"create", "table_bucket_arn":"arn:aws:s3tables:ap-southeast-1:964340114883:bucket/ah-soc-delta-pilot", "namespace":"pilot", "table":"test_table", "worker_lease_id": lease.json()["lease_id"]},
            files={"files": ("source.parquet", payload, "application/octet-stream")},
        )
        self.assertEqual(session.status_code, 201, session.text)
        self.assertEqual(session.json()["worker_lease"]["lease_id"], lease.json()["lease_id"])

    def test_stale_lease_from_another_emulated_user_is_replaced(self):
        import io
        import pyarrow as pa
        import pyarrow.parquet as pq

        env = {"AWS_REGION":"ap-southeast-1", "S3_UPLOADER_V2_LANDING_BUCKET":"landing", "S3_UPLOADER_V2_QUEUE_URL":"legacy", "S3_UPLOADER_V3_BASE_QUEUE_URL":"base", "S3_UPLOADER_V3_LARGE_QUEUE_URL":"large", "S3_UPLOADER_V3_LEASES_ENABLED":"true", "S3_UPLOADER_V2_LOGIN_PASSWORD":"password", "S3_UPLOADER_V2_LOGIN_SECRET":"x" * 32, "S3_UPLOADER_V2_API_BASE_URL":"https://s3-uploader-v2.bot-alex.com", "S3_UPLOADER_V2_GLUE_JOB_NAME":"job", "S3_UPLOADER_V2_ENV":"development", "S3_UPLOADER_V2_COOKIE_SECURE":"false"}
        s3, sqs = FakeS3(), FakeSqs()
        client = TestClient(create_app(Settings.from_environ(env), s3, sqs, FakeS3Tables()))
        client.post("/login", json={"password":"password"})
        buffer = io.BytesIO(); pq.write_table(pa.table({"id": ["1"]}), buffer)
        payload = buffer.getvalue()
        editor_headers = {"X-Pilot-User-Id": "local-editor"}
        stale = client.post("/api/v3/worker-leases", headers=editor_headers, json={"files": [{"name": "source.parquet", "size_bytes": len(payload)}]}).json()
        response = client.post(
            "/api/v2/upload-sessions", headers={"X-Pilot-User-Id": "local-admin"},
            data={"mode":"create", "table_bucket_arn":"arn:aws:s3tables:ap-southeast-1:964340114883:bucket/ah-soc-delta-pilot", "namespace":"pilot", "table":"admin_table", "worker_lease_id": stale["lease_id"]},
            files={"files": ("source.parquet", payload, "application/octet-stream")},
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertNotEqual(response.json()["worker_lease"]["lease_id"], stale["lease_id"])
        store = S3JobStore(s3, "landing", "s3-uploader-v2")
        self.assertEqual(store.get_lease(response.json()["worker_lease"]["lease_id"])["owner_user_id"], "local-admin")

    def test_unattached_same_size_lease_is_reused_when_file_selection_changes(self):
        env = {"AWS_REGION":"ap-southeast-1", "S3_UPLOADER_V2_LANDING_BUCKET":"landing", "S3_UPLOADER_V2_QUEUE_URL":"legacy", "S3_UPLOADER_V3_BASE_QUEUE_URL":"base", "S3_UPLOADER_V3_LARGE_QUEUE_URL":"large", "S3_UPLOADER_V3_LEASES_ENABLED":"true", "S3_UPLOADER_V2_LOGIN_PASSWORD":"password", "S3_UPLOADER_V2_LOGIN_SECRET":"x" * 32, "S3_UPLOADER_V2_API_BASE_URL":"https://s3-uploader-v2.bot-alex.com", "S3_UPLOADER_V2_GLUE_JOB_NAME":"job", "S3_UPLOADER_V2_ENV":"development", "S3_UPLOADER_V2_COOKIE_SECURE":"false"}
        s3, sqs = FakeS3(), FakeSqs()
        client = TestClient(create_app(Settings.from_environ(env), s3, sqs, FakeS3Tables()))
        client.post("/login", json={"password":"password"})
        first = client.post("/api/v3/worker-leases", json={"files": [{"name": "first.parquet", "size_bytes": 1}]}).json()
        replacement = client.put(f"/api/v3/worker-leases/{first['lease_id']}", json={"files": [{"name": "corrected.parquet", "size_bytes": 2}]})
        self.assertEqual(replacement.status_code, 200, replacement.text)
        self.assertTrue(replacement.json()["reused"])
        self.assertEqual(replacement.json()["lease_id"], first["lease_id"])
        self.assertEqual(len(sqs.messages), 1)

    def test_attached_rejected_review_reuses_the_idle_worker_for_a_new_selection(self):
        import json
        env = {"AWS_REGION":"ap-southeast-1", "S3_UPLOADER_V2_LANDING_BUCKET":"landing", "S3_UPLOADER_V2_QUEUE_URL":"legacy", "S3_UPLOADER_V3_BASE_QUEUE_URL":"base", "S3_UPLOADER_V3_LARGE_QUEUE_URL":"large", "S3_UPLOADER_V3_LEASES_ENABLED":"true", "S3_UPLOADER_V2_LOGIN_PASSWORD":"password", "S3_UPLOADER_V2_LOGIN_SECRET":"x" * 32, "S3_UPLOADER_V2_API_BASE_URL":"https://s3-uploader-v2.bot-alex.com", "S3_UPLOADER_V2_GLUE_JOB_NAME":"job", "S3_UPLOADER_V2_ENV":"development", "S3_UPLOADER_V2_COOKIE_SECURE":"false"}
        s3, sqs = FakeS3(), FakeSqs()
        client = TestClient(create_app(Settings.from_environ(env), s3, sqs, FakeS3Tables()))
        client.post("/login", json={"password":"password"})
        first = client.post("/api/v3/worker-leases", json={"files": [{"name": "rejected.xlsx", "size_bytes": 1}]}).json()
        store = S3JobStore(s3, "landing", "s3-uploader-v2")
        store.put_compat_session({"session_id": "rejected-session", "owner_user_id": "shared-operator", "phase": "READY_FOR_REVIEW", "preflight": {"accepted": False}})
        lease = store.get_lease(first["lease_id"])
        lease.update({"session_id": "rejected-session", "state": "AWAITING_KEY"})
        store.put_lease(lease)

        reused = client.put(f"/api/v3/worker-leases/{first['lease_id']}", json={"files": [{"name": "corrected.xlsx", "size_bytes": 2}]})

        self.assertEqual(reused.status_code, 200, reused.text)
        self.assertTrue(reused.json()["reused"])
        self.assertEqual(reused.json()["lease_id"], first["lease_id"])
        record = json.loads(s3.items[f"s3-uploader-v2/worker-leases/{first['lease_id']}/lease.json"])
        self.assertIsNone(record["session_id"])
        self.assertEqual(record["replaced_session_id"], "rejected-session")
        self.assertEqual(record["state"], "AWAITING_UPLOAD")
        self.assertEqual(len(sqs.messages), 1)

    def test_unattached_base_lease_is_replaced_when_new_selection_routes_large(self):
        env = {"AWS_REGION":"ap-southeast-1", "S3_UPLOADER_V2_LANDING_BUCKET":"landing", "S3_UPLOADER_V2_QUEUE_URL":"legacy", "S3_UPLOADER_V3_BASE_QUEUE_URL":"base", "S3_UPLOADER_V3_LARGE_QUEUE_URL":"large", "S3_UPLOADER_V3_LEASES_ENABLED":"true", "S3_UPLOADER_V2_LOGIN_PASSWORD":"password", "S3_UPLOADER_V2_LOGIN_SECRET":"x" * 32, "S3_UPLOADER_V2_API_BASE_URL":"https://s3-uploader-v2.bot-alex.com", "S3_UPLOADER_V2_GLUE_JOB_NAME":"job", "S3_UPLOADER_V2_ENV":"development", "S3_UPLOADER_V2_COOKIE_SECURE":"false"}
        s3, sqs = FakeS3(), FakeSqs()
        client = TestClient(create_app(Settings.from_environ(env), s3, sqs, FakeS3Tables()))
        client.post("/login", json={"password":"password"})
        first = client.post("/api/v3/worker-leases", json={"files": [{"name": "first.parquet", "size_bytes": 1}]}).json()
        replacement = client.put(f"/api/v3/worker-leases/{first['lease_id']}", json={"files": [{"name": "large.parquet", "size_bytes": 129 * 1024 * 1024}]})
        self.assertEqual(replacement.status_code, 200, replacement.text)
        self.assertTrue(replacement.json()["replaced"])
        self.assertEqual(replacement.json()["worker_size"], "LARGE")
        self.assertEqual(len(sqs.messages), 2)
        record = __import__("json").loads(s3.items[f"s3-uploader-v2/worker-leases/{first['lease_id']}/lease.json"])
        self.assertEqual(record["state"], "CANCELLED")

    def test_resource_limited_base_lease_can_be_manually_retried_as_large(self):
        import json
        env = {"AWS_REGION":"ap-southeast-1", "S3_UPLOADER_V2_LANDING_BUCKET":"landing", "S3_UPLOADER_V2_QUEUE_URL":"legacy", "S3_UPLOADER_V3_BASE_QUEUE_URL":"base", "S3_UPLOADER_V3_LARGE_QUEUE_URL":"large", "S3_UPLOADER_V3_LEASES_ENABLED":"true", "S3_UPLOADER_V2_LOGIN_PASSWORD":"password", "S3_UPLOADER_V2_LOGIN_SECRET":"x" * 32, "S3_UPLOADER_V2_API_BASE_URL":"https://s3-uploader-v2.bot-alex.com", "S3_UPLOADER_V2_GLUE_JOB_NAME":"s3-uploader-v2-ingest", "S3_UPLOADER_V2_ENV":"development", "S3_UPLOADER_V2_COOKIE_SECURE":"false"}
        s3, sqs = FakeS3(), FakeSqs()
        client = TestClient(create_app(Settings.from_environ(env), s3, sqs, FakeS3Tables()))
        client.post("/login", json={"password":"password"})
        lease = client.post("/api/v3/worker-leases", json={"files": [{"name": "source.parquet", "size_bytes": 1}]}).json()
        lease_key = f"s3-uploader-v2/worker-leases/{lease['lease_id']}/lease.json"
        record = json.loads(s3.items[lease_key])
        record.update({"session_id": "session", "state": "RESOURCE_LIMIT_EXCEEDED", "resume_phase": "RECEIVED", "can_retry_large": True})
        s3.items[lease_key] = json.dumps(record).encode()
        s3.items["s3-uploader-v2/compat-sessions/session/session.json"] = json.dumps({"session_id": "session", "owner_user_id": "local-admin", "expires_at": "2099-01-01T00:00:00+00:00", "phase": "FAILED", "error": {"code": "RESOURCE_LIMIT_EXCEEDED"}}).encode()
        response = client.post(f"/api/v3/worker-leases/{lease['lease_id']}/retry-large")
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["worker_size"], "LARGE")
        self.assertEqual(sqs.message["QueueUrl"], "large")

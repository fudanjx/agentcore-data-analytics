import hashlib
import io
import os
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

from s3tables_uploader.app.factory import create_app
from s3tables_uploader.config import Settings
from s3tables_uploader.job_store import S3JobStore
from s3tables_uploader.models import JobStatus
from s3tables_uploader.services.auth.bearer import BearerAuthService
from s3tables_uploader.services.secret_manager import InMemorySecretSource


class FakeS3:
    def __init__(self):
        self.items = {}; self.parts = {}; self.deleted_objects = []
        self.object_versions = {}
        self.delete_markers = {}
        self.versioning_status = None
        self.version_counter = 0
    @staticmethod
    def _precondition_error():
        return ClientError({"Error": {"Code": "PreconditionFailed"}}, "S3")
    @staticmethod
    def _not_found_error():
        return ClientError({"Error": {"Code": "NoSuchKey"}}, "S3")
    def create_multipart_upload(self, **kwargs): self.create_args = kwargs; return {"UploadId": "upload"}
    def put_object(self, Bucket, Key, Body, **kwargs):
        if kwargs.get("IfNoneMatch") == "*" and Key in self.items:
            raise self._precondition_error()
        self.items[Key] = Body
        result = {"ETag": "etag"}
        if self.versioning_status == "Enabled":
            self.version_counter += 1
            version_id = f"version-{self.version_counter}"
            version = {
                "VersionId": version_id,
                "Body": Body,
                "Metadata": kwargs.get("Metadata", {}),
                "LastModified": datetime.now(timezone.utc) + timedelta(microseconds=self.version_counter),
            }
            self.object_versions.setdefault(Key, []).append(version)
            result["VersionId"] = version_id
        return result
    def get_object(self, Bucket, Key, VersionId=None):
        import io
        body = self.items.get(Key)
        if VersionId is not None:
            version = next((item for item in self.object_versions.get(Key, []) if item["VersionId"] == VersionId), None)
            body = version["Body"] if version else None
        if body is None:
            raise self._not_found_error()
        return {"Body": io.BytesIO(body), "ETag": "etag", "ContentLength": len(body)}
    def head_object(self, Bucket, Key, VersionId=None):
        versions = self.object_versions.get(Key, [])
        version = next((item for item in versions if item["VersionId"] == VersionId), None) if VersionId else (versions[-1] if versions else None)
        if version:
            return {"ETag": "etag", "Metadata": version["Metadata"]}
        if Key not in self.items:
            raise self._not_found_error()
        return {"ETag": "etag", "Metadata": {}}
    def delete_object(self, Bucket, Key, VersionId=None):
        self.deleted_objects.append({"Bucket": Bucket, "Key": Key, "VersionId": VersionId})
        if VersionId is not None:
            versions = self.object_versions.get(Key, [])
            remaining = [item for item in versions if item["VersionId"] != VersionId]
            if len(remaining) != len(versions):
                self.object_versions[Key] = remaining
                if remaining:
                    self.items[Key] = remaining[-1]["Body"]
                else:
                    self.items.pop(Key, None)
                return {}
            markers = self.delete_markers.get(Key, [])
            if not versions and not markers:
                # Existing tests model an externally-created version by
                # storing only its current object body.
                self.items.pop(Key, None)
                return {}
            remaining_markers = [
                item for item in markers if item["VersionId"] != VersionId
            ]
            if len(remaining_markers) == len(markers):
                raise self._not_found_error()
            self.delete_markers[Key] = remaining_markers
        else:
            self.items.pop(Key, None)
        return {}
    def get_bucket_versioning(self, Bucket): return {"Status": self.versioning_status} if self.versioning_status else {}
    def put_bucket_versioning(self, Bucket, VersioningConfiguration): self.versioning_status = VersioningConfiguration["Status"]; return {}
    def generate_presigned_url(self, *args, **kwargs): return "https://s3.example/part"
    def upload_part(self, Bucket, Key, UploadId, PartNumber, Body):
        self.parts[(Key, PartNumber)] = Body
        return {"ETag": f"part-{PartNumber}"}
    def complete_multipart_upload(self, Bucket, Key, UploadId, MultipartUpload):
        self.items[Key] = b"".join(self.parts[(Key, item["PartNumber"])] for item in MultipartUpload["Parts"])
        return {"VersionId": "version"}
    def abort_multipart_upload(self, **kwargs): return {}
    def get_paginator(self, operation):
        assert operation in {"list_objects_v2", "list_object_versions"}
        client = self
        class Paginator:
            def paginate(self, Bucket, Prefix):
                if operation == "list_objects_v2":
                    yield {"Contents": [{"Key": key} for key in sorted(client.items) if key.startswith(Prefix)]}
                    return
                versions = []
                for key, records in client.object_versions.items():
                    if not key.startswith(Prefix):
                        continue
                    for index, record in enumerate(records):
                        versions.append({
                            "Key": key,
                            "VersionId": record["VersionId"],
                            "LastModified": record["LastModified"],
                            "Size": len(record["Body"]),
                            "IsLatest": index == len(records) - 1,
                        })
                delete_markers = []
                for key, records in client.delete_markers.items():
                    if not key.startswith(Prefix):
                        continue
                    for record in records:
                        delete_markers.append({"Key": key, **record})
                yield {"Versions": versions, "DeleteMarkers": delete_markers}
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
        self.deleted_bucket = None
        self.deleted_namespace = None

    def list_table_buckets(self, **kwargs): return {"tableBuckets": self.buckets}
    def list_tags_for_resource(self, resourceArn):
        # Fake every known bucket as belonging to this app.
        return {"tags": {"APP": "Data-Insights"}}
    def tag_resource(self, resourceArn, tags):
        return {}
    def create_table_bucket(self, name):
        arn = f"arn:aws:s3tables:ap-southeast-1:964340114883:bucket/{name}"
        self.buckets.append({"arn": arn, "name": name, "type": "customer"})
        self.namespaces[arn] = []
        return {"arn": arn}
    def delete_table_bucket(self, tableBucketARN):
        if self.namespaces.get(tableBucketARN):
            raise ClientError({"Error": {"Code": "ConflictException", "Message": "Bucket is not empty"}}, "DeleteTableBucket")
        self.buckets = [item for item in self.buckets if item["arn"] != tableBucketARN]
        self.namespaces.pop(tableBucketARN, None)
        self.deleted_bucket = tableBucketARN
    def list_namespaces(self, tableBucketARN, **kwargs): return {"namespaces": [{"namespace": [name]} for name in self.namespaces[tableBucketARN]]}
    def create_namespace(self, tableBucketARN, namespace):
        self.namespaces[tableBucketARN].append(namespace[0])
        return {"tableBucketARN": tableBucketARN, "namespace": namespace}
    def delete_namespace(self, tableBucketARN, namespace):
        if self.tables.get((tableBucketARN, namespace)):
            raise ClientError({"Error": {"Code": "ConflictException", "Message": "Namespace is not empty"}}, "DeleteNamespace")
        self.namespaces[tableBucketARN].remove(namespace)
        self.deleted_namespace = (tableBucketARN, namespace)
    def list_tables(self, tableBucketARN, namespace, **kwargs): return {"tables": self.tables.get((tableBucketARN, namespace), [])}
    def get_table(self, tableBucketARN, namespace, name): return {"metadataLocation": self.metadata_location}
    def delete_table(self, tableBucketARN, namespace, name):
        self.tables[(tableBucketARN, namespace)] = [item for item in self.tables.get((tableBucketARN, namespace), []) if item["name"] != name]
        self.deleted = (tableBucketARN, namespace, name)


def _test_bearer_service(settings) -> BearerAuthService:
    return BearerAuthService(
        InMemorySecretSource({settings.bearer_secret_arn: "test-token"}),
        settings.bearer_secret_arn,
        cache_ttl_seconds=3600,
        refresh_min_interval_seconds=300,
    )


class ApiTests(unittest.TestCase):
    def setUp(self):
        env = {"AWS_REGION":"ap-southeast-1", "S3_UPLOADER_LANDING_BUCKET":"landing", "S3_UPLOADER_LANDING_PREFIX":"s3-uploader", "S3_UPLOADER_CONTRACT_BUCKET":"ah-data-analytics", "S3_UPLOADER_CONTRACT_PREFIX":"temp_s3_update/web_ingest/table_contracts", "S3_UPLOADER_BASE_QUEUE_URL":"base", "S3_UPLOADER_LARGE_QUEUE_URL":"large", "S3_UPLOADER_MUTATION_QUEUE_URL":"mutation", "S3_UPLOADER_LOGIN_PASSWORD":"password", "S3_UPLOADER_LOGIN_SECRET":"x" * 32, "S3_UPLOADER_API_BASE_URL":"https://s3-uploader-v2.bot-alex.com", "S3_UPLOADER_GLUE_JOB_NAME":"s3-uploader-ingest", "S3_UPLOADER_ENV":"development", "S3_UPLOADER_COOKIE_SECURE":"false", "S3_UPLOADER_BEARER_SECRET_ARN":"arn:aws:secretsmanager::0:secret/test"}
        self.s3 = FakeS3(); self.s3tables = FakeS3Tables(); self.glue = FakeGlue(); self.sqs = FakeSqs()
        settings = Settings.from_environ(env)
        bearer_service = BearerAuthService(
            InMemorySecretSource({settings.bearer_secret_arn: "test-token"}),
            settings.bearer_secret_arn,
            cache_ttl_seconds=3600,
            refresh_min_interval_seconds=300,
        )
        self.client = self.enterContext(TestClient(create_app(settings, self.s3, self.sqs, self.s3tables, self.glue, lifespan_bearer_auth=bearer_service)))

    def test_v2_upload_session_route_is_not_exposed(self):
        self.client.post("/login", json={"password": "password"})
        response = self.client.get("/api/v2/upload-sessions/obsolete")
        self.assertEqual(response.status_code, 404)

    def _append_session(self, session_id, *, contract_columns, payload_columns=None, preflight_columns=None, key_impact=None, files=None):
        import json
        bucket, namespace, table = self.s3tables.bucket_arn, "pilot", "test_table"
        scope = hashlib.sha256(f"{bucket}|{namespace}".encode()).hexdigest()[:16]
        self.s3.items[f"temp_s3_update/web_ingest/table_contracts/{scope}/{table}.json"] = json.dumps({
            "contract_version": 3, "schema": [{"name": "id", "type": "STRING"}],
            "deduplication_columns": contract_columns,
            "deduplication_mode": "keyed" if contract_columns else "unconfigured",
            "deduplication_policy": "skip-existing-key-report-conflict",
        }).encode()
        store = S3JobStore(self.s3, "landing", "s3-uploader")
        source_files = files or [{"name": "source.parquet", "sha256": "a" * 64,
            "source_key": "s3-uploader/uploads/test/raw/source.parquet", "source_version_id": "version", "size_bytes": 1}]
        store.put_lease({
            "lease_id": f"lease-{session_id}", "owner_user_id": "local-admin", "session_id": session_id,
            "state": "AWAITING_CONFIRMATION", "worker_size": "BASE",
            "expires_at": "2100-01-01T00:00:00+00:00", "cancellation_locked_at": None,
        })
        store.put_compat_session({
            "session_id": session_id, "owner_user_id": "local-admin", "expires_at": "2100-01-01T00:00:00+00:00",
            "mode": "append", "table_bucket_arn": bucket, "namespace": namespace, "table": table,
            "phase": "READY_FOR_REVIEW", "worker_lease_id": f"lease-{session_id}", "files": source_files,
            "preflight": {"accepted": True, "deduplication_candidates": payload_columns or [], "deduplication_columns": preflight_columns or [], "sanitization_review": {"manual_encryption_candidates": []}},
            "key_impact": key_impact,
        })
        return store, bucket, namespace, table, scope

    def test_v1_history_contract_reads_canonical_audit_projection_and_queues_rollback(self):
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
        history = self.client.get("/api/v3/upload-history", params={"table_bucket_arn": bucket, "namespace": namespace, "table": table})
        self.assertEqual(history.status_code, 200, history.text)
        self.assertEqual(history.json()["latest_rollback_upload_id"], "UPLOAD-ABCDEF123456")
        rollback = self.client.post("/api/v3/rollbacks", json={"table_bucket_arn": bucket, "namespace": namespace, "table": table, "upload_id": "UPLOAD-ABCDEF123456", "confirm": True})
        self.assertEqual(rollback.status_code, 202, rollback.text)
        mutation_id = rollback.json()["mutation_id"]
        self.assertEqual(rollback.json()["phase"], "READY_FOR_MUTATION")
        self.assertFalse(self.glue.started)
        command = S3JobStore(self.s3, "landing", "s3-uploader").get_mutation_command(mutation_id)
        self.assertEqual(command.operation, "rollback")
        self.assertEqual(command.rollback_snapshot_id, "123")
        self.assertEqual(command.destination.table, table)
        self.assertEqual(self.sqs.messages[-1]["MessageBody"], mutation_id)
        repeated = self.client.post("/api/v3/rollbacks", json={"table_bucket_arn": bucket, "namespace": namespace, "table": table, "upload_id": "UPLOAD-ABCDEF123456", "confirm": True})
        self.assertEqual(repeated.status_code, 202, repeated.text)
        self.assertEqual(repeated.json()["mutation_id"], mutation_id)

    def test_administrator_can_create_and_force_delete_empty_bucket_and_namespace(self):
        self.client.post("/login", json={"password":"password"})
        bucket = self.client.post("/api/v3/buckets", json={"name": "new-analytics"})
        self.assertEqual(bucket.status_code, 201, bucket.text)
        deleted_bucket = self.client.request(
            "DELETE", "/api/v3/buckets",
            json={"table_bucket_arn": bucket.json()["table_bucket_arn"], "force": True},
        )
        self.assertEqual(deleted_bucket.status_code, 200, deleted_bucket.text)
        self.assertEqual(self.s3tables.deleted_bucket, bucket.json()["table_bucket_arn"])
        namespace = self.client.post("/api/v3/buckets/namespaces", json={"table_bucket_arn": self.s3tables.bucket_arn, "namespace": "reporting"})
        self.assertEqual(namespace.status_code, 201, namespace.text)
        deleted_namespace = self.client.request(
            "DELETE", "/api/v3/buckets/namespaces",
            json={"table_bucket_arn": self.s3tables.bucket_arn, "namespace": "reporting", "confirm": True},
        )
        self.assertEqual(deleted_namespace.status_code, 200, deleted_namespace.text)
        self.assertEqual(
            self.s3tables.deleted_namespace,
            (self.s3tables.bucket_arn, "reporting"),
        )

    def test_force_bucket_deletion_cascades_but_preserves_audit_and_skill_prefix(self):
        self.client.post("/login", json={"password":"password"})
        namespace = self.client.request(
            "DELETE", "/api/v3/buckets/namespaces",
            json={"table_bucket_arn": self.s3tables.bucket_arn, "namespace": "pilot", "confirm": True},
        )
        self.assertEqual(namespace.status_code, 409, namespace.text)
        scope = hashlib.sha256(f"{self.s3tables.bucket_arn}|pilot".encode()).hexdigest()[:16]
        contract_key = f"temp_s3_update/web_ingest/table_contracts/{scope}/test_table.json"
        audit_key = f"temp_s3_update/web_ingest/upload_history/{scope}/test_table/upload.json"
        skill_key = "skills/ah-soc-delta-pilot/SKILL.md"
        self.s3.versioning_status = "Enabled"
        self.s3.put_object(Bucket="ah-data-analytics", Key=contract_key, Body=b"first")
        self.s3.put_object(Bucket="ah-data-analytics", Key=contract_key, Body=b"second")
        self.s3.versioning_status = None
        self.s3.items[audit_key] = b"{}"
        self.s3.items[skill_key] = b"---\ndescription: retained\n---\n"
        bucket = self.client.request(
            "DELETE", "/api/v3/buckets",
            json={"table_bucket_arn": self.s3tables.bucket_arn, "force": True},
        )
        self.assertEqual(bucket.status_code, 200, bucket.text)
        self.assertEqual(bucket.json()["deleted_tables"], 1)
        self.assertEqual(bucket.json()["deleted_namespaces"], 1)
        self.assertEqual(bucket.json()["deleted_contracts"], 2)
        self.assertFalse(bucket.json()["skill_prefix_deleted"])
        self.assertNotIn(contract_key, self.s3.items)
        self.assertEqual(self.s3.object_versions[contract_key], [])
        self.assertIn(audit_key, self.s3.items)
        self.assertIn(skill_key, self.s3.items)
        self.assertEqual(self.s3tables.deleted_bucket, self.s3tables.bucket_arn)

    def test_force_bucket_deletion_optionally_purges_all_skill_versions(self):
        self.client.post("/login", json={"password":"password"})
        self.s3.versioning_status = "Enabled"
        skill_key = "skills/ah-soc-delta-pilot/ah-soc-delta-pilot.zip"
        self.s3.put_object(Bucket="agentcore-harness-dev", Key=skill_key, Body=b"first")
        self.s3.put_object(Bucket="agentcore-harness-dev", Key=skill_key, Body=b"second")
        self.s3.delete_markers[skill_key] = [{
            "VersionId": "delete-marker-1",
            "LastModified": datetime.now(timezone.utc),
        }]
        bucket = self.client.request(
            "DELETE", "/api/v3/buckets",
            json={
                "table_bucket_arn": self.s3tables.bucket_arn,
                "force": True,
                "delete_skill_prefix": True,
            },
        )
        self.assertEqual(bucket.status_code, 200, bucket.text)
        self.assertTrue(bucket.json()["skill_prefix_deleted"])
        self.assertEqual(bucket.json()["deleted_skill_versions"], 3)
        self.assertNotIn(skill_key, self.s3.items)
        self.assertEqual(self.s3.object_versions[skill_key], [])
        self.assertEqual(self.s3.delete_markers[skill_key], [])

    def test_force_bucket_deletion_stops_before_changes_when_a_table_is_locked(self):
        import json

        self.client.post("/login", json={"password":"password"})
        target = "\x1f".join(
            (self.s3tables.bucket_arn, "pilot", "test_table")
        ).encode()
        lock_key = f"s3-uploader/table-locks/{hashlib.sha256(target).hexdigest()}.json"
        self.s3.items[lock_key] = json.dumps({
            "owner_token": "active-job",
            "lease_expires_at": "2100-01-01T00:00:00+00:00",
        }).encode()
        bucket = self.client.request(
            "DELETE", "/api/v3/buckets",
            json={"table_bucket_arn": self.s3tables.bucket_arn, "force": True},
        )
        self.assertEqual(bucket.status_code, 409, bucket.text)
        self.assertIn("TABLE_MUTATION_IN_PROGRESS", bucket.json()["detail"])
        self.assertIsNone(self.s3tables.deleted_bucket)
        self.assertEqual(
            self.s3tables.tables[(self.s3tables.bucket_arn, "pilot")],
            [{"name": "test_table"}],
        )

    def test_bucket_force_and_namespace_confirmation_are_required(self):
        self.client.post("/login", json={"password":"password"})
        namespace = self.client.request(
            "DELETE", "/api/v3/buckets/namespaces",
            json={"table_bucket_arn": self.s3tables.bucket_arn, "namespace": "pilot"},
        )
        self.assertEqual(namespace.status_code, 422, namespace.text)
        bucket = self.client.request(
            "DELETE", "/api/v3/buckets",
            json={"table_bucket_arn": self.s3tables.bucket_arn},
        )
        self.assertEqual(bucket.status_code, 422, bucket.text)

    def test_non_admin_cannot_delete_bucket_or_namespace(self):
        self.client.post("/login", json={"password":"password"})
        headers = {"X-Pilot-User-Id": "local-editor"}
        namespace = self.client.request(
            "DELETE", "/api/v3/buckets/namespaces", headers=headers,
            json={"table_bucket_arn": self.s3tables.bucket_arn, "namespace": "pilot", "confirm": True},
        )
        self.assertEqual(namespace.status_code, 403, namespace.text)
        bucket = self.client.request(
            "DELETE", "/api/v3/buckets", headers=headers,
            json={"table_bucket_arn": self.s3tables.bucket_arn, "force": True},
        )
        self.assertEqual(bucket.status_code, 403, bucket.text)

    def test_administrator_can_delete_uploader_managed_table(self):
        self.client.post("/login", json={"password":"password"})
        scope = hashlib.sha256(f"{self.s3tables.bucket_arn}|pilot".encode()).hexdigest()[:16]
        self.s3.items[f"temp_s3_update/web_ingest/table_contracts/{scope}/test_table.json"] = b"{}"
        deleted = self.client.request("DELETE", "/api/v3/buckets/tables", json={"table_bucket_arn": self.s3tables.bucket_arn, "namespace": "pilot", "table": "test_table"})
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertEqual(deleted.json()["deleted"], "test_table")
        self.assertEqual(self.s3tables.deleted, (self.s3tables.bucket_arn, "pilot", "test_table"))

    def test_v1_local_identity_emulation_exposes_admin_editor_and_unassigned_profiles(self):
        self.client.post("/login", json={"password":"password"})
        profiles = self.client.get("/api/v3/dev/identity-profiles")
        self.assertEqual(profiles.status_code, 200, profiles.text)
        self.assertEqual([item["user_id"] for item in profiles.json()["profiles"]], ["local-admin", "local-editor", "local-unassigned"])

        editor = self.client.get("/api/v3/identity", headers={"X-Pilot-User-Id": "local-editor"})
        self.assertEqual(editor.status_code, 200, editor.text)
        self.assertFalse(editor.json()["is_admin"])
        self.assertEqual(editor.json()["buckets"][0]["table_bucket_arn"], "arn:aws:s3tables:ap-southeast-1:964340114883:bucket/ah-soc-delta-pilot")
        denied = self.client.get("/api/v3/buckets", headers={"X-Pilot-User-Id": "local-unassigned"})
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
        response = self.client.get("/api/v3/buckets/tables", params={"table_bucket_arn": self.s3tables.bucket_arn, "namespace": "pilot"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["tables"][0]["row_count"], 42)
        self.assertEqual(response.json()["tables"][0]["deduplication_columns"], ["id"])

    def test_append_derives_available_columns_from_the_locked_table_key(self):
        self.client.post("/login", json={"password":"password"})
        store, bucket, namespace, table, _ = self._append_session(
            "configured", contract_columns=["a", "b", "c", "d", "f"], preflight_columns=["c", "d", "f"],
        )
        response = self.client.post(f"/api/v3/upload-sessions/configured/ingestions", json={
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
        response = self.client.post(f"/api/v3/upload-sessions/no-intersection/ingestions", json={
            "request_id": "no-intersection", "deduplication_mode": "keyed", "deduplication_columns": ["other"],
        })
        self.assertEqual(response.status_code, 202, response.text)
        request = store.get_request(response.json()["job_id"])
        self.assertEqual(request.deduplication_mode, "none")
        self.assertEqual(request.deduplication_columns, [])

    def test_matching_multi_file_session_creates_one_immutable_job_manifest(self):
        self.client.post("/login", json={"password":"password"})
        files = [
            {"name": "first.parquet", "sha256": "a" * 64, "source_key": "s3-uploader/uploads/test/raw/first.parquet", "source_version_id": "version-1", "size_bytes": 1},
            {"name": "second.parquet", "sha256": "b" * 64, "source_key": "s3-uploader/uploads/test/raw/second.parquet", "source_version_id": "version-2", "size_bytes": 2},
        ]
        store, _, _, _, _ = self._append_session("multiple", contract_columns=[], payload_columns=[{"column": "id"}], files=files)
        response = self.client.post("/api/v3/upload-sessions/multiple/ingestions", json={
            "request_id": "multi-file", "deduplication_mode": "none", "deduplication_columns": [],
        })
        self.assertEqual(response.status_code, 202, response.text)
        request = store.get_request(response.json()["job_id"])
        self.assertEqual([source.name for source in request.source_files], ["first.parquet", "second.parquet"])
        self.assertEqual([source.source_version_id for source in request.source_files], ["version-1", "version-2"])

    def test_failed_worker_status_replaces_a_stale_queued_session(self):
        self.client.post("/login", json={"password":"password"})
        store, _, _, _, _ = self._append_session("failed-worker", contract_columns=["id"], preflight_columns=["id"])
        started = self.client.post(f"/api/v3/upload-sessions/failed-worker/ingestions", json={
            "request_id": "failed-worker", "deduplication_mode": "keyed", "deduplication_columns": [],
        })
        self.assertEqual(started.status_code, 202, started.text)
        job_id = started.json()["job_id"]
        store.put_status(JobStatus(job_id=job_id, phase="FAILED", message="The selected key columns are not present in the upload: acct_n", error_code="WorkerError"))
        response = self.client.get("/api/v3/upload-sessions/failed-worker")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["phase"], "FAILED")
        self.assertEqual(response.json()["error"]["code"], "WorkerError")

    def test_keyless_append_saves_its_first_user_selected_key(self):
        import json
        self.client.post("/login", json={"password":"password"})
        candidates = [{"column": "id", "deduplication_eligible": True}]
        impact = {"token": "ack", "deduplication_columns": ["id"], "expires_at": "2100-01-01T00:00:00+00:00"}
        _, bucket, namespace, table, scope = self._append_session("activate", contract_columns=[], payload_columns=candidates, key_impact=impact)
        response = self.client.post(f"/api/v3/upload-sessions/activate/ingestions", json={
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
        response = self.client.get("/api/v3/buckets/namespaces", params={"table_bucket_arn": bucket})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["namespaces"], ["future"])

    def test_skill_file_upload_download_and_confirmed_delete_use_v1_routes(self):
        self.client.post("/login", json={"password": "password"})
        bucket = self.s3tables.bucket_arn
        skill = b"---\ndescription: test skill\n---\n# Test\n"
        uploaded = self.client.post(
            "/api/v3/skills/files",
            data={"table_bucket_arn": bucket, "paths_json": '["SKILL.md"]'},
            files={"files": ("SKILL.md", skill, "text/markdown")},
        )
        self.assertEqual(uploaded.status_code, 200, uploaded.text)
        self.assertEqual(uploaded.json()["uploaded_paths"], ["SKILL.md"])
        key = "skills/ah-soc-delta-pilot/SKILL.md"
        self.assertIn(key, self.s3.items)
        self.assertIn(b"name: ah-soc-delta-pilot", self.s3.items[key])

        downloaded = self.client.get("/api/v3/skills/files/download", params={"table_bucket_arn": bucket, "path": "SKILL.md"})
        self.assertEqual(downloaded.status_code, 200, downloaded.text)
        self.assertEqual(downloaded.content, self.s3.items[key])
        self.assertIn("attachment", downloaded.headers["content-disposition"])

        rejected = self.client.request("DELETE", "/api/v3/skills/files", json={"table_bucket_arn": bucket, "path": "SKILL.md", "confirm": False})
        self.assertEqual(rejected.status_code, 422, rejected.text)
        deleted = self.client.request("DELETE", "/api/v3/skills/files", json={"table_bucket_arn": bucket, "path": "SKILL.md", "confirm": True})
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertEqual(deleted.json()["deleted_path"], "SKILL.md")
        self.assertNotIn(key, self.s3.items)

    def test_skill_zip_versions_use_native_s3_versions_and_descriptions(self):
        bucket = self.s3tables.bucket_arn
        skill = b"---\ndescription: test skill\n---\n# Test\n"
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as zipped:
            zipped.writestr("skill-folder/SKILL.md", skill)
            zipped.writestr("skill-folder/references/data.md", b"facts")
        content = archive.getvalue()

        self.assertEqual(self.client.get("/api/v3/skills/versions", params={"table_bucket_arn": bucket}).status_code, 401)
        self.client.post("/login", json={"password": "password"})
        rejected = self.client.post(
            "/api/v3/skills/versions", data={"table_bucket_arn": bucket, "uploaded_by": "tester@example.com"},
            files={"file": ("wrong.txt", content, "text/plain")},
        )
        self.assertEqual(rejected.status_code, 422, rejected.text)
        corrupt = self.client.post(
            "/api/v3/skills/versions", data={"table_bucket_arn": bucket, "uploaded_by": "tester@example.com"},
            files={"file": ("broken.zip", b"not a zip", "application/zip")},
        )
        self.assertEqual(corrupt.status_code, 422, corrupt.text)
        unsafe_archive = io.BytesIO()
        with zipfile.ZipFile(unsafe_archive, "w") as zipped:
            zipped.writestr("skill-folder/SKILL.md", skill)
            zipped.writestr("../outside.md", b"unsafe")
        unsafe_upload = self.client.post(
            "/api/v3/skills/versions", data={"table_bucket_arn": bucket, "uploaded_by": "tester@example.com"},
            files={"file": ("unsafe.zip", unsafe_archive.getvalue(), "application/zip")},
        )
        self.assertEqual(unsafe_upload.status_code, 422, unsafe_upload.text)
        unsafe_directory = io.BytesIO()
        with zipfile.ZipFile(unsafe_directory, "w") as zipped:
            zipped.writestr("skill-folder/SKILL.md", skill)
            zipped.writestr("../outside/", b"")
        directory_upload = self.client.post(
            "/api/v3/skills/versions", data={"table_bucket_arn": bucket, "uploaded_by": "tester@example.com"},
            files={"file": ("unsafe-dir.zip", unsafe_directory.getvalue(), "application/zip")},
        )
        self.assertEqual(directory_upload.status_code, 422, directory_upload.text)
        descriptions = ["Initial upload", "Added reference data – 中文"]
        uploaders = ["first@example.com", "second@example.com"]
        for description, uploaded_by in zip(descriptions, uploaders, strict=True):
            uploaded = self.client.post(
                "/api/v3/skills/versions", data={
                    "table_bucket_arn": bucket,
                    "description": description,
                    "uploaded_by": uploaded_by,
                },
                files={"file": ("wrapped-skill.zip", content, "application/zip")},
            )
            self.assertEqual(uploaded.status_code, 201, uploaded.text)
            self.assertEqual(uploaded.json()["filename"], "ah-soc-delta-pilot.zip")
            self.assertEqual(uploaded.json()["description"], description)
            self.assertEqual(uploaded.json()["uploaded_by"], uploaded_by)
            self.assertTrue(uploaded.json()["version_id"])
        self.assertEqual(self.s3.versioning_status, "Enabled")
        self.assertEqual(list(self.s3.object_versions), ["skills/ah-soc-delta-pilot/ah-soc-delta-pilot.zip"])
        versions = self.client.get("/api/v3/skills/versions", params={"table_bucket_arn": bucket})
        self.assertEqual(versions.status_code, 200, versions.text)
        listed = versions.json()["versions"]
        self.assertEqual(len(listed), 2)
        self.assertTrue(all("filename" not in item for item in listed))
        self.assertEqual([item["description"] for item in listed], list(reversed(descriptions)))
        self.assertEqual([item["uploaded_by"] for item in listed], list(reversed(uploaders)))
        self.assertNotEqual(listed[0]["version_id"], listed[1]["version_id"])
        self.assertTrue(all(item["uploaded_at"] for item in listed))
        downloaded = self.client.get("/api/v3/skills/versions/download", params={"table_bucket_arn": bucket, "version_id": listed[0]["version_id"]})
        self.assertEqual(downloaded.status_code, 200, downloaded.text)
        self.assertEqual(downloaded.content, content)
        deletion = self.client.request("DELETE", "/api/v3/skills/versions", json={"table_bucket_arn": bucket, "version_id": listed[0]["version_id"]})
        self.assertEqual(deletion.status_code, 405, deletion.text)

    def test_skill_zip_version_list_reads_description_from_object_metadata(self):
        bucket = self.s3tables.bucket_arn
        self.s3.put_bucket_versioning(Bucket="agentcore-harness-dev", VersioningConfiguration={"Status": "Enabled"})
        self.s3.put_object(
            Bucket="agentcore-harness-dev",
            Key="skills/ah-soc-delta-pilot/ah-soc-delta-pilot.zip",
            Body=b"existing snapshot",
            Metadata={"description": "Existing%20version", "uploaded_by": "owner%2Bskill%40example.com"},
        )
        self.client.post("/login", json={"password": "password"})
        response = self.client.get("/api/v3/skills/versions", params={"table_bucket_arn": bucket})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(response.json()["versions"]), 1)
        self.assertNotIn("filename", response.json()["versions"][0])
        self.assertEqual(response.json()["versions"][0]["description"], "Existing version")
        self.assertEqual(response.json()["versions"][0]["uploaded_by"], "owner+skill@example.com")

    def test_skill_zip_version_requires_valid_uploader_email(self):
        bucket = self.s3tables.bucket_arn
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as zipped:
            zipped.writestr("SKILL.md", b"---\ndescription: test skill\n---\n# Test\n")
        self.client.post("/login", json={"password": "password"})

        missing = self.client.post(
            "/api/v3/skills/versions",
            data={"table_bucket_arn": bucket},
            files={"file": ("skill.zip", archive.getvalue(), "application/zip")},
        )
        self.assertEqual(missing.status_code, 422, missing.text)

        invalid = self.client.post(
            "/api/v3/skills/versions",
            data={"table_bucket_arn": bucket, "uploaded_by": "not-an-email"},
            files={"file": ("skill.zip", archive.getvalue(), "application/zip")},
        )
        self.assertEqual(invalid.status_code, 422, invalid.text)
        self.assertIn("valid email address", invalid.json()["detail"])

    def test_skill_zip_version_list_returns_only_latest_ten(self):
        bucket = self.s3tables.bucket_arn
        key = "skills/ah-soc-delta-pilot/ah-soc-delta-pilot.zip"
        self.s3.put_bucket_versioning(Bucket="agentcore-harness-dev", VersioningConfiguration={"Status": "Enabled"})
        for index in range(12):
            self.s3.put_object(
                Bucket="agentcore-harness-dev",
                Key=key,
                Body=f"version-{index}".encode(),
                Metadata={"description": f"Description%20{index}"},
            )
        self.client.post("/login", json={"password": "password"})
        response = self.client.get("/api/v3/skills/versions", params={"table_bucket_arn": bucket})
        self.assertEqual(response.status_code, 200, response.text)
        listed = response.json()["versions"]
        self.assertEqual(len(listed), 10)
        self.assertEqual(listed[0]["description"], "Description 11")
        self.assertEqual(listed[-1]["description"], "Description 2")
        self.assertTrue(listed[0]["is_latest"])

    def test_skill_bundle_destination_requires_explicit_configuration(self):
        from s3tables_uploader import skill_bundle

        # Callers must pass destination_bucket / destination_prefix explicitly;
        # the module no longer reads env vars as a fallback.
        destination = skill_bundle._destination(
            "ah-soc-delta-pilot",
            destination_bucket="configured-skill-bucket",
            destination_prefix="configured/skills",
        )
        self.assertEqual(destination, (
            "configured-skill-bucket",
            "configured/skills/ah-soc-delta-pilot",
            "s3://configured-skill-bucket/configured/skills/ah-soc-delta-pilot/",
        ))

    def test_login_protects_session_creation_and_keeps_upload_off_api(self):
        self.assertEqual(self.client.post("/api/v3/upload-sessions", json={}).status_code, 401)
        self.assertEqual(self.client.post("/login", json={"password":"password"}).status_code, 200)
        response = self.client.post("/api/v3/upload-sessions", json={"file_name":"source.parquet", "content_type":"application/octet-stream", "source_sha256":"a" * 64})
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
            "/api/v3/upload-sessions",
            data={"mode":"create", "table_bucket_arn":"arn:aws:s3tables:ap-southeast-1:964340114883:bucket/ah-soc-delta-pilot", "namespace":"pilot", "table":"test_table"},
            files={"files": ("source.parquet", buffer.getvalue(), "application/octet-stream")},
        )
        self.assertEqual(response.status_code, 201, response.text)
        session = response.json()
        self.assertEqual(session["phase"], "RECEIVED")
        self.assertEqual(session["files"][0]["name"], "source.parquet")
        self.assertIsNone(session["preflight"])
        restored = self.client.get(f"/api/v3/upload-sessions/{session['session_id']}")
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertEqual(restored.json()["session_id"], session["session_id"])

    def test_selected_files_create_and_attach_a_deterministic_worker_lease(self):
        import io
        import pyarrow as pa
        import pyarrow.parquet as pq

        env = {"AWS_REGION":"ap-southeast-1", "S3_UPLOADER_LANDING_BUCKET":"landing", "S3_UPLOADER_LANDING_PREFIX":"s3-uploader", "S3_UPLOADER_CONTRACT_BUCKET":"ah-data-analytics", "S3_UPLOADER_CONTRACT_PREFIX":"temp_s3_update/web_ingest/table_contracts", "S3_UPLOADER_BASE_QUEUE_URL":"base", "S3_UPLOADER_LARGE_QUEUE_URL":"large", "S3_UPLOADER_MUTATION_QUEUE_URL":"mutation", "S3_UPLOADER_LOGIN_PASSWORD":"password", "S3_UPLOADER_LOGIN_SECRET":"x" * 32, "S3_UPLOADER_API_BASE_URL":"https://s3-uploader-v2.bot-alex.com", "S3_UPLOADER_GLUE_JOB_NAME":"s3-uploader-ingest", "S3_UPLOADER_ENV":"development", "S3_UPLOADER_COOKIE_SECURE":"false", "S3_UPLOADER_BEARER_SECRET_ARN":"arn:aws:secretsmanager::0:secret/test"}
        s3, sqs = FakeS3(), FakeSqs()
        settings = Settings.from_environ(env)
        client = self.enterContext(TestClient(create_app(settings, s3, sqs, FakeS3Tables(), lifespan_bearer_auth=_test_bearer_service(settings))))
        client.post("/login", json={"password":"password"})
        buffer = io.BytesIO(); pq.write_table(pa.table({"id": ["1"]}), buffer)
        payload = buffer.getvalue()
        lease = client.post("/api/v3/worker-leases", json={"files": [{"name": "source.parquet", "size_bytes": len(payload)}]})
        self.assertEqual(lease.status_code, 201, lease.text)
        self.assertEqual(lease.json()["worker_size"], "BASE")
        self.assertEqual(sqs.message["QueueUrl"], "base")
        session = client.post(
            "/api/v3/upload-sessions",
            data={"mode":"create", "table_bucket_arn":"arn:aws:s3tables:ap-southeast-1:964340114883:bucket/ah-soc-delta-pilot", "namespace":"pilot", "table":"test_table", "worker_lease_id": lease.json()["lease_id"]},
            files={"files": ("source.parquet", payload, "application/octet-stream")},
        )
        self.assertEqual(session.status_code, 201, session.text)
        self.assertEqual(session.json()["worker_lease"]["lease_id"], lease.json()["lease_id"])

    def test_stale_lease_from_another_emulated_user_is_replaced(self):
        import io
        import pyarrow as pa
        import pyarrow.parquet as pq

        env = {"AWS_REGION":"ap-southeast-1", "S3_UPLOADER_LANDING_BUCKET":"landing", "S3_UPLOADER_LANDING_PREFIX":"s3-uploader", "S3_UPLOADER_CONTRACT_BUCKET":"ah-data-analytics", "S3_UPLOADER_CONTRACT_PREFIX":"temp_s3_update/web_ingest/table_contracts", "S3_UPLOADER_BASE_QUEUE_URL":"base", "S3_UPLOADER_LARGE_QUEUE_URL":"large", "S3_UPLOADER_MUTATION_QUEUE_URL":"mutation", "S3_UPLOADER_LOGIN_PASSWORD":"password", "S3_UPLOADER_LOGIN_SECRET":"x" * 32, "S3_UPLOADER_API_BASE_URL":"https://s3-uploader-v2.bot-alex.com", "S3_UPLOADER_GLUE_JOB_NAME":"job", "S3_UPLOADER_ENV":"development", "S3_UPLOADER_COOKIE_SECURE":"false", "S3_UPLOADER_BEARER_SECRET_ARN":"arn:aws:secretsmanager::0:secret/test"}
        s3, sqs = FakeS3(), FakeSqs()
        settings = Settings.from_environ(env)
        client = self.enterContext(TestClient(create_app(settings, s3, sqs, FakeS3Tables(), lifespan_bearer_auth=_test_bearer_service(settings))))
        client.post("/login", json={"password":"password"})
        buffer = io.BytesIO(); pq.write_table(pa.table({"id": ["1"]}), buffer)
        payload = buffer.getvalue()
        editor_headers = {"X-Pilot-User-Id": "local-editor"}
        stale = client.post("/api/v3/worker-leases", headers=editor_headers, json={"files": [{"name": "source.parquet", "size_bytes": len(payload)}]}).json()
        response = client.post(
            "/api/v3/upload-sessions", headers={"X-Pilot-User-Id": "local-admin"},
            data={"mode":"create", "table_bucket_arn":"arn:aws:s3tables:ap-southeast-1:964340114883:bucket/ah-soc-delta-pilot", "namespace":"pilot", "table":"admin_table", "worker_lease_id": stale["lease_id"]},
            files={"files": ("source.parquet", payload, "application/octet-stream")},
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertNotEqual(response.json()["worker_lease"]["lease_id"], stale["lease_id"])
        store = S3JobStore(s3, "landing", "s3-uploader")
        self.assertEqual(store.get_lease(response.json()["worker_lease"]["lease_id"])["owner_user_id"], "local-admin")

    def test_unattached_same_size_lease_is_reused_when_file_selection_changes(self):
        env = {"AWS_REGION":"ap-southeast-1", "S3_UPLOADER_LANDING_BUCKET":"landing", "S3_UPLOADER_LANDING_PREFIX":"s3-uploader", "S3_UPLOADER_CONTRACT_BUCKET":"ah-data-analytics", "S3_UPLOADER_CONTRACT_PREFIX":"temp_s3_update/web_ingest/table_contracts", "S3_UPLOADER_BASE_QUEUE_URL":"base", "S3_UPLOADER_LARGE_QUEUE_URL":"large", "S3_UPLOADER_MUTATION_QUEUE_URL":"mutation", "S3_UPLOADER_LOGIN_PASSWORD":"password", "S3_UPLOADER_LOGIN_SECRET":"x" * 32, "S3_UPLOADER_API_BASE_URL":"https://s3-uploader-v2.bot-alex.com", "S3_UPLOADER_GLUE_JOB_NAME":"job", "S3_UPLOADER_ENV":"development", "S3_UPLOADER_COOKIE_SECURE":"false", "S3_UPLOADER_BEARER_SECRET_ARN":"arn:aws:secretsmanager::0:secret/test"}
        s3, sqs = FakeS3(), FakeSqs()
        settings = Settings.from_environ(env)
        client = self.enterContext(TestClient(create_app(settings, s3, sqs, FakeS3Tables(), lifespan_bearer_auth=_test_bearer_service(settings))))
        client.post("/login", json={"password":"password"})
        first = client.post("/api/v3/worker-leases", json={"files": [{"name": "first.parquet", "size_bytes": 1}]}).json()
        replacement = client.put(f"/api/v3/worker-leases/{first['lease_id']}", json={"files": [{"name": "corrected.parquet", "size_bytes": 2}]})
        self.assertEqual(replacement.status_code, 200, replacement.text)
        self.assertTrue(replacement.json()["reused"])
        self.assertEqual(replacement.json()["lease_id"], first["lease_id"])
        self.assertEqual(len(sqs.messages), 1)

    def test_attached_rejected_review_reuses_the_idle_worker_for_a_new_selection(self):
        import json
        env = {"AWS_REGION":"ap-southeast-1", "S3_UPLOADER_LANDING_BUCKET":"landing", "S3_UPLOADER_LANDING_PREFIX":"s3-uploader", "S3_UPLOADER_CONTRACT_BUCKET":"ah-data-analytics", "S3_UPLOADER_CONTRACT_PREFIX":"temp_s3_update/web_ingest/table_contracts", "S3_UPLOADER_BASE_QUEUE_URL":"base", "S3_UPLOADER_LARGE_QUEUE_URL":"large", "S3_UPLOADER_MUTATION_QUEUE_URL":"mutation", "S3_UPLOADER_LOGIN_PASSWORD":"password", "S3_UPLOADER_LOGIN_SECRET":"x" * 32, "S3_UPLOADER_API_BASE_URL":"https://s3-uploader-v2.bot-alex.com", "S3_UPLOADER_GLUE_JOB_NAME":"job", "S3_UPLOADER_ENV":"development", "S3_UPLOADER_COOKIE_SECURE":"false", "S3_UPLOADER_BEARER_SECRET_ARN":"arn:aws:secretsmanager::0:secret/test"}
        s3, sqs = FakeS3(), FakeSqs()
        settings = Settings.from_environ(env)
        client = self.enterContext(TestClient(create_app(settings, s3, sqs, FakeS3Tables(), lifespan_bearer_auth=_test_bearer_service(settings))))
        client.post("/login", json={"password":"password"})
        first = client.post("/api/v3/worker-leases", json={"files": [{"name": "rejected.xlsx", "size_bytes": 1}]}).json()
        store = S3JobStore(s3, "landing", "s3-uploader")
        store.put_compat_session({"session_id": "rejected-session", "owner_user_id": "shared-operator", "phase": "READY_FOR_REVIEW", "preflight": {"accepted": False}})
        lease = store.get_lease(first["lease_id"])
        lease.update({"session_id": "rejected-session", "state": "AWAITING_KEY"})
        store.put_lease(lease)

        reused = client.put(f"/api/v3/worker-leases/{first['lease_id']}", json={"files": [{"name": "corrected.xlsx", "size_bytes": 2}]})

        self.assertEqual(reused.status_code, 200, reused.text)
        self.assertTrue(reused.json()["reused"])
        self.assertEqual(reused.json()["lease_id"], first["lease_id"])
        record = json.loads(s3.items[f"s3-uploader/worker-leases/{first['lease_id']}/lease.json"])
        self.assertIsNone(record["session_id"])
        self.assertEqual(record["replaced_session_id"], "rejected-session")
        self.assertEqual(record["state"], "AWAITING_UPLOAD")
        self.assertEqual(len(sqs.messages), 1)

    def test_unattached_base_lease_is_replaced_when_new_selection_routes_large(self):
        env = {"AWS_REGION":"ap-southeast-1", "S3_UPLOADER_LANDING_BUCKET":"landing", "S3_UPLOADER_LANDING_PREFIX":"s3-uploader", "S3_UPLOADER_CONTRACT_BUCKET":"ah-data-analytics", "S3_UPLOADER_CONTRACT_PREFIX":"temp_s3_update/web_ingest/table_contracts", "S3_UPLOADER_BASE_QUEUE_URL":"base", "S3_UPLOADER_LARGE_QUEUE_URL":"large", "S3_UPLOADER_MUTATION_QUEUE_URL":"mutation", "S3_UPLOADER_LOGIN_PASSWORD":"password", "S3_UPLOADER_LOGIN_SECRET":"x" * 32, "S3_UPLOADER_API_BASE_URL":"https://s3-uploader-v2.bot-alex.com", "S3_UPLOADER_GLUE_JOB_NAME":"job", "S3_UPLOADER_ENV":"development", "S3_UPLOADER_COOKIE_SECURE":"false", "S3_UPLOADER_BEARER_SECRET_ARN":"arn:aws:secretsmanager::0:secret/test"}
        s3, sqs = FakeS3(), FakeSqs()
        settings = Settings.from_environ(env)
        client = self.enterContext(TestClient(create_app(settings, s3, sqs, FakeS3Tables(), lifespan_bearer_auth=_test_bearer_service(settings))))
        client.post("/login", json={"password":"password"})
        first = client.post("/api/v3/worker-leases", json={"files": [{"name": "first.parquet", "size_bytes": 1}]}).json()
        replacement = client.put(f"/api/v3/worker-leases/{first['lease_id']}", json={"files": [{"name": "large.parquet", "size_bytes": 129 * 1024 * 1024}]})
        self.assertEqual(replacement.status_code, 200, replacement.text)
        self.assertTrue(replacement.json()["replaced"])
        self.assertEqual(replacement.json()["worker_size"], "LARGE")
        self.assertEqual(len(sqs.messages), 2)
        record = __import__("json").loads(s3.items[f"s3-uploader/worker-leases/{first['lease_id']}/lease.json"])
        self.assertEqual(record["state"], "CANCELLED")

    def test_cancel_and_start_over_cancels_attached_lease_and_deletes_exact_raw_version(self):
        self.client.post("/login", json={"password":"password"})
        store = S3JobStore(self.s3, "landing", "s3-uploader")
        raw_key = "s3-uploader/uploads/session/raw/source.parquet"
        self.s3.items[raw_key] = b"raw"
        store.put_compat_session({
            "session_id": "session", "owner_user_id": "local-admin", "expires_at": "2100-01-01T00:00:00+00:00",
            "phase": "READY_FOR_REVIEW", "files": [{"name": "source.parquet", "source_key": raw_key, "source_version_id": "version-123"}],
        })
        store.put_lease({
            "lease_id": "lease", "owner_user_id": "local-admin", "session_id": "session", "state": "AWAITING_KEY",
            "worker_size": "BASE", "expires_at": "2100-01-01T00:00:00+00:00", "cancellation_locked_at": None,
        })

        cancelled = self.client.delete("/api/v3/worker-leases/lease")

        self.assertEqual(cancelled.status_code, 204, cancelled.text)
        self.assertEqual(store.get_lease("lease")["state"], "CANCELLED")
        session = store.get_compat_session("session")
        self.assertEqual(session["phase"], "DELETED")
        self.assertFalse(session["cleanup_pending"])
        self.assertNotIn(raw_key, self.s3.items)
        self.assertEqual(self.s3.deleted_objects, [{"Bucket": "landing", "Key": raw_key, "VersionId": "version-123"}])
        self.assertEqual(self.client.delete("/api/v3/worker-leases/lease").status_code, 204)

    def test_cancel_and_start_over_is_refused_after_ingestion_acceptance(self):
        self.client.post("/login", json={"password":"password"})
        store = S3JobStore(self.s3, "landing", "s3-uploader")
        store.put_compat_session({
            "session_id": "session", "owner_user_id": "local-admin", "expires_at": "2100-01-01T00:00:00+00:00",
            "phase": "READY_FOR_REVIEW", "ingestion": {"job_id": "job"}, "files": [],
        })
        store.put_lease({
            "lease_id": "lease", "owner_user_id": "local-admin", "session_id": "session", "state": "AWAITING_CONFIRMATION",
            "worker_size": "BASE", "expires_at": "2100-01-01T00:00:00+00:00", "cancellation_locked_at": "2026-09-12T00:00:00+00:00",
        })

        refused = self.client.delete("/api/v3/worker-leases/lease")

        self.assertEqual(refused.status_code, 409, refused.text)
        self.assertEqual(refused.json()["detail"], "CANCEL_AND_START_OVER_UNAVAILABLE")
        self.assertEqual(store.get_lease("lease")["state"], "AWAITING_CONFIRMATION")

    def test_ingestion_acceptance_locks_the_attached_lease_before_creating_work(self):
        env = {
            "AWS_REGION": "ap-southeast-1", "S3_UPLOADER_LANDING_BUCKET": "landing", "S3_UPLOADER_LANDING_PREFIX": "s3-uploader", "S3_UPLOADER_CONTRACT_BUCKET": "ah-data-analytics", "S3_UPLOADER_CONTRACT_PREFIX": "temp_s3_update/web_ingest/table_contracts",             "S3_UPLOADER_BASE_QUEUE_URL": "base", "S3_UPLOADER_LARGE_QUEUE_URL":"large", "S3_UPLOADER_MUTATION_QUEUE_URL":"mutation",             "S3_UPLOADER_LOGIN_PASSWORD": "password", "S3_UPLOADER_LOGIN_SECRET": "x" * 32,
            "S3_UPLOADER_API_BASE_URL": "https://s3-uploader-v2.bot-alex.com", "S3_UPLOADER_GLUE_JOB_NAME": "job",
            "S3_UPLOADER_ENV": "development", "S3_UPLOADER_COOKIE_SECURE": "false",
            "S3_UPLOADER_BEARER_SECRET_ARN": "arn:aws:secretsmanager::0:secret/test",
        }
        s3, sqs, tables = FakeS3(), FakeSqs(), FakeS3Tables()
        settings = Settings.from_environ(env)
        client = self.enterContext(TestClient(create_app(settings, s3, sqs, tables, lifespan_bearer_auth=_test_bearer_service(settings))))
        client.post("/login", json={"password": "password"})
        store = S3JobStore(s3, "landing", "s3-uploader")
        store.put_compat_session({
            "session_id": "session", "owner_user_id": "local-admin", "expires_at": "2100-01-01T00:00:00+00:00",
            "mode": "create", "table_bucket_arn": tables.bucket_arn, "namespace": "pilot", "table": "new_table",
            "phase": "READY_FOR_REVIEW", "worker_lease_id": "lease",
            "files": [{"name": "source.parquet", "sha256": "a" * 64, "source_key": "s3-uploader/uploads/session/raw/source.parquet", "source_version_id": "version", "size_bytes": 1}],
            "preflight": {"accepted": True, "sanitization_review": {"manual_encryption_candidates": []}},
        })
        store.put_lease({
            "lease_id": "lease", "owner_user_id": "local-admin", "session_id": "session", "state": "AWAITING_CONFIRMATION",
            "worker_size": "BASE", "expires_at": "2100-01-01T00:00:00+00:00", "cancellation_locked_at": None,
        })

        accepted = client.post("/api/v3/upload-sessions/session/ingestions", json={
            "request_id": "request", "deduplication_mode": "none", "deduplication_columns": [],
        })

        self.assertEqual(accepted.status_code, 202, accepted.text)
        self.assertIsNotNone(store.get_lease("lease")["cancellation_locked_at"])
        self.assertEqual(client.delete("/api/v3/worker-leases/lease").status_code, 409)

    def test_manual_encryption_promotes_a_create_schema_field_to_string(self):
        self.client.post("/login", json={"password": "password"})
        store = S3JobStore(self.s3, "landing", "s3-uploader")
        store.put_compat_session({
            "session_id": "session", "owner_user_id": "local-admin", "expires_at": "2100-01-01T00:00:00+00:00",
            "mode": "create", "table_bucket_arn": self.s3tables.bucket_arn, "namespace": "pilot", "table": "new_table",
            "phase": "READY_FOR_REVIEW", "worker_lease_id": "lease",
            "files": [{"name": "source.parquet", "sha256": "a" * 64, "source_key": "s3-uploader/uploads/session/raw/source.parquet", "source_version_id": "version", "size_bytes": 1}],
            "preflight": {
                "accepted": True,
                "target_schema": [{"name": "numeric_id", "type": "BIGINT"}, {"name": "notes", "type": "STRING"}],
                "sanitization_review": {"manual_encryption_candidates": [{"column": "numeric_id"}]},
            },
        })
        store.put_lease({
            "lease_id": "lease", "owner_user_id": "local-admin", "session_id": "session", "state": "AWAITING_CONFIRMATION",
            "worker_size": "BASE", "expires_at": "2100-01-01T00:00:00+00:00", "cancellation_locked_at": None,
        })

        accepted = self.client.post("/api/v3/upload-sessions/session/ingestions", json={
            "request_id": "request", "manual_encryption_columns": ["numeric_id"],
        })

        self.assertEqual(accepted.status_code, 202, accepted.text)
        schema = store.get_compat_session("session")["preflight"]["target_schema"]
        self.assertEqual(schema, [{"name": "numeric_id", "type": "STRING"}, {"name": "notes", "type": "STRING"}])

    def test_resource_limited_base_lease_can_be_manually_retried_as_large(self):
        import json
        env = {"AWS_REGION":"ap-southeast-1", "S3_UPLOADER_LANDING_BUCKET":"landing", "S3_UPLOADER_LANDING_PREFIX":"s3-uploader", "S3_UPLOADER_CONTRACT_BUCKET":"ah-data-analytics", "S3_UPLOADER_CONTRACT_PREFIX":"temp_s3_update/web_ingest/table_contracts", "S3_UPLOADER_BASE_QUEUE_URL":"base", "S3_UPLOADER_LARGE_QUEUE_URL":"large", "S3_UPLOADER_MUTATION_QUEUE_URL":"mutation", "S3_UPLOADER_LOGIN_PASSWORD":"password", "S3_UPLOADER_LOGIN_SECRET":"x" * 32, "S3_UPLOADER_API_BASE_URL":"https://s3-uploader-v2.bot-alex.com", "S3_UPLOADER_GLUE_JOB_NAME":"s3-uploader-ingest", "S3_UPLOADER_ENV":"development", "S3_UPLOADER_COOKIE_SECURE":"false", "S3_UPLOADER_BEARER_SECRET_ARN":"arn:aws:secretsmanager::0:secret/test"}
        s3, sqs = FakeS3(), FakeSqs()
        settings = Settings.from_environ(env)
        client = self.enterContext(TestClient(create_app(settings, s3, sqs, FakeS3Tables(), lifespan_bearer_auth=_test_bearer_service(settings))))
        client.post("/login", json={"password":"password"})
        lease = client.post("/api/v3/worker-leases", json={"files": [{"name": "source.parquet", "size_bytes": 1}]}).json()
        lease_key = f"s3-uploader/worker-leases/{lease['lease_id']}/lease.json"
        record = json.loads(s3.items[lease_key])
        record.update({"session_id": "session", "state": "RESOURCE_LIMIT_EXCEEDED", "resume_phase": "RECEIVED", "can_retry_large": True})
        s3.items[lease_key] = json.dumps(record).encode()
        s3.items["s3-uploader/compat-sessions/session/session.json"] = json.dumps({"session_id": "session", "owner_user_id": "local-admin", "expires_at": "2099-01-01T00:00:00+00:00", "phase": "FAILED", "error": {"code": "RESOURCE_LIMIT_EXCEEDED"}}).encode()
        response = client.post(f"/api/v3/worker-leases/{lease['lease_id']}/retry-large")
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["worker_size"], "LARGE")
        self.assertEqual(sqs.message["QueueUrl"], "large")

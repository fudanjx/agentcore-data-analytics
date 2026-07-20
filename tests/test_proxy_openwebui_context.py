import concurrent.futures
import threading
import time
import unittest
from unittest.mock import AsyncMock, patch

import botocore.exceptions
from fastapi.testclient import TestClient

from proxy import server


USER_ID = "fce94d3d-e556-429e-8dcc-6565d9851512"
CHAT_ID = "ab3a94f6-aa17-46f5-8d74-c97f2ab0023f"
FILE_ID = "7a40b85b-c03d-43e1-a5b4-173953cc555c"
FILE_URI = (
    "s3://agentcore-openwebui-test-964340114883/"
    f"openwebui-test/{FILE_ID}_costs.csv"
)
INSIGHTS_FILE_URI = (
    "s3://agentcore-openwebui-insights-964340114883/"
    f"openwebui-insights/{FILE_ID}_costs.csv"
)


class FakeS3:
    def __init__(
        self,
        *,
        owner=USER_ID,
        file_id=FILE_ID,
        size=1945,
        fail=False,
        exists=True,
    ):
        self.owner = owner
        self.file_id = file_id
        self.size = size
        self.fail = fail
        self.exists = exists

    def list_objects_v2(self, **kwargs):
        if self.fail:
            raise botocore.exceptions.EndpointConnectionError(endpoint_url="https://s3.test")
        if not self.exists:
            return {"Contents": []}
        return {
            "Contents": [
                {
                    "Key": kwargs["Prefix"],
                    "Size": self.size,
                }
            ]
        }

    def get_object_tagging(self, **kwargs):
        return {
            "TagSet": [
                {"Key": "OpenWebUI-User-Id", "Value": self.owner},
                {"Key": "OpenWebUI-File-Id", "Value": self.file_id},
            ]
        }


class DynamicFakeS3(FakeS3):
    def get_object_tagging(self, **kwargs):
        file_id = kwargs["Key"].rsplit("/", 1)[-1].split("_", 1)[0]
        return {
            "TagSet": [
                {"Key": "OpenWebUI-User-Id", "Value": self.owner},
                {"Key": "OpenWebUI-File-Id", "Value": file_id},
            ]
        }


class SlowHarnessClient:
    def __init__(self):
        self.active = 0
        self.max_active = 0
        self.guard = threading.Lock()

    def invoke_harness(self, **kwargs):
        with self.guard:
            self.active += 1
            self.max_active = max(self.max_active, self.active)

        def events():
            try:
                time.sleep(0.1)
                yield {"contentBlockDelta": {"delta": {"text": "ok"}}}
            finally:
                with self.guard:
                    self.active -= 1

        return {"stream": events()}


class OpenWebUIIdentityTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(server.app)
        self.body = {
            "model": "harness",
            "messages": [{"role": "user", "content": "Hello"}],
            "stream": False,
        }
        self.headers = {
            "X-OpenWebUI-User-Id": USER_ID,
            "X-OpenWebUI-Chat-Id": CHAT_ID,
        }

    def manifest_entry(
        self,
        *,
        file_id=FILE_ID,
        uri=FILE_URI,
        filename="costs.csv",
        size=1945,
    ):
        return {
            "file_id": file_id,
            "s3_uri": uri,
            "filename": filename,
            "mime_type": "text/csv",
            "size": size,
        }

    def post_with_manifest(self, manifest, s3):
        completion = AsyncMock(return_value={"ok": True})
        with (
            patch.object(server, "_build_completion", completion),
            patch.object(server, "get_s3", return_value=s3),
        ):
            response = self.client.post(
                "/harness/v1/chat/completions",
                json={**self.body, "agentcore_files": manifest},
                headers=self.headers,
            )
        return response, completion

    def test_openwebui_headers_become_namespaced_actor_and_session(self):
        completion = AsyncMock(return_value={"ok": True})
        with patch.object(server, "_build_completion", completion):
            response = self.client.post(
                "/harness/v1/chat/completions",
                json=self.body,
                headers=self.headers,
            )

        self.assertEqual(response.status_code, 200)
        args = completion.await_args.args
        self.assertEqual(args[4], f"owui-{USER_ID}-{CHAT_ID}")
        self.assertEqual(args[5], f"openwebui:{USER_ID}")

    def test_insights_headers_use_separate_actor_and_session_namespaces(self):
        completion = AsyncMock(return_value={"ok": True})
        with patch.object(server, "_build_completion", completion):
            response = self.client.post(
                "/insights/v1/chat/completions",
                json=self.body,
                headers=self.headers,
            )

        self.assertEqual(response.status_code, 200)
        args = completion.await_args.args
        self.assertEqual(args[4], f"owui-insights-{USER_ID}-{CHAT_ID}")
        self.assertEqual(args[5], f"openwebui-insights:{USER_ID}")

    def test_insights_accepts_only_its_dedicated_s3_bucket_and_prefix(self):
        completion = AsyncMock(return_value={"ok": True})
        manifest = [
            self.manifest_entry(
                uri=INSIGHTS_FILE_URI,
            )
        ]
        with (
            patch.object(server, "_build_completion", completion),
            patch.object(server, "get_s3", return_value=FakeS3()),
        ):
            response = self.client.post(
                "/insights/v1/chat/completions",
                json={**self.body, "agentcore_files": manifest},
                headers=self.headers,
            )

        self.assertEqual(response.status_code, 200)
        system_content = "\n".join(
            message["content"]
            for message in completion.await_args.args[0]
            if message["role"] == "system"
        )
        self.assertIn(INSIGHTS_FILE_URI, system_content)

    def test_insights_rejects_the_local_poc_s3_bucket(self):
        completion = AsyncMock(return_value={"ok": True})
        with (
            patch.object(server, "_build_completion", completion),
            patch.object(server, "get_s3", return_value=FakeS3()),
        ):
            response = self.client.post(
                "/insights/v1/chat/completions",
                json={
                    **self.body,
                    "agentcore_files": [self.manifest_entry()],
                },
                headers=self.headers,
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "file_not_accessible")
        completion.assert_not_awaited()

    def test_foreground_request_forwards_only_system_and_latest_user_turn(self):
        completion = AsyncMock(return_value={"ok": True})
        body = {
            **self.body,
            "messages": [
                {"role": "system", "content": "System policy"},
                {"role": "user", "content": "First question"},
                {"role": "assistant", "content": "First answer"},
                {"role": "user", "content": "Second question"},
            ],
        }
        with patch.object(server, "_build_completion", completion):
            response = self.client.post(
                "/harness/v1/chat/completions",
                json=body,
                headers=self.headers,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            completion.await_args.args[0],
            [
                {"role": "system", "content": "System policy"},
                {"role": "user", "content": "Second question"},
            ],
        )

    def test_background_requests_use_unique_task_sessions_and_actor_namespace(self):
        completion = AsyncMock(return_value={"ok": True})
        body = {
            **self.body,
            "messages": [
                {"role": "user", "content": "Background task prompt"},
                {"role": "assistant", "content": "Background context"},
            ],
            "agentcore_request_context": {
                "kind": "background",
                "task": "follow_up_generation",
            },
        }
        with patch.object(server, "_build_completion", completion):
            first = self.client.post(
                "/harness/v1/chat/completions",
                json=body,
                headers=self.headers,
            )
            second = self.client.post(
                "/harness/v1/chat/completions",
                json=body,
                headers=self.headers,
            )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        first_args = completion.await_args_list[0].args
        second_args = completion.await_args_list[1].args
        self.assertTrue(first_args[4].startswith("owui-bg-"))
        self.assertTrue(second_args[4].startswith("owui-bg-"))
        self.assertNotEqual(first_args[4], second_args[4])
        self.assertEqual(first_args[5], f"openwebui-task:{USER_ID}")
        self.assertEqual(first_args[0], body["messages"])

    def test_same_foreground_session_harness_invocations_are_serialized(self):
        harness_client = SlowHarnessClient()

        def post():
            with TestClient(server.app) as client:
                return client.post(
                    "/harness/v1/chat/completions",
                    json=self.body,
                    headers=self.headers,
                )

        with patch.object(server, "get_client", return_value=harness_client):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                responses = list(executor.map(lambda _: post(), range(2)))

        self.assertTrue(all(response.status_code == 200 for response in responses))
        self.assertEqual(harness_client.max_active, 1)

    def test_missing_openwebui_user_header_is_rejected(self):
        completion = AsyncMock(return_value={"ok": True})
        with patch.object(server, "_build_completion", completion):
            response = self.client.post(
                "/harness/v1/chat/completions",
                json=self.body,
                headers={"X-OpenWebUI-Chat-Id": CHAT_ID},
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "identity_context_required")
        completion.assert_not_awaited()

    def test_missing_openwebui_chat_header_is_rejected(self):
        completion = AsyncMock(return_value={"ok": True})
        with patch.object(server, "_build_completion", completion):
            response = self.client.post(
                "/harness/v1/chat/completions",
                json=self.body,
                headers={"X-OpenWebUI-User-Id": USER_ID},
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "identity_context_required")
        completion.assert_not_awaited()

    def test_valid_owned_file_is_injected_as_system_context(self):
        response, completion = self.post_with_manifest(
            [self.manifest_entry()], FakeS3()
        )

        self.assertEqual(response.status_code, 200)
        messages = completion.await_args.args[0]
        system_content = "\n".join(
            message["content"] for message in messages if message["role"] == "system"
        )
        self.assertIn(FILE_URI, system_content)
        self.assertIn("Code Interpreter", system_content)
        self.assertIn("aws s3 cp", system_content)
        self.assertIn("--region ap-southeast-1", system_content)
        self.assertIn("Do not use requests", system_content)

    def test_wrong_owner_tag_rejects_entire_request(self):
        response, completion = self.post_with_manifest(
            [self.manifest_entry()], FakeS3(owner="another-user")
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "file_not_accessible")
        completion.assert_not_awaited()

    def test_wrong_file_id_tag_is_rejected(self):
        response, completion = self.post_with_manifest(
            [self.manifest_entry()], FakeS3(file_id="another-file")
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "file_not_accessible")
        completion.assert_not_awaited()

    def test_missing_object_is_rejected(self):
        response, completion = self.post_with_manifest(
            [self.manifest_entry()], FakeS3(exists=False)
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "file_not_accessible")
        completion.assert_not_awaited()

    def test_non_allowlisted_bucket_is_rejected(self):
        entry = self.manifest_entry(
            uri=f"s3://another-bucket/openwebui-test/{FILE_ID}_costs.csv"
        )
        response, completion = self.post_with_manifest([entry], FakeS3())

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "file_not_accessible")
        completion.assert_not_awaited()

    def test_unsupported_extension_is_rejected(self):
        entry = self.manifest_entry(filename="payload.zip")
        response, completion = self.post_with_manifest([entry], FakeS3())

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_file_manifest")
        completion.assert_not_awaited()

    def test_malformed_manifest_is_rejected(self):
        response, completion = self.post_with_manifest("not-a-list", FakeS3())

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_file_manifest")
        completion.assert_not_awaited()

    def test_file_over_50_mb_is_rejected(self):
        response, completion = self.post_with_manifest(
            [self.manifest_entry()],
            FakeS3(size=server.MAX_UPLOAD_BYTES + 1),
        )

        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()["error"]["code"], "file_limit_exceeded")
        completion.assert_not_awaited()

    def test_more_than_10_files_is_rejected(self):
        entries = [self.manifest_entry() for _ in range(server.MAX_FILES_PER_CHAT + 1)]
        response, completion = self.post_with_manifest(entries, FakeS3())

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "too_many_files")
        completion.assert_not_awaited()

    def test_combined_size_over_200_mb_is_rejected(self):
        entries = []
        for index in range(5):
            file_id = f"file-{index}"
            entries.append(
                self.manifest_entry(
                    file_id=file_id,
                    uri=(
                        "s3://agentcore-openwebui-test-964340114883/"
                        f"openwebui-test/{file_id}_costs.csv"
                    ),
                )
            )
        response, completion = self.post_with_manifest(
            entries,
            DynamicFakeS3(size=server.MAX_UPLOAD_BYTES),
        )

        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()["error"]["code"], "file_limit_exceeded")
        completion.assert_not_awaited()

    def test_s3_metadata_failure_is_rejected_before_agentcore(self):
        response, completion = self.post_with_manifest(
            [self.manifest_entry()], FakeS3(fail=True)
        )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"]["code"], "file_validation_failed")
        completion.assert_not_awaited()

    def test_text_only_request_does_not_add_file_system_context(self):
        completion = AsyncMock(return_value={"ok": True})
        with patch.object(server, "_build_completion", completion):
            response = self.client.post(
                "/harness/v1/chat/completions",
                json=self.body,
                headers=self.headers,
            )

        self.assertEqual(response.status_code, 200)
        messages = completion.await_args.args[0]
        self.assertEqual(messages, self.body["messages"])


if __name__ == "__main__":
    unittest.main()

import concurrent.futures
import json
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
INSIGHTS_FILE_URI = (
    "s3://agentcore-openwebui-insights-964340114883/"
    f"openwebui-insights/{FILE_ID}_costs.csv"
)
NON_INSIGHTS_FILE_URI = (
    "s3://agentcore-openwebui-test-964340114883/"
    f"openwebui-test/{FILE_ID}_costs.csv"
)
OFFICE_ARTIFACT_URI = (
    "s3://agentcore-openwebui-insights-964340114883/"
    f"openwebui-insights/outputs/{USER_ID}/{CHAT_ID}/report.xlsx"
)
DIFY_ARTIFACT_URI = (
    f"s3://{server.DIFY_OFFICE_ARTIFACTS_BUCKET}/"
    f"{server.DIFY_OFFICE_ARTIFACTS_PREFIX}{USER_ID}/{CHAT_ID}/report.xlsx"
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
        self.presign_calls = []

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

    def generate_presigned_url(self, operation, **kwargs):
        self.presign_calls.append((operation, kwargs))
        return "https://downloads.example/report.xlsx?signature=test"


class DynamicFakeS3(FakeS3):
    def get_object_tagging(self, **kwargs):
        file_id = kwargs["Key"].rsplit("/", 1)[-1].split("_", 1)[0]
        return {
            "TagSet": [
                {"Key": "OpenWebUI-User-Id", "Value": self.owner},
                {"Key": "OpenWebUI-File-Id", "Value": file_id},
            ]
        }


class OfficeArtifactS3(FakeS3):
    def get_object_tagging(self, **kwargs):
        return {
            "TagSet": [
                {"Key": "OpenWebUI-User-Id", "Value": USER_ID},
                {"Key": "OpenWebUI-Chat-Id", "Value": CHAT_ID},
                {"Key": "AgentCore-Artifact", "Value": "generated"},
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
        uri=INSIGHTS_FILE_URI,
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
                "/insights/v1/chat/completions",
                json={**self.body, "agentcore_files": manifest},
                headers=self.headers,
            )
        return response, completion

    def test_dify_harness_requires_identity_context(self):
        completion = AsyncMock(return_value={"ok": True})
        with (
            patch.object(server, "_build_completion", completion),
            patch.object(
                server,
                "get_s3",
                side_effect=AssertionError("invalid request must not access S3"),
            ),
        ):
            response = self.client.post(
                "/harness/v1/chat/completions",
                json={**self.body, "agentcore_files": "not-a-list"},
            )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            response.json()["detail"]["error"]["code"],
            "identity_context_required",
        )
        completion.assert_not_awaited()

    def test_insights_headers_become_namespaced_actor_and_session(self):
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

    def test_office_route_keeps_the_insights_actor_and_session_namespace(self):
        context = server._extract_openwebui_context(
            type("Request", (), {"headers": {k.lower(): v for k, v in self.headers.items()}})(),
            {},
            server.INSIGHTS_OPENWEBUI_SOURCE_PROFILE,
        )
        self.assertEqual(context[0], f"owui-insights-{USER_ID}-{CHAT_ID}")
        self.assertEqual(context[1], f"openwebui-insights:{USER_ID}")

    def test_office_artifact_registration_accepts_only_tagged_user_chat_output(self):
        with patch.object(server, "get_s3", return_value=OfficeArtifactS3(size=120)):
            response = self.client.post(
                "/insights-office/v1/artifacts/register",
                json={
                    "artifacts": [
                        {"s3_uri": OFFICE_ARTIFACT_URI, "filename": "report.xlsx"}
                    ]
                },
                headers=self.headers,
            )
        self.assertEqual(response.status_code, 200)
        artifact = response.json()["artifacts"][0]
        self.assertEqual(artifact["s3_uri"], OFFICE_ARTIFACT_URI)
        self.assertEqual(artifact["filename"], "report.xlsx")

    def test_office_artifact_registration_rejects_another_chat_prefix(self):
        wrong_chat_uri = OFFICE_ARTIFACT_URI.replace(CHAT_ID, "another-chat")
        with patch.object(server, "get_s3", return_value=OfficeArtifactS3(size=120)):
            response = self.client.post(
                "/insights-office/v1/artifacts/register",
                json={
                    "artifacts": [
                        {"s3_uri": wrong_chat_uri, "filename": "report.xlsx"}
                    ]
                },
                headers=self.headers,
            )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "artifact_not_accessible")

    def test_office_progress_statuses_never_expose_tool_details(self):
        self.assertEqual(
            server._safe_tool_status(
                {"name": "agentcore_code_interpreter", "serverName": "private"}
            ),
            "Running Code Interpreter",
        )
        self.assertEqual(
            server._safe_tool_status({"name": "execute_sql", "serverName": "db"}),
            "Calling connected data tool",
        )

    def test_office_artifact_stream_sanitizer_hides_raw_marker_and_s3_uri(self):
        sanitizer = server._OfficeArtifactStreamSanitizer()
        pieces = []
        pieces.extend(sanitizer.feed("Report created.\n<agentcore-art"))
        pieces.extend(
            sanitizer.feed(
                "ifacts>\n[{\"s3_uri\":\"%s\",\"filename\":\"report.xlsx\"}]"
                "\n</agentcore-artifacts>"
                % OFFICE_ARTIFACT_URI
            )
        )
        pieces.extend(sanitizer.finish())

        output = "".join(pieces)
        self.assertIn("Report created.", output)
        self.assertIn("<!--agentcore-artifacts:", output)
        self.assertNotIn("<agentcore-artifacts>", output)
        self.assertNotIn(OFFICE_ARTIFACT_URI, output)

    def test_office_stream_discovers_new_output_when_agent_marker_is_unusable(self):
        with (
            patch.object(
                server,
                "_stream_harness_events_with_progress",
                return_value=iter(
                    [
                        ("text", "Workbook created.\n<agentcore-artifacts>not-json"),
                        ("text", "</agentcore-artifacts>"),
                    ]
                ),
            ),
            patch.object(
                server,
                "_discover_openwebui_office_artifacts",
                return_value=[
                    {
                        "s3_uri": OFFICE_ARTIFACT_URI,
                        "filename": "report.xlsx",
                    }
                ],
            ) as discover,
        ):
            chunks = list(
                server._sse_harness_office_stream(
                    [],
                    "harness-arn",
                    "session-id",
                    "actor-id",
                    "insights-office",
                    "completion-id",
                    artifact_context=(
                        USER_ID,
                        CHAT_ID,
                        server.INSIGHTS_OPENWEBUI_SOURCE_PROFILE,
                    ),
                )
            )

        contents = []
        for chunk in chunks:
            if not chunk.startswith("data: {"):
                continue
            payload = json.loads(chunk[6:])
            contents.append(
                ((payload.get("choices") or [{}])[0].get("delta") or {}).get("content")
            )
        combined = "".join(item or "" for item in contents)
        discover.assert_called_once()
        self.assertIn("Workbook created.", combined)
        self.assertIn("<!--agentcore-artifacts:", combined)
        self.assertNotIn("Generated file could not be made available", combined)

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
                    "agentcore_files": [
                        self.manifest_entry(uri=NON_INSIGHTS_FILE_URI)
                    ],
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
                "/insights/v1/chat/completions",
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
                "/insights/v1/chat/completions",
                json=body,
                headers=self.headers,
            )
            second = self.client.post(
                "/insights/v1/chat/completions",
                json=body,
                headers=self.headers,
            )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        first_args = completion.await_args_list[0].args
        second_args = completion.await_args_list[1].args
        self.assertTrue(first_args[4].startswith("owui-insights-bg-"))
        self.assertTrue(second_args[4].startswith("owui-insights-bg-"))
        self.assertNotEqual(first_args[4], second_args[4])
        self.assertEqual(first_args[5], f"openwebui-insights-task:{USER_ID}")
        self.assertEqual(first_args[0], body["messages"])

    def test_same_foreground_session_harness_invocations_are_serialized(self):
        harness_client = SlowHarnessClient()

        def post():
            with TestClient(server.app) as client:
                return client.post(
                    "/insights/v1/chat/completions",
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
                "/insights/v1/chat/completions",
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
                "/insights/v1/chat/completions",
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
        self.assertIn(INSIGHTS_FILE_URI, system_content)
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
                        "s3://agentcore-openwebui-insights-964340114883/"
                        f"openwebui-insights/{file_id}_costs.csv"
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
                "/insights/v1/chat/completions",
                json=self.body,
                headers=self.headers,
            )

        self.assertEqual(response.status_code, 200)
        messages = completion.await_args.args[0]
        self.assertEqual(messages, self.body["messages"])


class DifyOfficeArtifactContextTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(server.app)

    def test_dify_style_slugs_receive_scoped_office_output_instructions(self):
        for slug in ("harness", "dify", "dify-eks"):
            with self.subTest(slug=slug):
                body = {
                    "model": slug,
                    "user": USER_ID,
                    "messages": [
                        {
                            "role": "system",
                            "content": f"System instructions <C_ID>{CHAT_ID}<C_ID>",
                        },
                        {"role": "user", "content": "Create a concise report"},
                    ],
                    "stream": False,
                }
                completion = AsyncMock(return_value={"ok": True})

                with patch.object(server, "_build_completion", completion):
                    response = self.client.post(
                        f"/{slug}/v1/chat/completions",
                        json=body,
                    )

                self.assertEqual(response.status_code, 200)
                messages = completion.await_args.args[0]
                system_content = "\n".join(
                    message["content"]
                    for message in messages
                    if message["role"] == "system"
                )
                self.assertIn(
                    f"s3://{server.DIFY_OFFICE_ARTIFACTS_BUCKET}/"
                    f"{server.DIFY_OFFICE_ARTIFACTS_PREFIX}{USER_ID}/{CHAT_ID}/",
                    system_content,
                )
                self.assertIn("aws s3api put-object", system_content)
                self.assertNotIn("aws s3 presign", system_content)
                self.assertIn("trusted proxy", system_content)
                self.assertIn(f"OpenWebUI-User-Id={USER_ID}", system_content)
                self.assertIn(f"OpenWebUI-Chat-Id={CHAT_ID}", system_content)
                self.assertNotIn("<C_ID>", system_content)

    def test_buffered_dify_artifact_is_validated_and_presigned_by_proxy(self):
        body = {
            "model": "dify",
            "user": USER_ID,
            "messages": [
                {
                    "role": "system",
                    "content": f"System instructions <C_ID>{CHAT_ID}<C_ID>",
                },
                {"role": "user", "content": "Create a workbook"},
            ],
            "stream": False,
        }
        harness_result = (
            "Workbook created.\n<agentcore-artifacts>"
            f'[{json.dumps({"s3_uri": DIFY_ARTIFACT_URI, "filename": "report.xlsx"})}]'
            "</agentcore-artifacts>"
        )
        s3 = OfficeArtifactS3(size=120)

        with (
            patch.object(server, "_invoke_harness_buffered", return_value=harness_result),
            patch.object(server, "get_s3", return_value=s3),
        ):
            response = self.client.post("/dify/v1/chat/completions", json=body)

        self.assertEqual(response.status_code, 200)
        content = response.json()["choices"][0]["message"]["content"]
        self.assertIn("Workbook created.", content)
        self.assertIn("https://downloads.example/report.xlsx?signature=test", content)
        self.assertIn("Links expire in 60 minutes.", content)
        self.assertNotIn("<agentcore-artifacts>", content)
        self.assertNotIn(DIFY_ARTIFACT_URI, content)
        self.assertEqual(len(s3.presign_calls), 1)
        operation, kwargs = s3.presign_calls[0]
        self.assertEqual(operation, "get_object")
        self.assertEqual(kwargs["ExpiresIn"], 3600)

    def test_buffered_dify_artifact_rejects_forged_s3_uri(self):
        body = {
            "model": "dify",
            "user": USER_ID,
            "messages": [
                {
                    "role": "system",
                    "content": f"System instructions <C_ID>{CHAT_ID}<C_ID>",
                },
                {"role": "user", "content": "Create a workbook"},
            ],
            "stream": False,
        }
        forged_uri = (
            f"s3://{server.DIFY_OFFICE_ARTIFACTS_BUCKET}/"
            f"{server.DIFY_OFFICE_ARTIFACTS_PREFIX}another-user/{CHAT_ID}/report.xlsx"
        )
        harness_result = (
            "Workbook created.\n<agentcore-artifacts>"
            f'[{json.dumps({"s3_uri": forged_uri, "filename": "report.xlsx"})}]'
            "</agentcore-artifacts>"
        )
        s3 = OfficeArtifactS3(size=120)

        with (
            patch.object(server, "_invoke_harness_buffered", return_value=harness_result),
            patch.object(server, "get_s3", return_value=s3),
        ):
            response = self.client.post("/dify/v1/chat/completions", json=body)

        self.assertEqual(response.status_code, 200)
        content = response.json()["choices"][0]["message"]["content"]
        self.assertIn("Generated file could not be made available", content)
        self.assertNotIn(forged_uri, content)
        self.assertEqual(s3.presign_calls, [])

    def test_streaming_dify_artifact_is_presigned_after_validation(self):
        marker = (
            "<agentcore-artifacts>"
            f'[{json.dumps({"s3_uri": DIFY_ARTIFACT_URI, "filename": "report.xlsx"})}]'
            "</agentcore-artifacts>"
        )
        s3 = OfficeArtifactS3(size=120)
        with (
            patch.object(
                server,
                "_stream_harness_events",
                return_value=iter(["Workbook created.\n", marker[:30], marker[30:]]),
            ),
            patch.object(server, "get_s3", return_value=s3),
        ):
            chunks = list(
                server._sse_harness_dify_artifact_stream(
                    [],
                    "harness-arn",
                    CHAT_ID,
                    USER_ID,
                    "dify",
                    "completion-id",
                    (
                        USER_ID,
                        CHAT_ID,
                        server.DIFY_OFFICE_SOURCE_PROFILE,
                    ),
                )
            )

        contents = []
        for chunk in chunks:
            if not chunk.startswith("data: {"):
                continue
            payload = json.loads(chunk[6:])
            contents.append(
                ((payload.get("choices") or [{}])[0].get("delta") or {}).get("content")
            )
        combined = "".join(item or "" for item in contents)
        self.assertIn("Workbook created.", combined)
        self.assertIn("https://downloads.example/report.xlsx?signature=test", combined)
        self.assertNotIn(DIFY_ARTIFACT_URI, combined)
        self.assertNotIn("<agentcore-artifacts>", combined)
        self.assertEqual(len(s3.presign_calls), 1)


if __name__ == "__main__":
    unittest.main()

import asyncio
import json
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
HTML_ARTIFACT_URI = (
    "s3://agentcore-openwebui-insights-964340114883/"
    f"openwebui-insights/outputs/{USER_ID}/{CHAT_ID}/dashboard.html"
)
OPENWEBUI_HEADERS = {
    "X-OpenWebUI-User-Id": USER_ID,
    "X-OpenWebUI-Chat-Id": CHAT_ID,
}


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


class RuntimeEventBody:
    def __init__(self, lines):
        self.lines = lines

    def iter_lines(self):
        return iter(self.lines)


class RuntimeEventClient:
    def __init__(self, lines):
        self.lines = lines

    def invoke_agent_runtime(self, **_kwargs):
        return {"response": RuntimeEventBody(self.lines)}


class HarnessEventClient:
    def __init__(self, events):
        self.events = events
        self.kwargs = None

    def invoke_harness(self, **kwargs):
        self.kwargs = kwargs
        return {"stream": iter(self.events)}


class TransientHarnessCredentialClient:
    def __init__(self, failures):
        self.failures = failures
        self.calls = 0

    def invoke_harness(self, **_kwargs):
        self.calls += 1
        if self.calls <= self.failures:
            raise botocore.exceptions.EventStreamError(
                {
                    "Error": {
                        "Code": "runtimeClientError",
                        "Message": (
                            "Failed to start MCP client: the client initialization "
                            "failed: Unable to locate credentials"
                        ),
                    }
                },
                "InvokeHarness",
            )
        return {"stream": iter([{"contentBlockDelta": {"delta": {"text": "OK"}}}])}


class OpenWebUIIdentityTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(server.app)
        self.body = {
            "model": "strands",
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

    def test_buffered_artifact_sanitizer_defers_opaque_marker_to_caller(self):
        sanitizer = server._OfficeArtifactStreamSanitizer(emit_opaque_marker=False)
        output = sanitizer.feed(
            '<agentcore-artifacts>[{"s3_uri":"%s","filename":"report.xlsx"}]</agentcore-artifacts>'
            % OFFICE_ARTIFACT_URI
        )

        self.assertEqual(output, [])
        self.assertEqual(sanitizer.artifacts[0]["s3_uri"], OFFICE_ARTIFACT_URI)
        self.assertTrue(sanitizer.artifact_emitted)

    def test_office_stream_discovers_new_output_when_agent_marker_is_unusable(self):
        with (
            patch.object(
                server,
                "_stream_backend_events",
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
            async def collect():
                return [
                    chunk
                    async for chunk in server._sse_runtime_stream(
                        [],
                        "runtime",
                        "runtime-arn",
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
                ]

            chunks = asyncio.run(collect())

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
        forwarded = completion.await_args.args[0]
        self.assertEqual(
            forwarded[:2],
            [
                {"role": "system", "content": "System policy"},
                {"role": "user", "content": "Second question"},
            ],
        )
        self.assertIn("## Generated files", forwarded[2]["content"])

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

    def test_text_only_request_adds_output_but_not_input_file_context(self):
        completion = AsyncMock(return_value={"ok": True})
        with patch.object(server, "_build_completion", completion):
            response = self.client.post(
                "/insights/v1/chat/completions",
                json=self.body,
                headers=self.headers,
            )

        self.assertEqual(response.status_code, 200)
        messages = completion.await_args.args[0]
        self.assertEqual(messages[0], self.body["messages"][0])
        system_content = "\n".join(
            item["content"] for item in messages if item["role"] == "system"
        )
        self.assertIn("## Generated files", system_content)
        self.assertNotIn("## Files available in this OpenWebUI chat", system_content)

    def test_office_artifact_context_requires_tagged_prefix_for_s3_access_checks(self):
        messages = server._inject_openwebui_office_artifact_context(
            [{"role": "user", "content": "Can you check S3 access?"}],
            USER_ID,
            CHAT_ID,
            server.INSIGHTS_OPENWEBUI_SOURCE_PROFILE,
        )

        instruction = messages[-1]["content"]
        self.assertIn("perform the check by writing a small temporary", instruction)
        self.assertIn("file below this exact per-request output prefix", instruction)
        self.assertIn("all three tags above", instruction)
        self.assertIn("not test an arbitrary S3 key", instruction)


class RuntimeRouterTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(server.app)
        self.body = {
            "model": "strands",
            "messages": [{"role": "user", "content": "Hello"}],
            "stream": False,
        }

    def test_canonical_models_are_discoverable(self):
        for slug in ("strands", "insights-office", "gmio-pcr-dev"):
            with self.subTest(slug=slug):
                response = self.client.get(f"/{slug}/v1/models")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["data"][0]["id"], slug)

    def test_runtime_registry_rejects_harness_arns_and_bad_slugs(self):
        with self.assertRaisesRegex(RuntimeError, "invalid runtime ARN"):
            server._load_runtime_routes(
                json.dumps(
                    {
                        "strands": {
                            "runtime_arn": (
                                "arn:aws:bedrock-agentcore:ap-southeast-1:"
                                "964340114883:harness/legacy"
                            ),
                            "model_name": "Legacy",
                        }
                    }
                )
            )

    def test_runtime_registry_accepts_one_harness_backend(self):
        routes = server._load_runtime_routes(
            json.dumps(
                {
                    "strands": server.DEFAULT_RUNTIME_ROUTES["strands"],
                    "office": {
                        "harness_arn": (
                            "arn:aws:bedrock-agentcore:ap-southeast-1:"
                            "964340114883:harness/harness_insights_office-NXyYkHT02U"
                        ),
                        "model_name": "Office",
                    },
                }
            )
        )

        self.assertEqual(routes["office"]["backend_type"], "harness")
        self.assertIn(":harness/", routes["office"]["backend_arn"])
        with self.assertRaisesRegex(RuntimeError, "Invalid AgentCore runtime slug"):
            server._load_runtime_routes(
                json.dumps(
                    {
                        "Bad_Slug": {
                            "runtime_arn": server.DEFAULT_RUNTIME_ROUTES["strands"][
                                "runtime_arn"
                            ],
                            "model_name": "Bad",
                        },
                        "strands": server.DEFAULT_RUNTIME_ROUTES["strands"],
                    }
                )
            )

    def test_compatibility_routes_resolve_to_strands(self):
        completion = AsyncMock(return_value={"ok": True})
        with patch.object(server, "_build_completion", completion):
            root = self.client.post(
                "/v1/chat/completions", json=self.body, headers=OPENWEBUI_HEADERS
            )
            insights = self.client.post(
                "/insights/v1/chat/completions",
                json=self.body,
                headers=OPENWEBUI_HEADERS,
            )

        self.assertEqual(root.status_code, 200)
        self.assertEqual(insights.status_code, 200)
        self.assertEqual(completion.await_args_list[0].args[1], "strands")
        self.assertEqual(completion.await_args_list[1].args[1], "strands")

    def test_every_canonical_route_requires_trusted_identity_headers(self):
        for slug in ("strands", "insights-office", "gmio-pcr-dev"):
            with self.subTest(slug=slug):
                response = self.client.post(
                    f"/{slug}/v1/chat/completions", json=self.body
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(
                    response.json()["error"]["code"], "identity_context_required"
                )

    def test_removed_dify_routes_return_not_found(self):
        self.assertEqual(
            self.client.post("/dify/strands/v1/chat-messages", json={}).status_code,
            404,
        )
        self.assertEqual(
            self.client.post("/dify/strands/files/upload").status_code,
            404,
        )
        self.assertFalse(
            any(path.startswith("/dify/") for path in server.app.openapi()["paths"])
        )

    def test_html_artifact_can_be_registered_for_every_canonical_slug(self):
        with patch.object(server, "get_s3", return_value=OfficeArtifactS3()):
            for slug in ("strands", "insights-office", "gmio-pcr-dev"):
                with self.subTest(slug=slug):
                    response = self.client.post(
                        f"/{slug}/v1/artifacts/register",
                        json={
                            "artifacts": [
                                {
                                    "s3_uri": HTML_ARTIFACT_URI,
                                    "filename": "dashboard.html",
                                }
                            ]
                        },
                        headers=OPENWEBUI_HEADERS,
                    )
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(
                        response.json()["artifacts"][0]["mime_type"], "text/html"
                    )

    def test_runtime_tool_events_are_forwarded_individually_in_order(self):
        lines = [
            b'data: {"event":"agent_step","step":{"type":"tool","name":"SQL query","status":"started","details":{"input":"secret"}}}',
            b'data: {"event":"agent_step","step":{"type":"mcp","name":"TimesFM","status":"started"}}',
            b'data: {"event":"agent_step","step":{"type":"tool","name":"SQL query","status":"completed","details":{"result":"secret"}}}',
            b'data: {"choices":[{"delta":{"content":"Done"}}]}',
            b"data: [DONE]",
        ]
        with patch.object(server, "get_client", return_value=RuntimeEventClient(lines)):
            events = list(
                server._stream_runtime_events(
                    [], "runtime-arn", "session-id", "actor-id"
                )
            )

        self.assertEqual(
            [kind for kind, _value in events],
            ["status", "status", "status", "text"],
        )
        descriptions = [
            value["description"] for kind, value in events if kind == "status"
        ]
        self.assertEqual(
            descriptions,
            [
                "Starting tool: SQL query",
                "Starting MCP: TimesFM",
                "Completed tool: SQL query",
            ],
        )
        self.assertNotIn("secret", json.dumps(events))
        self.assertNotIn("Preparing final answer", json.dumps(events))

    def test_harness_tool_events_are_forwarded_individually_in_order(self):
        events = [
            {
                "contentBlockStart": {
                    "contentBlockIndex": 1,
                    "start": {
                        "toolUse": {
                            "name": "query_database",
                            "serverName": "analytics",
                            "input": {"sql": "secret"},
                        }
                    },
                }
            },
            {"contentBlockDelta": {"delta": {"text": "Done"}}},
            {"contentBlockStop": {"contentBlockIndex": 1}},
            {"messageStop": {"stopReason": "end_turn"}},
        ]
        client = HarnessEventClient(events)
        messages = [
            {"role": "system", "content": "Policy"},
            {"role": "user", "content": "Question"},
        ]
        with patch.object(server, "get_client", return_value=client):
            streamed = list(
                server._stream_backend_events(
                    messages,
                    "harness",
                    "harness-arn",
                    "session-id",
                    "actor-id",
                )
            )

        self.assertEqual(
            [kind for kind, _value in streamed],
            ["status", "text", "status"],
        )
        self.assertEqual(streamed[0][1]["description"], "Starting MCP: query_database")
        self.assertEqual(streamed[2][1]["description"], "Completed MCP: query_database")
        self.assertNotIn("secret", json.dumps(streamed))
        self.assertNotIn("Preparing final answer", json.dumps(streamed))
        self.assertEqual(client.kwargs["actorId"], "actor-id")
        self.assertEqual(client.kwargs["systemPrompt"], [{"text": "Policy"}])

    def test_harness_retries_pre_response_gateway_credential_bootstrap(self):
        client = TransientHarnessCredentialClient(failures=2)
        with (
            patch.object(server, "get_client", return_value=client),
            patch.object(server.time, "sleep") as sleep,
        ):
            streamed = list(
                server._stream_harness_events(
                    [{"role": "user", "content": "Question"}],
                    "harness-arn",
                    "session-id",
                    "actor-id",
                )
            )

        self.assertEqual(streamed, [("text", "OK")])
        self.assertEqual(client.calls, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2])


if __name__ == "__main__":
    unittest.main()

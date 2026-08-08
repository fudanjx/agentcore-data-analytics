import asyncio
import importlib.util
import json
import pathlib
import time
import unittest
from unittest.mock import AsyncMock, patch


MODULE_PATH = (
    pathlib.Path(__file__).parents[1] / "dify-proxy" / "dify-server.py"
)
SPEC = importlib.util.spec_from_file_location("dify_server_runtime_tests", MODULE_PATH)
dify_server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dify_server)


class FakeStreamingBody:
    def __init__(self, lines):
        self.lines = lines

    def iter_lines(self):
        return iter(self.lines)


class FakeRuntimeClient:
    def __init__(self, lines):
        self.lines = lines
        self.calls = []

    def invoke_agent_runtime(self, **kwargs):
        self.calls.append(kwargs)
        return {"response": FakeStreamingBody(self.lines)}


class FakePaginator:
    def __init__(self, pages):
        self.pages = pages

    def paginate(self):
        return iter(self.pages)


class FakeControlClient:
    def __init__(self, pages):
        self.pages = pages

    def get_paginator(self, operation):
        if operation != "list_agent_runtimes":
            raise AssertionError(f"Unexpected operation: {operation}")
        return FakePaginator(self.pages)


class FakePresignS3:
    def __init__(self):
        self.calls = []

    def generate_presigned_url(self, operation, **kwargs):
        self.calls.append((operation, kwargs))
        return "https://downloads.example/report.xlsx?signature=test"


class FakeRequest:
    def __init__(self, body):
        self.body = body

    async def json(self):
        return self.body


class DifyRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.original_runtimes = dict(dify_server.DIFY_RUNTIMES)
        self.original_enabled = dify_server.DIFY_RUNTIME_DISCOVERY_ENABLED
        self.original_attempted = dify_server._runtime_discovery_attempted
        self.original_refreshed_at = dify_server._runtime_discovery_refreshed_at

        dify_server.DIFY_RUNTIME_DISCOVERY_ENABLED = True
        dify_server.DIFY_RUNTIMES.clear()
        dify_server.DIFY_RUNTIMES["dev"] = "arn:runtime"
        dify_server._runtime_discovery_attempted = True
        dify_server._runtime_discovery_refreshed_at = time.monotonic()

    def tearDown(self):
        dify_server.DIFY_RUNTIMES.clear()
        dify_server.DIFY_RUNTIMES.update(self.original_runtimes)
        dify_server.DIFY_RUNTIME_DISCOVERY_ENABLED = self.original_enabled
        dify_server._runtime_discovery_attempted = self.original_attempted
        dify_server._runtime_discovery_refreshed_at = self.original_refreshed_at

    def test_runtime_backend_resolves_without_harness_discovery(self):
        with patch.object(
            dify_server,
            "get_dify_harness_arn",
            side_effect=AssertionError("runtime should resolve first"),
        ):
            backend = dify_server.get_dify_backend("dev")

        self.assertEqual(backend, ("runtime", "arn:runtime"))

    def test_discovers_ready_runtimes_across_pages_and_adds_short_alias(self):
        dify_server.DIFY_RUNTIMES.clear()
        dify_server._runtime_discovery_attempted = False
        client = FakeControlClient(
            [
                {
                    "agentRuntimes": [
                        {
                            "agentRuntimeName": "agentcore_dev",
                            "agentRuntimeArn": "arn:dev",
                            "status": "READY",
                        },
                        {
                            "agentRuntimeName": "still_creating",
                            "agentRuntimeArn": "arn:creating",
                            "status": "CREATING",
                        },
                    ]
                },
                {
                    "agentRuntimes": [
                        {
                            "agentRuntimeName": "analytics",
                            "agentRuntimeArn": "arn:analytics",
                            "status": "READY",
                        }
                    ]
                },
            ]
        )

        with patch.object(
            dify_server,
            "get_agentcore_control_client",
            return_value=client,
        ):
            runtimes = dify_server.refresh_dify_runtimes(force=True)

        self.assertEqual(runtimes["agentcore_dev"], "arn:dev")
        self.assertEqual(runtimes["dev"], "arn:dev")
        self.assertEqual(runtimes["analytics"], "arn:analytics")
        self.assertNotIn("still_creating", runtimes)

    def test_runtime_discovery_failure_preserves_cached_runtimes(self):
        cached = {"cached": "arn:cached"}
        dify_server.DIFY_RUNTIMES.clear()
        dify_server.DIFY_RUNTIMES.update(cached)

        with patch.object(
            dify_server,
            "get_agentcore_control_client",
            side_effect=RuntimeError("access denied"),
        ):
            runtimes = dify_server.refresh_dify_runtimes(force=True)

        self.assertEqual(runtimes, cached)
        self.assertEqual(dify_server.DIFY_RUNTIMES, cached)

    def test_runtime_kwargs_include_runtime_and_payload_identity(self):
        kwargs = dify_server._runtime_kwargs(
            [{"role": "user", "content": "hello"}],
            "arn:runtime",
            "session-id",
            "user-id",
        )

        self.assertEqual(kwargs["agentRuntimeArn"], "arn:runtime")
        self.assertEqual(kwargs["runtimeSessionId"], "session-id")
        self.assertEqual(kwargs["runtimeUserId"], "user-id")
        self.assertEqual(kwargs["accept"], "text/event-stream")
        self.assertEqual(
            json.loads(kwargs["payload"]),
            {
                "messages": [{"role": "user", "content": "hello"}],
                "chat_id": "session-id",
                "model_item": {"info": {"user_id": "user-id"}},
            },
        )

    def test_runtime_stream_extracts_openai_content_deltas(self):
        client = FakeRuntimeClient(
            [
                b": keepalive",
                b'data: {"choices":[{"delta":{"content":"Hello"}}]}',
                b'data: {"choices":[{"delta":{"content":" world"}}]}',
                b"data: [DONE]",
            ]
        )

        with patch.object(
            dify_server,
            "get_agentcore_client",
            return_value=client,
        ):
            text = "".join(
                dify_server._stream_runtime_events(
                    [{"role": "user", "content": "hello"}],
                    "arn:runtime",
                    "session-id",
                    "user-id",
                )
            )

        self.assertEqual(text, "Hello world")
        self.assertEqual(len(client.calls), 1)

    def test_runtime_stream_extracts_sanitized_agent_status_events(self):
        client = FakeRuntimeClient(
            [
                b'data: {"event":"agent_step","step":{"type":"skill",'
                b'"name":"admission-analysis","status":"started"}}',
                b'data: {"event":"agent_step","step":{"type":"tool",'
                b'"name":"NUH: query data<script>","status":"completed"}}',
                b'data: {"choices":[{"delta":{"content":"Answer"}}]}',
                b"data: [DONE]",
            ]
        )

        with patch.object(
            dify_server,
            "get_agentcore_client",
            return_value=client,
        ):
            events = list(
                dify_server._stream_runtime_events(
                    [{"role": "user", "content": "hello"}],
                    "arn:runtime",
                    "session-id",
                    "user-id",
                )
            )

        self.assertIsInstance(events[0], dify_server._RuntimeStatus)
        self.assertEqual(events[0].kind, "skill")
        self.assertEqual(events[0].name, "admission-analysis")
        self.assertEqual(events[1].name, "NUH: query datascript")
        self.assertEqual(events[2], "Answer")

    def test_artifact_stream_renders_runtime_status_as_visible_markdown(self):
        events = iter(
            [
                dify_server._RuntimeStatus("skill", "admission-analysis", "started"),
                dify_server._RuntimeStatus("tool", "NUH: query data", "completed"),
                "Answer",
            ]
        )

        with patch.object(dify_server, "_resolve_artifacts", return_value=[]):
            response = "".join(
                dify_server._sse_artifact_stream(
                    events,
                    "runtime",
                    "session-id",
                    "dev",
                    "chatcmpl-test",
                    ("user-id", "session-id", {}),
                )
            )

        self.assertIn("**Skill:** `admission-analysis`", response)
        self.assertIn("**Tool:** `NUH: query data`", response)
        self.assertIn("Answer", response)

    def test_non_streaming_completion_dispatches_to_runtime(self):
        messages = [{"role": "user", "content": "hello"}]
        with (
            patch.object(
                dify_server,
                "_invoke_runtime_buffered",
                return_value="runtime answer",
            ) as invoke_runtime,
            patch.object(
                dify_server,
                "_render_buffered_result",
                return_value="runtime answer with artifacts",
            ) as render_result,
        ):
            response = asyncio.run(
                dify_server._build_completion(
                    messages=messages,
                    backend_type="runtime",
                    backend_arn="arn:runtime",
                    slug="dev",
                    model="dev",
                    stream=False,
                    session_id="session-id",
                    user_id="user-id",
                )
            )

        invoke_runtime.assert_called_once_with(
            messages,
            "arn:runtime",
            "session-id",
            "user-id",
        )
        render_result.assert_called_once()
        self.assertEqual(
            response["choices"][0]["message"]["content"],
            "runtime answer with artifacts",
        )

    def test_runtime_chat_adds_artifact_prompt(self):
        user_id = "832757e8-7a25-4e75-8401-8b4a51bfe638"
        body = {
            "user": user_id,
            "messages": [{"role": "user", "content": "hello"}],
        }
        completion = {"id": "chatcmpl-test"}

        with (
            patch.object(
                dify_server,
                "get_dify_backend",
                return_value=("runtime", "arn:runtime"),
            ),
            patch.object(
                dify_server,
                "_build_completion",
                new=AsyncMock(return_value=completion),
            ) as build_completion,
        ):
            response = asyncio.run(
                dify_server.chat_completions_by_slug("dev", FakeRequest(body))
            )

        self.assertEqual(response, completion)
        call = build_completion.await_args.kwargs
        self.assertEqual(call["backend_type"], "runtime")
        self.assertEqual(call["backend_arn"], "arn:runtime")
        self.assertEqual(call["messages"][:-1], body["messages"])
        self.assertEqual(call["messages"][-1]["role"], "system")
        self.assertIn("Generated Office files", call["messages"][-1]["content"])
        self.assertFalse(call["output_urls"])

    def test_runtime_chat_passes_output_url_setting(self):
        conversation_id = "f48bbf06-2c37-4e5a-90ac-12a3be6d8fe1"
        body = {
            "user": "832757e8-7a25-4e75-8401-8b4a51bfe638",
            "messages": [
                {
                    "role": "assistant",
                    "content": (
                        f"<C_ID>{conversation_id}<C_ID>\n"
                        "<output_url>True</output_url>"
                    ),
                },
                {"role": "user", "content": "Create a workbook"},
            ],
        }

        with (
            patch.object(
                dify_server,
                "get_dify_backend",
                return_value=("runtime", "arn:runtime"),
            ),
            patch.object(
                dify_server,
                "_build_completion",
                new=AsyncMock(return_value={"id": "chatcmpl-test"}),
            ) as build_completion,
        ):
            asyncio.run(
                dify_server.chat_completions_by_slug("dev", FakeRequest(body))
            )

        call = build_completion.await_args.kwargs
        self.assertTrue(call["output_urls"])
        self.assertNotIn("<output_url>", json.dumps(call["messages"]))

    def test_assistant_output_url_flag_is_removed_and_enabled(self):
        conversation_id = "f48bbf06-2c37-4e5a-90ac-12a3be6d8fe1"
        body = {
            "user": "user-id",
            "messages": [
                {
                    "role": "assistant",
                    "content": (
                        f"<C_ID>{conversation_id}<C_ID>\n"
                        "<output_url>True<output_url>"
                    ),
                },
                {"role": "user", "content": "Create a workbook"},
            ],
        }

        session_id, _, messages, output_urls = (
            dify_server._extract_dify_session_context(body)
        )

        self.assertEqual(session_id, conversation_id)
        self.assertTrue(output_urls)
        self.assertEqual(
            messages,
            [{"role": "user", "content": "Create a workbook"}],
        )

    def test_buffered_artifact_uses_presigned_url_when_enabled(self):
        artifacts = [
            {
                "filename": "report.xlsx",
                "s3_uri": "s3://bucket/path/report.xlsx",
                "mime_type": (
                    "application/vnd.openxmlformats-officedocument."
                    "spreadsheetml.sheet"
                ),
                "size": 120,
            }
        ]
        s3 = FakePresignS3()

        with (
            patch.object(dify_server, "_resolve_artifacts", return_value=artifacts),
            patch.object(dify_server, "get_s3_client", return_value=s3),
        ):
            content = dify_server._render_buffered_result(
                "Workbook created.",
                ("user-id", "session-id", {}),
                time.time(),
                output_urls=True,
            )

        self.assertIn("https://downloads.example/report.xlsx?signature=test", content)
        self.assertIn("Links expire in 60 minutes.", content)
        self.assertNotIn("<agentcore-generated-files>", content)
        self.assertEqual(s3.calls[0][0], "get_object")
        self.assertEqual(s3.calls[0][1]["ExpiresIn"], 3600)

    def test_streaming_artifact_uses_presigned_url_when_enabled(self):
        artifacts = [
            {
                "filename": "report.xlsx",
                "s3_uri": "s3://bucket/path/report.xlsx",
                "mime_type": "application/octet-stream",
                "size": 120,
            }
        ]
        s3 = FakePresignS3()

        with (
            patch.object(dify_server, "_resolve_artifacts", return_value=artifacts),
            patch.object(dify_server, "get_s3_client", return_value=s3),
        ):
            response = "".join(
                dify_server._sse_artifact_stream(
                    iter(["Workbook created."]),
                    "runtime",
                    "session-id",
                    "dev",
                    "chatcmpl-test",
                    ("user-id", "session-id", {}),
                    output_urls=True,
                )
            )

        self.assertIn("https://downloads.example/report.xlsx?signature=test", response)
        self.assertNotIn("<agentcore-generated-files>", response)

    def test_artifact_stream_sanitizes_runtime_output(self):
        events = iter(
            [
                "Answer",
                '<agentcore-artifacts>[{"filename":"report.xlsx",',
                '"s3_uri":"s3://bucket/report.xlsx"}]</agentcore-artifacts>',
            ]
        )
        artifacts = [
            {
                "filename": "report.xlsx",
                "s3_uri": "s3://bucket/report.xlsx",
            }
        ]

        with patch.object(
            dify_server,
            "_resolve_artifacts",
            return_value=artifacts,
        ):
            response = "".join(
                dify_server._sse_artifact_stream(
                    events,
                    "runtime",
                    "session-id",
                    "dev",
                    "chatcmpl-test",
                    ("user-id", "session-id", {}),
                )
            )

        self.assertIn("Answer", response)
        self.assertNotIn("<agentcore-artifacts>", response)
        self.assertIn("<agentcore-generated-files>", response)


if __name__ == "__main__":
    unittest.main()

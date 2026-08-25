import importlib.util
import io
import json
import pathlib
import sys
import unittest
from datetime import datetime, timezone


PROXY_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROXY_DIR))
SPEC = importlib.util.spec_from_file_location(
    "dify_server",
    PROXY_DIR / "dify-server.py",
)
DIFY_SERVER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DIFY_SERVER)


DIRECT_HTML = "<!DOCTYPE html><html><body>direct model</body></html>"
S3_HTML = "<!DOCTYPE html><html><body>validated s3</body></html>"


def fenced(document):
    return f"```html\n{document}\n```"


def artifact_marker(key, filename="dashboard.html"):
    return (
        "<agentcore-artifacts>"
        + json.dumps(
            [
                {
                    "s3_uri": f"s3://ah-dify/{key}",
                    "filename": filename,
                }
            ]
        )
        + "</agentcore-artifacts>"
    )


class FakeS3Client:
    def __init__(self, key, body=S3_HTML.encode("utf-8")):
        self.key = key
        self.body = body

    def list_objects_v2(self, **_kwargs):
        return {
            "Contents": [
                {
                    "Key": self.key,
                    "Size": len(self.body),
                    "LastModified": datetime.now(timezone.utc),
                }
            ]
        }

    def get_object_tagging(self, **_kwargs):
        return {
            "TagSet": [
                {"Key": "Dify-User-Id", "Value": "user-1"},
                {"Key": "Dify-Conversation-Id", "Value": "conversation-1"},
                {"Key": "AgentCore-Artifact", "Value": "generated"},
            ]
        }

    def get_object(self, **_kwargs):
        return {
            "Body": io.BytesIO(self.body),
            "ContentLength": len(self.body),
        }


class EmptyS3Client:
    def list_objects_v2(self, **_kwargs):
        return {"Contents": []}


class DifyHtmlArtifactTests(unittest.TestCase):
    def setUp(self):
        self.original_s3_client = DIFY_SERVER._s3_client

    def tearDown(self):
        DIFY_SERVER._s3_client = self.original_s3_client

    def test_html_is_an_allowed_generated_artifact(self):
        self.assertIn(
            "html",
            DIFY_SERVER.DIFY_OFFICE_SOURCE_PROFILE["output_extensions"],
        )

    def test_injected_guidance_requires_uploaded_self_contained_html(self):
        instruction = DIFY_SERVER._inject_dify_artifact_context(
            [],
            "user-1",
            "conversation-1",
            DIFY_SERVER.DIFY_OFFICE_SOURCE_PROFILE,
        )[-1]["content"]

        self.assertIn("HTML output must be a complete document", instruction)
        self.assertIn("Chart.js may be loaded from a standard CDN", instruction)
        self.assertNotIn("Do not use CDN scripts", instruction)
        self.assertIn("`cat` to return the file", instruction)
        self.assertIn("aws s3api put-object", instruction)
        self.assertIn("<agentcore-artifacts>", instruction)
        self.assertIn("Direct model-generated HTML will be rejected", instruction)
        self.assertNotIn("For html, just return", instruction)

    def test_owned_html_object_passes_artifact_validation(self):
        key = "harness_dev/user-1/conversation-1/dashboard.html"
        DIFY_SERVER._s3_client = FakeS3Client(key)

        validated = DIFY_SERVER._validate_dify_artifacts(
            [
                {
                    "s3_uri": f"s3://ah-dify/{key}",
                    "filename": "dashboard.html",
                }
            ],
            "user-1",
            "conversation-1",
            DIFY_SERVER.DIFY_OFFICE_SOURCE_PROFILE,
        )

        self.assertEqual(validated[0]["filename"], "dashboard.html")
        self.assertEqual(validated[0]["mime_type"], "text/html")

    def test_buffered_response_emits_complete_validated_s3_html(self):
        key = "harness_dev/user-1/conversation-1/dashboard.html"
        DIFY_SERVER._s3_client = FakeS3Client(key)

        rendered = DIFY_SERVER._render_buffered_result(
            artifact_marker(key),
            (
                "user-1",
                "conversation-1",
                DIFY_SERVER.DIFY_OFFICE_SOURCE_PROFILE,
            ),
            0,
        )

        self.assertEqual(rendered, fenced(S3_HTML))

    def test_validated_s3_html_replaces_direct_model_html(self):
        key = "harness_dev/user-1/conversation-1/dashboard.html"
        DIFY_SERVER._s3_client = FakeS3Client(key)

        rendered = DIFY_SERVER._render_buffered_result(
            fenced(DIRECT_HTML) + artifact_marker(key),
            (
                "user-1",
                "conversation-1",
                DIFY_SERVER.DIFY_OFFICE_SOURCE_PROFILE,
            ),
            0,
        )

        self.assertEqual(rendered, fenced(S3_HTML))
        self.assertNotIn("direct model", rendered)

    def test_direct_model_html_is_rejected_when_no_s3_artifact_exists(self):
        DIFY_SERVER._s3_client = EmptyS3Client()
        direct = fenced(DIRECT_HTML)

        rendered = DIFY_SERVER._render_buffered_result(
            direct,
            (
                "user-1",
                "conversation-1",
                DIFY_SERVER.DIFY_OFFICE_SOURCE_PROFILE,
            ),
            0,
        )

        self.assertEqual(rendered, DIFY_SERVER._DIRECT_HTML_REJECTED_TEXT)
        self.assertNotIn("direct model", rendered)

    def test_incomplete_direct_model_html_is_also_suppressed(self):
        DIFY_SERVER._s3_client = EmptyS3Client()

        rendered = DIFY_SERVER._render_buffered_result(
            "```html\n<!DOCTYPE html><html><body>truncated",
            (
                "user-1",
                "conversation-1",
                DIFY_SERVER.DIFY_OFFICE_SOURCE_PROFILE,
            ),
            0,
        )

        self.assertEqual(rendered, DIFY_SERVER._DIRECT_HTML_REJECTED_TEXT)
        self.assertNotIn("truncated", rendered)

    def test_stream_reassembles_to_only_validated_s3_html(self):
        key = "harness_dev/user-1/conversation-1/dashboard.html"
        DIFY_SERVER._s3_client = FakeS3Client(key)
        direct = fenced(DIRECT_HTML)
        events = [direct[:4], direct[4:] + artifact_marker(key)]

        chunks = list(
            DIFY_SERVER._sse_artifact_stream(
                events,
                "runtime",
                "conversation-1",
                "model",
                "completion-1",
                (
                    "user-1",
                    "conversation-1",
                    DIFY_SERVER.DIFY_OFFICE_SOURCE_PROFILE,
                ),
            )
        )
        content = ""
        for chunk in chunks:
            if not chunk.startswith("data: {"):
                continue
            payload = json.loads(chunk[len("data: ") :])
            choices = payload.get("choices", [])
            if choices:
                content += choices[0].get("delta", {}).get("content", "")

        self.assertEqual(content, fenced(S3_HTML))
        self.assertNotIn("direct model", content)

    def test_invalid_s3_html_does_not_fall_back_to_direct_model_html(self):
        key = "harness_dev/user-1/conversation-1/dashboard.html"
        DIFY_SERVER._s3_client = FakeS3Client(
            key,
            b"<html><body>missing doctype</body></html>",
        )

        rendered = DIFY_SERVER._render_buffered_result(
            fenced(DIRECT_HTML) + artifact_marker(key),
            (
                "user-1",
                "conversation-1",
                DIFY_SERVER.DIFY_OFFICE_SOURCE_PROFILE,
            ),
            0,
        )

        self.assertEqual(rendered, DIFY_SERVER._DIRECT_HTML_REJECTED_TEXT)
        self.assertNotIn("direct model", rendered)

    def test_stream_splits_large_html_into_bounded_content_chunks(self):
        key = "harness_dev/user-1/conversation-1/dashboard.html"
        large_html = (
            "<!DOCTYPE html><html><body>"
            + ("validated data " * 200)
            + "</body></html>"
        )
        DIFY_SERVER._s3_client = FakeS3Client(key, large_html.encode("utf-8"))
        original_chunk_chars = DIFY_SERVER.DIFY_RESPONSE_CHUNK_CHARS
        DIFY_SERVER.DIFY_RESPONSE_CHUNK_CHARS = 64
        try:
            chunks = list(
                DIFY_SERVER._sse_artifact_stream(
                    [artifact_marker(key)],
                    "runtime",
                    "conversation-1",
                    "model",
                    "completion-1",
                    (
                        "user-1",
                        "conversation-1",
                        DIFY_SERVER.DIFY_OFFICE_SOURCE_PROFILE,
                    ),
                )
            )
        finally:
            DIFY_SERVER.DIFY_RESPONSE_CHUNK_CHARS = original_chunk_chars

        content_chunks = []
        for chunk in chunks:
            if not chunk.startswith("data: {"):
                continue
            payload = json.loads(chunk[len("data: ") :])
            choices = payload.get("choices", [])
            if not choices:
                continue
            content = choices[0].get("delta", {}).get("content")
            if content:
                content_chunks.append(content)

        self.assertGreater(len(content_chunks), 1)
        self.assertTrue(all(len(chunk) <= 64 for chunk in content_chunks))
        self.assertEqual("".join(content_chunks), fenced(large_html))

    def test_non_html_artifact_keeps_existing_reference_contract(self):
        key = "harness_dev/user-1/conversation-1/report.xlsx"
        DIFY_SERVER._s3_client = FakeS3Client(key, b"xlsx-placeholder")

        rendered = DIFY_SERVER._render_buffered_result(
            artifact_marker(key, "report.xlsx"),
            (
                "user-1",
                "conversation-1",
                DIFY_SERVER.DIFY_OFFICE_SOURCE_PROFILE,
            ),
            0,
        )

        self.assertIn("<agentcore-generated-files>", rendered)
        self.assertIn('"filename":"report.xlsx"', rendered)
        self.assertNotIn("```html", rendered)

    def test_normal_streaming_text_is_unchanged(self):
        DIFY_SERVER._s3_client = EmptyS3Client()
        chunks = list(
            DIFY_SERVER._sse_artifact_stream(
                ["hello", " world"],
                "runtime",
                "conversation-1",
                "model",
                "completion-1",
                (
                    "user-1",
                    "conversation-1",
                    DIFY_SERVER.DIFY_OFFICE_SOURCE_PROFILE,
                ),
            )
        )
        content = ""
        for chunk in chunks:
            if not chunk.startswith("data: {"):
                continue
            payload = json.loads(chunk[len("data: ") :])
            choices = payload.get("choices", [])
            if choices:
                content += choices[0].get("delta", {}).get("content", "")

        self.assertEqual(content, "hello world")


if __name__ == "__main__":
    unittest.main()

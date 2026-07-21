import sys
import types

import pytest

# The local development environment does not need the container-only Claude SDK
# to test document input parsing and validation.
sdk = types.ModuleType("claude_agent_sdk")
sdk.ClaudeAgentOptions = type("ClaudeAgentOptions", (), {})
sdk.ResultMessage = type("ResultMessage", (), {})
sdk.query = lambda **kwargs: None
sdk_types = types.ModuleType("claude_agent_sdk.types")
sdk_types.AssistantMessage = type("AssistantMessage", (), {})
sdk_types.StreamEvent = type("StreamEvent", (), {})
sdk_types.TextBlock = type("TextBlock", (), {})
sys.modules.setdefault("claude_agent_sdk", sdk)
sys.modules.setdefault("claude_agent_sdk.types", sdk_types)
gateway_proxy = types.ModuleType("app.gateway_proxy")
gateway_proxy.mcp_urls = lambda: {}
memory = types.ModuleType("app.memory")
memory.retrieve_context = lambda *args: ""
memory.save_turn = lambda *args: None
sys.modules.setdefault("app.gateway_proxy", gateway_proxy)
sys.modules.setdefault("app.memory", memory)

from app import agent
from app.agent import (
    _extract_tagged_documents,
    _redact_document_references,
    _validate_document_url,
)


def _messages(payload: str) -> list[dict]:
    return [{
        "role": "system",
        "content": f"Compare the documents.\n<DOCUMENT_INPUT>{payload}</DOCUMENT_INPUT>",
    }]


def test_extracts_tagged_json_document_input():
    messages = _messages("""{
      "documents": [
        {"name": "version_1", "url": "https://example.com/v1.docx?sign=one"},
        {"name": "version_2", "url": "https://example.com/v2.docx?sign=two"}
      ]
    }""")

    assert _extract_tagged_documents(messages) == [
        {"name": "version_1", "url": "https://example.com/v1.docx?sign=one"},
        {"name": "version_2", "url": "https://example.com/v2.docx?sign=two"},
    ]
    sanitized = _redact_document_references(messages)[0]["content"]
    assert "Compare the documents." in sanitized
    assert "sign=one" not in sanitized
    assert "sign=two" not in sanitized


def test_document_input_is_optional():
    messages = [
        {"role": "system", "content": "You are a coding assistant."},
        {"role": "user", "content": "Explain this function."},
    ]
    assert _extract_tagged_documents(messages) == []
    assert _redact_document_references(messages) == messages


def test_accepts_one_document():
    payload = '{"documents": [{"name": "report", "url": "https://example.com/report.xlsx"}]}'
    assert _extract_tagged_documents(_messages(payload)) == [
        {"name": "report", "url": "https://example.com/report.xlsx"}
    ]


def test_extracts_line_oriented_s3_document_input():
    messages = _messages(
        """Name: Clean_RLS.pdf, S3_URI: s3://ah-dify/upload_files/user/clean.pdf
Name: Signoff_RLS.pdf, S3_URI: s3://ah-dify/upload_files/user/signoff.pdf"""
    )

    assert _extract_tagged_documents(messages) == [
        {
            "name": "Clean_RLS.pdf",
            "url": "s3://ah-dify/upload_files/user/clean.pdf",
        },
        {
            "name": "Signoff_RLS.pdf",
            "url": "s3://ah-dify/upload_files/user/signoff.pdf",
        },
    ]


def test_accepts_json_s3_uri_field():
    payload = (
        '{"documents": [{"name": "report", '
        '"S3_URI": "s3://ah-dify/upload_files/user/report.pdf"}]}'
    )
    assert _extract_tagged_documents(_messages(payload)) == [
        {
            "name": "report",
            "url": "s3://ah-dify/upload_files/user/report.pdf",
        }
    ]


def test_downloads_s3_document_with_runtime_credentials(monkeypatch, tmp_path):
    class FakeBody:
        def __init__(self):
            self.closed = False

        def iter_chunks(self, chunk_size):
            assert chunk_size == 64 * 1024
            yield b"%PDF-test"

        def close(self):
            self.closed = True

    body = FakeBody()

    class FakeS3:
        def get_object(self, **kwargs):
            assert kwargs == {
                "Bucket": "ah-dify",
                "Key": "upload_files/user/report.pdf",
            }
            return {
                "Body": body,
                "ContentLength": 9,
                "ContentType": "application/pdf",
            }

    monkeypatch.setattr(agent, "_s3_client", FakeS3())
    local_path = agent._download_document(
        "s3://ah-dify/upload_files/user/report.pdf", str(tmp_path), 1
    )

    assert local_path.endswith("document_001.pdf")
    assert (tmp_path / "document_001.pdf").read_bytes() == b"%PDF-test"
    assert body.closed


def test_rejects_s3_document_outside_configured_location():
    payload = "Name: secret, S3_URI: s3://another-bucket/upload_files/secret.pdf"
    with pytest.raises(ValueError, match="must be under"):
        _extract_tagged_documents(_messages(payload))


@pytest.mark.parametrize(
    "payload,error",
    [
        ('{"documents": []}', "at least one"),
        ('{"documents": {}}', "must be an array"),
        ('{"documents": [}', "invalid JSON"),
        (
            '{"documents": ['
            '{"name": "same", "url": "https://example.com/a"},'
            '{"name": "same", "url": "https://example.com/b"}]}',
            "Duplicate document name",
        ),
    ],
)
def test_rejects_invalid_document_input(payload, error):
    with pytest.raises(ValueError, match=error):
        _extract_tagged_documents(_messages(payload))


@pytest.mark.parametrize(
    "url",
    [
        "http://internal.example:8080/document.docx",
        "https://user:password@sharepoint.example.com/shared/document.docx",
        "https://example.com/any/path",
    ],
)
def test_accepts_http_document_urls(url):
    _validate_document_url(url)


@pytest.mark.parametrize("url", ["ftp://example.com/a.pdf", "not-a-url"])
def test_rejects_non_http_document_urls(url):
    with pytest.raises(ValueError):
        _validate_document_url(url)

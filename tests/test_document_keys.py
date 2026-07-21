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

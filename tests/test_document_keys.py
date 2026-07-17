import sys
import types

import pytest

# The local development environment does not need the container-only Claude SDK
# to test the pure S3-key parsing and validation helpers.
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

from app.agent import _extract_document_keys, _validate_document_key


def test_extracts_dify_prompt_variables():
    messages = [{
        "role": "user",
        "content": (
            "Compare the two documents.\n"
            "VERSION_1_S3_KEY: document-review/user-123/v1.pdf\n"
            "VERSION_2_S3_KEY: document-review/user-123/v2.pdf\n"
        ),
    }]

    assert _extract_document_keys(messages) == {
        "1": "document-review/user-123/v1.pdf",
        "2": "document-review/user-123/v2.pdf",
    }


def test_structured_inputs_are_supported():
    assert _extract_document_keys([], {
        "version_1_key": "document-review/user-123/v1.pdf",
        "version_2_key": "document-review/user-123/v2.pdf",
    }) == {
        "1": "document-review/user-123/v1.pdf",
        "2": "document-review/user-123/v2.pdf",
    }


@pytest.mark.parametrize("key", ["s3://bucket/a.pdf", "../a.pdf", "/a.pdf", "a\\b.pdf"])
def test_rejects_unsafe_or_non_key_references(key):
    with pytest.raises(ValueError):
        _validate_document_key(key)

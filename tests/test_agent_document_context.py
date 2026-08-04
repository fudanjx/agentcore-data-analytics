import asyncio
import sys
import types


class FakeClaudeAgentOptions:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


# The local test environment does not need the container-only Claude SDK.
sdk = types.ModuleType("claude_agent_sdk")
sdk.ClaudeAgentOptions = FakeClaudeAgentOptions
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


def test_document_input_reaches_agent_unchanged(monkeypatch):
    document_input = (
        "<DOCUMENT_INPUT>"
        "Name: report.xlsx, S3_URI: s3://ah-dify/upload_files/user/report.xlsx"
        "</DOCUMENT_INPUT>"
    )
    captured = {}

    async def fake_query(*, prompt, options):
        captured["prompt"] = prompt
        captured["options"] = options
        if False:
            yield None

    monkeypatch.setattr(agent, "query", fake_query)

    async def invoke():
        return [
            text
            async for text in agent.stream(
                [
                    {"role": "system", "content": document_input},
                    {"role": "user", "content": "Analyze the uploaded file."},
                ]
            )
        ]

    assert asyncio.run(invoke()) == []
    assert document_input in captured["options"].system_prompt
    assert captured["prompt"] == "Analyze the uploaded file."
    assert "downloaded by application" not in captured["options"].system_prompt
    assert not hasattr(agent, "_document_payload_from_s3_uris")
    assert not hasattr(agent, "_download_document")

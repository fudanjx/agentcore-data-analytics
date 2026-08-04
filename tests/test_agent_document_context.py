import asyncio
import sys
import types


class FakeClaudeAgentOptions:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def fake_tool(*_args, **_kwargs):
    def decorate(function):
        return function

    return decorate


# The local test environment does not need the container-only Claude SDK.
sdk = types.ModuleType("claude_agent_sdk")
sdk.ClaudeAgentOptions = FakeClaudeAgentOptions
sdk.ClaudeSDKClient = type("ClaudeSDKClient", (), {})
sdk.ResultMessage = type("ResultMessage", (), {})
sdk.create_sdk_mcp_server = lambda **kwargs: kwargs
sdk.tool = fake_tool
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
    stopped_sessions = []

    class FakeClaudeSDKClient:
        def __init__(self, *, options):
            captured["options"] = options

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def query(self, prompt):
            captured["prompt"] = prompt

        async def receive_response(self):
            if False:
                yield None

    async def fake_start_session(runtime_session_id):
        captured["runtime_session_id"] = runtime_session_id
        return "code-interpreter-session-id"

    async def fake_stop_session(code_interpreter_session_id):
        stopped_sessions.append(code_interpreter_session_id)

    monkeypatch.setattr(agent, "ClaudeSDKClient", FakeClaudeSDKClient)
    monkeypatch.setattr(
        agent.code_interpreter,
        "start_session",
        fake_start_session,
    )
    monkeypatch.setattr(
        agent.code_interpreter,
        "stop_session",
        fake_stop_session,
    )
    monkeypatch.setattr(
        agent.code_interpreter,
        "build_mcp_server",
        lambda session_id: {"session_id": session_id},
    )

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
    assert captured["runtime_session_id"] is None
    assert captured["options"].mcp_servers["code_interpreter"] == {
        "session_id": "code-interpreter-session-id"
    }
    assert "mcp__code_interpreter__execute_code" in captured["options"].allowed_tools
    assert stopped_sessions == ["code-interpreter-session-id"]
    assert "downloaded by application" not in captured["options"].system_prompt
    assert not hasattr(agent, "_document_payload_from_s3_uris")
    assert not hasattr(agent, "_download_document")

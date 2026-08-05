import asyncio
import json
import sys
import types


def fake_tool(*_args, **_kwargs):
    def decorate(function):
        return function

    return decorate


# The local test environment does not need the container-only Claude SDK.
sdk = sys.modules.setdefault("claude_agent_sdk", types.ModuleType("claude_agent_sdk"))
if not hasattr(sdk, "tool"):
    sdk.tool = fake_tool
if not hasattr(sdk, "create_sdk_mcp_server"):
    sdk.create_sdk_mcp_server = lambda **kwargs: kwargs

from app import code_interpreter


class FakeAgentCoreClient:
    def __init__(self):
        self.started = []
        self.invoked = []
        self.stopped = []

    def start_code_interpreter_session(self, **kwargs):
        self.started.append(kwargs)
        return {"sessionId": "managed-code-session-id"}

    def invoke_code_interpreter(self, **kwargs):
        self.invoked.append(kwargs)
        return {
            "stream": iter(
                [
                    {
                        "result": {
                            "content": [{"type": "text", "text": "42"}],
                            "structuredContent": {"exitCode": 0},
                            "isError": False,
                        }
                    },
                    {"result": {"content": [{"type": "image", "data": b"png"}]}},
                ]
            )
        }

    def stop_code_interpreter_session(self, **kwargs):
        self.stopped.append(kwargs)
        return {"sessionId": kwargs["sessionId"]}


def test_starts_invokes_and_stops_managed_session(monkeypatch):
    client = FakeAgentCoreClient()
    monkeypatch.setattr(code_interpreter, "_client", client)
    monkeypatch.setattr(code_interpreter, "CODE_INTERPRETER_ID", "interpreter-id")

    session_id = asyncio.run(code_interpreter.start_session("runtime-session"))
    rendered = code_interpreter._invoke_and_collect(
        session_id,
        "executeCode",
        {"code": "print(42)", "language": "python"},
    )
    asyncio.run(code_interpreter.stop_session(session_id))

    assert session_id == "managed-code-session-id"
    assert client.started == [
        {
            "codeInterpreterIdentifier": "interpreter-id",
            "name": "runtime-runtime-session",
            "sessionTimeoutSeconds": code_interpreter.SESSION_TIMEOUT_SECONDS,
        }
    ]
    assert client.invoked == [
        {
            "codeInterpreterIdentifier": "interpreter-id",
            "sessionId": "managed-code-session-id",
            "name": "executeCode",
            "arguments": {"code": "print(42)", "language": "python"},
        }
    ]
    assert client.stopped == [
        {
            "codeInterpreterIdentifier": "interpreter-id",
            "sessionId": "managed-code-session-id",
        }
    ]

    results = json.loads(rendered)
    assert results[0]["content"][0]["text"] == "42"
    assert results[1]["content"][0]["data"] == {"binaryBytes": 3}


def test_tool_failure_is_returned_to_agent(monkeypatch):
    def fail(*_args, **_kwargs):
        raise RuntimeError("access denied")

    monkeypatch.setattr(code_interpreter, "_invoke_and_collect", fail)
    result = asyncio.run(
        code_interpreter._invoke_tool(
            "managed-code-session-id",
            "executeCommand",
            {"command": "aws s3 ls"},
        )
    )

    assert "access denied" in result["content"][0]["text"]
    assert result["is_error"] is True


def test_code_interpreter_result_error_is_returned_to_agent(monkeypatch):
    monkeypatch.setattr(
        code_interpreter,
        "_invoke_and_collect",
        lambda *_args, **_kwargs: '[{"isError":true,"content":[]}]',
    )

    result = asyncio.run(
        code_interpreter._invoke_tool(
            "managed-code-session-id",
            "executeCode",
            {"code": "raise RuntimeError()", "language": "python"},
        )
    )

    assert result["is_error"] is True

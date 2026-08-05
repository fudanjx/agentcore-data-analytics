import asyncio
import json

from app import agent
from app import main as runtime_main


def test_runtime_encodes_agent_steps_as_sideband_sse(monkeypatch):
    async def fake_agent_stream(*_args, **_kwargs):
        yield agent.AgentStep("skill", "admission-analysis", "started")
        yield agent.AgentStep("tool", "NUH: query data", "completed")
        yield "Final answer"

    monkeypatch.setattr(runtime_main.agent, "stream", fake_agent_stream)

    async def invoke():
        return [
            event
            async for event in runtime_main._sse_stream(
                [{"role": "user", "content": "hello"}],
                "dev",
                "actor-id",
                "session-id",
            )
        ]

    events = asyncio.run(invoke())
    payloads = [
        json.loads(event.removeprefix("data: ").strip())
        for event in events
        if event != "data: [DONE]\n\n"
    ]

    assert payloads[0] == {
        "event": "agent_step",
        "step": {
            "type": "skill",
            "name": "admission-analysis",
            "status": "started",
        },
    }
    assert payloads[1]["step"] == {
        "type": "tool",
        "name": "NUH: query data",
        "status": "completed",
    }
    assert payloads[2]["choices"][0]["delta"]["content"] == "Final answer"
    assert payloads[-1]["choices"][0]["finish_reason"] == "stop"

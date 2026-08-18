import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).parents[1] / "Strands-runtime" / "agent.py"
DOCUMENT_GUIDANCE = (
    "When <document_input> tags are present:\n"
    "Each <document_input> provides the uploaded file’s original filename and S3 "
    "URL. Use Code Interpreter to download these files"
)


def _load_agent_module(
    monkeypatch,
    prompt_cache_ttl=None,
    skills_enabled=True,
    model_id="test-model-id",
    agent_name=None,
    agent_description=None,
):
    for name in (
        "ENABLE_TOOL_DETAILS",
        "ENABLE_MODEL_USAGE_LOGS",
        "MODEL_PRICING_LABEL",
        "MODEL_INPUT_PRICE_PER_MTOK_USD",
        "MODEL_OUTPUT_PRICE_PER_MTOK_USD",
        "MODEL_CACHE_READ_PRICE_PER_MTOK_USD",
        "MODEL_CACHE_WRITE_5M_PRICE_PER_MTOK_USD",
        "MODEL_CACHE_WRITE_1H_PRICE_PER_MTOK_USD",
    ):
        monkeypatch.delenv(name, raising=False)
    if prompt_cache_ttl is None:
        monkeypatch.delenv("PROMPT_CACHE_TTL", raising=False)
    else:
        monkeypatch.setenv("PROMPT_CACHE_TTL", prompt_cache_ttl)
    monkeypatch.delenv("MODEL_ARN", raising=False)
    if model_id is None:
        monkeypatch.delenv("MODEL_ID", raising=False)
    else:
        monkeypatch.setenv("MODEL_ID", model_id)
    for name, value in {
        "AGENT_NAME": agent_name,
        "AGENT_DESCRIPTION": agent_description,
    }.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)

    code_interpreter = types.ModuleType("code_interpreter")
    code_interpreter.CODE_INTERPRETER_ID = ""

    gateway_proxy = types.ModuleType("gateway_proxy")
    gateway_proxy.GATEWAY_CONFIGS = {}
    gateway_proxy.mcp_label = lambda slug: slug

    memory = types.ModuleType("memory")
    memory.MEMORY_GUIDANCE = "\nMEMORY_GUIDANCE"
    memory.memory_enabled = lambda: True

    skills_sync = types.ModuleType("skills_sync")
    skills_sync.ACTIVATION_GUIDANCE = "\nACTIVATE_SKILLS"
    skills_sync.LOCAL_DIR = "skills"
    skills_sync.read_skill_resource = lambda: None
    skills_sync.skill_resource_s3_uri = lambda *_args: "s3://bucket/skills/resource"
    skills_sync.skills_enabled = lambda: skills_enabled

    system_prompt = types.ModuleType("system_prompt")
    system_prompt.load = lambda: "BASE_SYSTEM"

    strands = types.ModuleType("strands")
    strands.Agent = object
    strands.AgentSkills = object

    callback_handler = types.ModuleType("strands.handlers.callback_handler")
    callback_handler.null_callback_handler = lambda *_args, **_kwargs: None

    class FakeCacheConfig:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    models = types.ModuleType("strands.models")
    models.BedrockModel = object
    models.CacheConfig = FakeCacheConfig
    models.CacheToolsConfig = FakeCacheConfig

    mcp = types.ModuleType("strands.tools.mcp")
    mcp.MCPClient = object

    for name, module in {
        "code_interpreter": code_interpreter,
        "gateway_proxy": gateway_proxy,
        "memory": memory,
        "skills_sync": skills_sync,
        "system_prompt": system_prompt,
        "strands": strands,
        "strands.handlers.callback_handler": callback_handler,
        "strands.models": models,
        "strands.tools.mcp": mcp,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)

    spec = importlib.util.spec_from_file_location(
        "strands_runtime_agent_memory", MODULE_PATH
    )
    agent = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(agent)
    return agent, memory


def test_prepare_uses_native_session_manager_and_only_current_user_turn(monkeypatch):
    agent, memory = _load_agent_module(
        monkeypatch,
        agent_name="gmio-pcr",
        agent_description="GMIO PCR intake agent",
    )
    captured = {}

    class FakeMemorySessionManager:
        def close(self):
            captured["memory_closed"] = True

    memory_manager = FakeMemorySessionManager()

    def create_session_manager(actor_id, session_id, *, async_mode):
        captured["memory_factory"] = (actor_id, session_id, async_mode)
        return memory_manager

    class FakeAgent:
        def __init__(self, **kwargs):
            captured["agent_kwargs"] = kwargs

        def cleanup(self):
            captured["agent_cleaned"] = True

    memory.create_session_manager = create_session_manager
    monkeypatch.setattr(agent, "Agent", FakeAgent)
    monkeypatch.setattr(agent, "AgentSkills", lambda **kwargs: kwargs)
    def build_model(**kwargs):
        captured["model_kwargs"] = kwargs
        return kwargs

    monkeypatch.setattr(agent, "BedrockModel", build_model)
    monkeypatch.setattr(agent, "ENABLE_GATEWAYS", False)
    monkeypatch.setattr(agent, "ENABLE_CODE_INTERPRETER", False)

    request = agent.InvocationRequest(
        messages=[
            {"role": "system", "content": "CALLER_SYSTEM"},
            {"role": "user", "content": "old question"},
            {"role": "assistant", "content": "old answer"},
            {"role": "user", "content": "current question"},
        ],
        actor_id="actor-id",
        session_id="session-id",
        model_slug="strands-data-analyst",
        stream=True,
    )

    runtime_agent, interpreter_session, returned_manager, prompt = agent._prepare(
        request
    )

    assert isinstance(runtime_agent, FakeAgent)
    assert interpreter_session is None
    assert returned_manager is memory_manager
    assert prompt == "current question"
    assert captured["memory_factory"] == ("actor-id", "session-id", True)
    assert captured["agent_kwargs"]["session_manager"] is memory_manager
    assert captured["agent_kwargs"]["name"] == "gmio-pcr"
    assert captured["agent_kwargs"]["description"] == "GMIO PCR intake agent"
    assert captured["model_kwargs"]["cache_config"].ttl == "5m"
    assert captured["model_kwargs"]["cache_tools"].ttl == "5m"
    assert captured["agent_kwargs"]["system_prompt"] == (
        f"BASE_SYSTEM\n\n{DOCUMENT_GUIDANCE}\nACTIVATE_SKILLS\nMEMORY_GUIDANCE"
        "\n\n---\n\n## Caller-provided system guidance\n\nCALLER_SYSTEM"
    )

    agent._cleanup(runtime_agent, interpreter_session, memory_manager)
    assert captured["agent_cleaned"] is True
    assert captured["memory_closed"] is True


def test_prepare_applies_one_hour_prompt_cache_ttl(monkeypatch):
    agent, memory = _load_agent_module(monkeypatch, prompt_cache_ttl="1h")
    captured = {}

    memory.create_session_manager = lambda *_args, **_kwargs: None
    monkeypatch.setattr(agent, "Agent", lambda **kwargs: kwargs)
    monkeypatch.setattr(agent, "AgentSkills", lambda **kwargs: kwargs)

    def build_model(**kwargs):
        captured.update(kwargs)
        return kwargs

    monkeypatch.setattr(agent, "BedrockModel", build_model)
    monkeypatch.setattr(agent, "ENABLE_GATEWAYS", False)
    monkeypatch.setattr(agent, "ENABLE_CODE_INTERPRETER", False)

    request = agent.InvocationRequest(
        messages=[{"role": "user", "content": "create a dashboard"}],
        actor_id=None,
        session_id="session-id",
        model_slug="strands-data-analyst",
        stream=False,
    )

    agent._prepare(request)

    assert captured["cache_config"].ttl == "1h"
    assert captured["cache_tools"].ttl == "1h"


def test_prepare_omits_all_skill_components_when_skills_are_disabled(monkeypatch):
    agent, memory = _load_agent_module(monkeypatch, skills_enabled=False)
    captured = {}

    memory.create_session_manager = lambda *_args, **_kwargs: None

    def build_agent(**kwargs):
        captured.update(kwargs)
        return kwargs

    monkeypatch.setattr(agent, "Agent", build_agent)
    monkeypatch.setattr(
        agent,
        "AgentSkills",
        lambda **_kwargs: pytest.fail("AgentSkills must not be created"),
    )
    monkeypatch.setattr(agent, "BedrockModel", lambda **kwargs: kwargs)
    monkeypatch.setattr(agent, "ENABLE_GATEWAYS", False)
    monkeypatch.setattr(agent, "ENABLE_CODE_INTERPRETER", True)
    monkeypatch.setattr(agent.code_interpreter, "CODE_INTERPRETER_ID", "interpreter-id")
    monkeypatch.setattr(
        agent.code_interpreter,
        "start_session",
        lambda _session_id: "interpreter-session",
        raising=False,
    )

    def build_interpreter_tools(_session, *, skill_resource_uri):
        captured["skill_resource_uri"] = skill_resource_uri
        return ["execute_code"]

    monkeypatch.setattr(
        agent.code_interpreter,
        "build_tools",
        build_interpreter_tools,
        raising=False,
    )

    request = agent.InvocationRequest(
        messages=[{"role": "user", "content": "create a dashboard"}],
        actor_id=None,
        session_id="session-id",
        model_slug="strands-data-analyst",
        stream=False,
    )

    agent._prepare(request)

    assert captured["system_prompt"] == (
        f"BASE_SYSTEM\n\n{DOCUMENT_GUIDANCE}\nMEMORY_GUIDANCE"
    )
    assert captured["tools"] == ["execute_code"]
    assert captured["plugins"] == []
    assert captured["skill_resource_uri"] is None


def test_rejects_unsupported_prompt_cache_ttl(monkeypatch):
    with pytest.raises(ValueError, match="PROMPT_CACHE_TTL must be '5m' or '1h'"):
        _load_agent_module(monkeypatch, prompt_cache_ttl="30m")


def test_prepare_requires_an_explicit_model(monkeypatch):
    agent, _ = _load_agent_module(monkeypatch, model_id=None)
    request = agent.InvocationRequest(
        messages=[{"role": "user", "content": "hello"}],
        actor_id=None,
        session_id="session-id",
        model_slug="strands-data-analyst",
        stream=False,
    )

    with pytest.raises(ValueError, match="MODEL_ID or MODEL_ARN must be configured"):
        agent._prepare(request)


def test_model_usage_payload_separates_cache_tokens_and_estimates_cost(monkeypatch):
    agent, _ = _load_agent_module(monkeypatch)
    request = agent.InvocationRequest(
        messages=[{"role": "user", "content": "create a dashboard"}],
        actor_id="actor-id",
        session_id="session-id",
        model_slug="strands-data-analyst",
        stream=True,
    )
    runtime_agent = types.SimpleNamespace(
        event_loop_metrics=types.SimpleNamespace(
            accumulated_usage={
                "inputTokens": 1_000,
                "outputTokens": 100,
                "cacheReadInputTokens": 2_000,
                "cacheWriteInputTokens": 4_000,
                "totalTokens": 7_100,
            }
        )
    )

    payload = agent._model_usage_payload(
        request,
        runtime_agent,
        duration_ms=12_345,
        succeeded=True,
    )

    assert payload["input_tokens"] == 1_000
    assert payload["cache_read_input_tokens"] == 2_000
    assert payload["cache_write_input_tokens"] == 4_000
    assert payload["total_input_tokens"] == 7_000
    assert payload["cache_read_ratio"] == 0.285714
    assert payload["estimated_cost_breakdown_usd"] == {
        "input": 0.003,
        "output": 0.0015,
        "cache_read": 0.0006,
        "cache_write": 0.015,
    }
    assert payload["estimated_cost_usd"] == 0.0201
    assert payload["duration_ms"] == 12_345
    assert payload["succeeded"] is True


def test_stream_emits_model_usage_before_final_chunk(monkeypatch):
    agent, _ = _load_agent_module(monkeypatch)

    class FakeAgent:
        event_loop_metrics = types.SimpleNamespace(
            accumulated_usage={
                "inputTokens": 1_000,
                "outputTokens": 100,
                "cacheReadInputTokens": 2_000,
                "cacheWriteInputTokens": 4_000,
                "totalTokens": 7_100,
            }
        )

        async def stream_async(self, _prompt):
            yield {"data": "Answer"}

        def cleanup(self):
            pass

    runtime_agent = FakeAgent()
    monkeypatch.setattr(
        agent,
        "_prepare",
        lambda _request: (runtime_agent, None, None, "question"),
    )
    request = agent.InvocationRequest(
        messages=[{"role": "user", "content": "question"}],
        actor_id="actor-id",
        session_id="session-id",
        model_slug="strands-data-analyst",
        stream=True,
    )

    async def collect():
        return [event async for event in agent.stream(request)]

    events = asyncio.run(collect())

    assert events[0]["choices"][0]["delta"]["content"] == "Answer"
    assert events[-2]["event"] == "model_usage"
    assert events[-2]["total_input_tokens"] == 7_000
    assert events[-2]["output_tokens"] == 100
    assert events[-1]["choices"][0]["finish_reason"] == "stop"


def test_stream_logs_cancellation_and_reraises(monkeypatch, caplog):
    agent, _ = _load_agent_module(monkeypatch)

    class FakeAgent:
        event_loop_metrics = types.SimpleNamespace(accumulated_usage={})

        async def stream_async(self, _prompt):
            if False:
                yield None
            raise asyncio.CancelledError("client disconnected")

        def cleanup(self):
            pass

    runtime_agent = FakeAgent()
    monkeypatch.setattr(
        agent,
        "_prepare",
        lambda _request: (runtime_agent, None, None, "question"),
    )
    request = agent.InvocationRequest(
        messages=[{"role": "user", "content": "question"}],
        actor_id="actor-id",
        session_id="cancelled-session",
        model_slug="strands-data-analyst",
        stream=True,
    )

    async def collect():
        return [event async for event in agent.stream(request)]

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(collect())

    assert "Strands streaming invocation cancelled" in caplog.text
    assert "cancelled-session" in caplog.text


def test_stream_emits_heartbeat_while_waiting_for_strands(monkeypatch):
    agent, _ = _load_agent_module(monkeypatch)
    monkeypatch.setattr(agent, "RUNTIME_STREAM_HEARTBEAT_SECONDS", 0.01)

    class FakeAgent:
        event_loop_metrics = types.SimpleNamespace(accumulated_usage={})

        async def stream_async(self, _prompt):
            await asyncio.Event().wait()
            if False:
                yield None

        def cleanup(self):
            pass

    runtime_agent = FakeAgent()
    monkeypatch.setattr(
        agent,
        "_prepare",
        lambda _request: (runtime_agent, None, None, "question"),
    )
    request = agent.InvocationRequest(
        messages=[{"role": "user", "content": "question"}],
        actor_id="actor-id",
        session_id="heartbeat-session",
        model_slug="strands-data-analyst",
        stream=True,
    )

    async def first_event():
        events = agent.stream(request)
        try:
            return await events.__anext__()
        finally:
            await events.aclose()

    assert asyncio.run(first_event()) == {"event": "heartbeat"}


def test_heartbeat_wrapper_consumes_stream_in_one_task(monkeypatch):
    agent, _ = _load_agent_module(monkeypatch)
    monkeypatch.setattr(agent, "RUNTIME_STREAM_HEARTBEAT_SECONDS", 0.01)

    producer_tasks = []

    async def source_events():
        for index in range(3):
            producer_tasks.append(asyncio.current_task())
            yield {"data": str(index)}
            await asyncio.sleep(0)

    async def collect():
        return [
            event
            async for event in agent._events_with_heartbeats(source_events())
        ]

    assert asyncio.run(collect()) == [
        {"data": "0"},
        {"data": "1"},
        {"data": "2"},
    ]
    assert len({id(task) for task in producer_tasks}) == 1


def test_stream_emits_skill_input_and_full_result_details(monkeypatch):
    agent, _ = _load_agent_module(monkeypatch)
    monkeypatch.setattr(agent, "ENABLE_TOOL_DETAILS", True)

    class FakeAgent:
        event_loop_metrics = types.SimpleNamespace(accumulated_usage={})

        async def stream_async(self, _prompt):
            yield {
                "type": "tool_use_stream",
                "current_tool_use": {
                    "toolUseId": "skill-use-1",
                    "name": "skills",
                    "input": '{"skill_name":"admission-analysis"}',
                },
            }
            yield {
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "toolResult": {
                                "toolUseId": "skill-use-1",
                                "status": "success",
                                "content": [
                                    {"text": "Complete SKILL.md instructions"}
                                ],
                            }
                        }
                    ],
                },
            }

        def cleanup(self):
            pass

    runtime_agent = FakeAgent()
    monkeypatch.setattr(
        agent,
        "_prepare",
        lambda _request: (runtime_agent, None, None, "question"),
    )
    request = agent.InvocationRequest(
        messages=[{"role": "user", "content": "question"}],
        actor_id="actor-id",
        session_id="session-id",
        model_slug="strands-data-analyst",
        stream=True,
    )

    async def collect():
        return [event async for event in agent.stream(request)]

    events = asyncio.run(collect())
    steps = [event["step"] for event in events if event.get("event") == "agent_step"]

    assert steps == [
        {
            "id": "skill-use-1",
            "type": "skill",
            "name": "admission-analysis",
            "status": "started",
            "details": {"input": {"skill_name": "admission-analysis"}},
        },
        {
            "id": "skill-use-1",
            "type": "skill",
            "name": "admission-analysis",
            "status": "completed",
            "details": {
                "input": {"skill_name": "admission-analysis"},
                "output": [{"text": "Complete SKILL.md instructions"}],
            },
        },
    ]


def test_stream_waits_for_complete_skill_input_before_started_event(monkeypatch):
    agent, _ = _load_agent_module(monkeypatch)
    monkeypatch.setattr(agent, "ENABLE_TOOL_DETAILS", True)

    class FakeAgent:
        event_loop_metrics = types.SimpleNamespace(accumulated_usage={})

        async def stream_async(self, _prompt):
            yield {
                "type": "tool_use_stream",
                "current_tool_use": {
                    "toolUseId": "skill-use-1",
                    "name": "skills",
                    "input": "",
                },
            }
            yield {
                "type": "tool_use_stream",
                "current_tool_use": {
                    "toolUseId": "skill-use-1",
                    "name": "skills",
                    "input": '{"skill_name":"admission-analysis"}',
                },
            }
            yield {
                "type": "tool_result",
                "tool_result": {
                    "toolUseId": "skill-use-1",
                    "status": "success",
                    "content": [{"text": "Skill instructions"}],
                },
            }

        def cleanup(self):
            pass

    runtime_agent = FakeAgent()
    monkeypatch.setattr(
        agent,
        "_prepare",
        lambda _request: (runtime_agent, None, None, "question"),
    )
    request = agent.InvocationRequest(
        messages=[{"role": "user", "content": "question"}],
        actor_id="actor-id",
        session_id="session-id",
        model_slug="strands-data-analyst",
        stream=True,
    )

    async def collect():
        return [event async for event in agent.stream(request)]

    events = asyncio.run(collect())
    steps = [event["step"] for event in events if event.get("event") == "agent_step"]

    assert [step["status"] for step in steps] == ["started", "completed"]
    assert [step["name"] for step in steps] == [
        "admission-analysis",
        "admission-analysis",
    ]


def test_agent_step_omits_details_when_disabled(monkeypatch):
    agent, _ = _load_agent_module(monkeypatch)
    monkeypatch.setattr(agent, "ENABLE_TOOL_DETAILS", False)

    event = agent._agent_step(
        "skill-use-1",
        "skills",
        "completed",
        tool_input={"skill_name": "admission-analysis"},
        output=[{"text": "Skill instructions"}],
    )

    assert event["step"] == {
        "id": "skill-use-1",
        "type": "skill",
        "name": "admission-analysis",
        "status": "completed",
    }


def test_prepare_keeps_caller_history_when_memory_is_disabled(monkeypatch):
    agent, memory = _load_agent_module(monkeypatch)
    captured = {}

    memory.create_session_manager = lambda *_args, **_kwargs: None

    class FakeAgent:
        def __init__(self, **kwargs):
            captured["agent_kwargs"] = kwargs

    monkeypatch.setattr(agent, "Agent", FakeAgent)
    monkeypatch.setattr(agent, "AgentSkills", lambda **kwargs: kwargs)
    monkeypatch.setattr(agent, "BedrockModel", lambda **kwargs: kwargs)
    monkeypatch.setattr(agent, "ENABLE_GATEWAYS", False)
    monkeypatch.setattr(agent, "ENABLE_CODE_INTERPRETER", False)

    request = agent.InvocationRequest(
        messages=[
            {"role": "user", "content": "old question"},
            {"role": "assistant", "content": "old answer"},
            {"role": "user", "content": "current question"},
        ],
        actor_id=None,
        session_id="session-id",
        model_slug="strands-data-analyst",
        stream=False,
    )

    _, _, returned_manager, prompt = agent._prepare(request)

    assert returned_manager is None
    assert prompt == (
        "USER: old question\n"
        "ASSISTANT: old answer\n\n"
        "Current user message: current question"
    )
    assert captured["agent_kwargs"]["session_manager"] is None

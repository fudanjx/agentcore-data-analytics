import importlib.util
import sys
import types
from pathlib import Path

MODULE_PATH = Path(__file__).parents[1] / "Strands-runtime" / "agent.py"


def _load_agent_module(monkeypatch):
    code_interpreter = types.ModuleType("code_interpreter")
    code_interpreter.CODE_INTERPRETER_ID = ""

    gateway_proxy = types.ModuleType("gateway_proxy")
    gateway_proxy.GATEWAY_CONFIGS = {}
    gateway_proxy.mcp_label = lambda slug: slug

    memory = types.ModuleType("memory")
    memory.MEMORY_GUIDANCE = "\nMEMORY_GUIDANCE"

    skills_sync = types.ModuleType("skills_sync")
    skills_sync.ACTIVATION_GUIDANCE = "\nACTIVATE_SKILLS"
    skills_sync.LOCAL_DIR = "skills"
    skills_sync.read_skill_resource = lambda: None

    system_prompt = types.ModuleType("system_prompt")
    system_prompt.load = lambda: "BASE_SYSTEM"

    for name, module in {
        "code_interpreter": code_interpreter,
        "gateway_proxy": gateway_proxy,
        "memory": memory,
        "skills_sync": skills_sync,
        "system_prompt": system_prompt,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)

    spec = importlib.util.spec_from_file_location(
        "strands_runtime_agent_memory", MODULE_PATH
    )
    agent = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(agent)
    return agent, memory


def test_prepare_uses_native_session_manager_and_only_current_user_turn(monkeypatch):
    agent, memory = _load_agent_module(monkeypatch)
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
    monkeypatch.setattr(agent, "BedrockModel", lambda **kwargs: kwargs)
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
    assert captured["agent_kwargs"]["system_prompt"] == (
        "BASE_SYSTEM\nACTIVATE_SKILLS\nMEMORY_GUIDANCE"
        "\n\n---\n\n## Caller-provided system guidance\n\nCALLER_SYSTEM"
    )

    agent._cleanup(runtime_agent, interpreter_session, memory_manager)
    assert captured["agent_cleaned"] is True
    assert captured["memory_closed"] is True


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

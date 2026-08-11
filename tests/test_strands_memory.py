import importlib.util
from pathlib import Path

MODULE_PATH = Path(__file__).parents[1] / "Strands-runtime" / "memory.py"


def _load_memory_module():
    spec = importlib.util.spec_from_file_location("strands_runtime_memory", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_discovers_active_strategies_in_stable_type_order(monkeypatch):
    memory = _load_memory_module()

    class FakeControlClient:
        def get_memory(self, **kwargs):
            assert kwargs == {"memoryId": "memory-id"}
            return {
                "memory": {
                    "strategies": [
                        {
                            "strategyId": "preference-id",
                            "type": "USER_PREFERENCE",
                            "status": "ACTIVE",
                        },
                        {
                            "strategyId": "inactive-id",
                            "type": "SEMANTIC",
                            "status": "CREATING",
                        },
                        {
                            "strategyId": "summary-id",
                            "type": "SUMMARIZATION",
                            "status": "ACTIVE",
                        },
                        {
                            "strategyId": "semantic-id",
                            "type": "SEMANTIC",
                            "status": "ACTIVE",
                        },
                    ]
                }
            }

    monkeypatch.setattr(memory, "MEMORY_ID", "memory-id")
    monkeypatch.setattr(memory, "_control_client", FakeControlClient())
    monkeypatch.setattr(memory, "_strategy_ids", None)

    assert memory._get_strategy_ids() == (
        "semantic-id",
        "preference-id",
        "summary-id",
    )


def test_creates_native_session_manager_with_retrieval_and_lifecycle_config(
    monkeypatch,
):
    memory = _load_memory_module()
    captured = {}

    class FakeSessionManager:
        def __init__(self, config, region_name, converter):
            captured["config"] = config
            captured["region_name"] = region_name
            captured["converter"] = converter

    monkeypatch.setattr(memory, "MEMORY_ID", "memory-id")
    monkeypatch.setattr(memory, "REGION", "ap-southeast-1")
    monkeypatch.setattr(memory, "MEMORY_BATCH_SIZE", 10)
    monkeypatch.setattr(memory, "MEMORY_TOP_K", 5)
    monkeypatch.setattr(memory, "MEMORY_RELEVANCE_SCORE", 0.3)
    monkeypatch.setattr(memory, "_strategy_ids", ("semantic-id", "preference-id"))
    monkeypatch.setattr(memory, "AgentCoreMemorySessionManager", FakeSessionManager)

    manager = memory.create_session_manager(
        "actor-id",
        "session-id",
        async_mode=True,
    )

    assert isinstance(manager, FakeSessionManager)
    assert captured["region_name"] == "ap-southeast-1"
    assert captured["converter"] is memory._ConversationMemoryConverter
    config = captured["config"]
    assert config.memory_id == "memory-id"
    assert config.actor_id == "actor-id"
    assert config.session_id == "session-id"
    assert config.batch_size == 10
    assert config.async_mode is True
    assert config.context_tag == "memory_context"
    assert config.filter_restored_tool_context is True
    assert list(config.retrieval_config) == [
        "/strategies/semantic-id/actors/{actorId}/",
        "/strategies/preference-id/actors/{actorId}/",
    ]
    for retrieval in config.retrieval_config.values():
        assert retrieval.top_k == 5
        assert retrieval.relevance_score == 0.3


def test_memory_is_disabled_without_a_trusted_actor(monkeypatch):
    memory = _load_memory_module()
    monkeypatch.setattr(memory, "MEMORY_ID", "memory-id")

    assert memory.create_session_manager(None, "session-id") is None


def test_converter_restores_legacy_plain_text_events():
    memory = _load_memory_module()
    messages = memory._ConversationMemoryConverter.events_to_messages(
        [
            {
                "eventId": "event-id",
                "payload": [
                    {
                        "conversational": {
                            "role": "USER",
                            "content": {"text": "legacy question"},
                        }
                    },
                    {
                        "conversational": {
                            "role": "ASSISTANT",
                            "content": {"text": "legacy answer"},
                        }
                    },
                ],
            }
        ]
    )

    assert [item.message for item in messages] == [
        {"role": "user", "content": [{"text": "legacy question"}]},
        {"role": "assistant", "content": [{"text": "legacy answer"}]},
    ]


def test_converter_does_not_persist_raw_tool_results():
    memory = _load_memory_module()
    tool_message = memory.SessionMessage.from_message(
        {
            "role": "user",
            "content": [
                {
                    "toolResult": {
                        "toolUseId": "tool-id",
                        "content": [{"text": "sensitive database result"}],
                    }
                }
            ],
        },
        0,
    )

    assert memory._ConversationMemoryConverter.message_to_payload(tool_message) == []

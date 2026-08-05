import importlib.util
from datetime import datetime, timezone
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "app" / "memory.py"
SPEC = importlib.util.spec_from_file_location("runtime_memory_tests", MODULE_PATH)
memory = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(memory)


class FakeMemoryClient:
    def __init__(self):
        self.list_calls = []
        self.retrieve_calls = []

    def list_events(self, **kwargs):
        self.list_calls.append(kwargs)
        return {
            # Service order is newest first; the Runtime must reverse it.
            "events": [
                {
                    "eventTimestamp": datetime(2026, 8, 4, 9, 2, tzinfo=timezone.utc),
                    "payload": [
                        {
                            "conversational": {
                                "role": "USER",
                                "content": {"text": "What did I say?"},
                            }
                        },
                        {
                            "conversational": {
                                "role": "ASSISTANT",
                                "content": {"text": "You said hello."},
                            }
                        },
                    ],
                },
                {
                    "eventTimestamp": datetime(2026, 8, 4, 9, 1, tzinfo=timezone.utc),
                    "payload": [
                        {
                            "conversational": {
                                "role": "USER",
                                "content": {"text": "Hello"},
                            }
                        },
                        {
                            "conversational": {
                                "role": "ASSISTANT",
                                "content": {"text": "Hi there."},
                            }
                        },
                    ],
                },
            ]
        }

    def retrieve_memory_records(self, **kwargs):
        self.retrieve_calls.append(kwargs)
        if kwargs["namespace"].startswith("/strategies/semantic-"):
            text = "The user works with roster data."
        elif kwargs["namespace"].startswith("/strategies/preference-"):
            text = "The user prefers concise answers."
        else:
            text = "The prior session produced a staffing report."
        return {
            "memoryRecordSummaries": [
                {"content": {"text": text}},
            ]
        }


def test_retrieves_short_and_long_term_memory(monkeypatch):
    client = FakeMemoryClient()
    monkeypatch.setattr(memory, "_client", client)
    monkeypatch.setattr(memory, "MEMORY_ID", "memory-id")
    monkeypatch.setattr(memory, "SEMANTIC_STRATEGY_ID", "semantic-id")
    monkeypatch.setattr(memory, "PREFERENCE_STRATEGY_ID", "preference-id")
    monkeypatch.setattr(memory, "SUMMARY_STRATEGY_ID", "summary-id")

    short_term = memory.retrieve_short_term_context("actor-id", "session-id")
    long_term = memory.retrieve_long_term_context("actor-id", "hi")

    assert short_term.index("USER: Hello") < short_term.index("USER: What did I say?")
    assert "ASSISTANT: Hi there." in short_term
    assert "The user works with roster data." in long_term
    assert "The user prefers concise answers." in long_term
    assert "The prior session produced a staffing report." in long_term
    assert client.list_calls == [
        {
            "memoryId": "memory-id",
            "actorId": "actor-id",
            "sessionId": "session-id",
            "includePayloads": True,
            "maxResults": memory.MAX_SHORT_TERM_EVENTS,
        }
    ]
    assert [call["namespace"] for call in client.retrieve_calls] == [
        "/strategies/semantic-id/actors/actor-id/",
        "/strategies/preference-id/actors/actor-id/",
        "/strategies/summary-id/actors/actor-id/",
    ]
    assert all(
        call["searchCriteria"] == {"searchQuery": "hi", "topK": 5}
        for call in client.retrieve_calls
    )


def test_missing_memory_is_non_fatal(monkeypatch):
    class FailingClient:
        def list_events(self, **_kwargs):
            raise RuntimeError("short-term unavailable")

        def retrieve_memory_records(self, **_kwargs):
            raise RuntimeError("long-term unavailable")

    monkeypatch.setattr(memory, "_client", FailingClient())

    assert memory.retrieve_short_term_context("actor-id", "session-id") == ""
    assert memory.retrieve_long_term_context("actor-id", "question") == ""

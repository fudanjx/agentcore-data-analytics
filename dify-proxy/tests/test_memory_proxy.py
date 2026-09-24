import pathlib
import sys
import unittest
from unittest.mock import patch

from fastapi import HTTPException


PROXY_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROXY_DIR))

import memory_proxy


class FakeMemoryClient:
    def __init__(self):
        self.create_request = None
        self.retrieve_request = None

    def create_event(self, **kwargs):
        self.create_request = kwargs
        return {"event": {"eventId": "event-123"}}

    def retrieve_memory_records(self, **kwargs):
        self.retrieve_request = kwargs
        return {
            "memoryRecordSummaries": [
                {
                    "memoryRecordId": "record-1",
                    "content": {"text": "Prefers concise answers"},
                    "score": 0.91,
                },
                {"memoryRecordId": "record-2", "content": {}},
            ]
        }


class MemoryProxyTests(unittest.TestCase):
    def test_write_uses_payload_memory_id_and_conversation(self):
        client = FakeMemoryClient()
        payload = memory_proxy.MemoryWriteRequest(
            memory_id="memory_example-1234567890",
            actor_id="actor-1",
            session_id="session-1",
            user_text="Hello",
            assistant_text="Hi",
        )

        with patch.object(memory_proxy, "get_memory_client", return_value=client):
            result = memory_proxy._write_memory(payload)

        self.assertEqual(result, {"event_id": "event-123", "status": "accepted"})
        self.assertEqual(client.create_request["memoryId"], payload.memory_id)
        self.assertEqual(client.create_request["actorId"], "actor-1")
        self.assertEqual(client.create_request["sessionId"], "session-1")
        self.assertEqual(
            [item["conversational"]["role"] for item in client.create_request["payload"]],
            ["USER", "ASSISTANT"],
        )

    def test_retrieve_uses_payload_ids_and_returns_context(self):
        client = FakeMemoryClient()
        payload = memory_proxy.MemoryRetrieveRequest(
            memory_id="memory_example-1234567890",
            strategy_id="semantic_builtin-1234567890",
            actor_id="actor-1",
            query="What should I remember?",
            top_k=3,
        )

        with patch.object(memory_proxy, "get_memory_client", return_value=client):
            result = memory_proxy._retrieve_memory(payload)

        self.assertEqual(client.retrieve_request["memoryId"], payload.memory_id)
        self.assertEqual(
            client.retrieve_request["namespace"],
            "/strategies/semantic_builtin-1234567890/actors/actor-1/",
        )
        self.assertEqual(
            client.retrieve_request["searchCriteria"]["memoryStrategyId"],
            payload.strategy_id,
        )
        self.assertEqual(result["context"], "- Prefers concise answers")
        self.assertEqual(len(result["memories"]), 1)

    def test_auth_is_disabled_without_a_configured_key(self):
        with patch.object(memory_proxy, "MEMORY_PROXY_API_KEY", ""):
            with self.assertRaises(HTTPException) as caught:
                memory_proxy.require_memory_proxy_auth("Bearer anything")

        self.assertEqual(caught.exception.status_code, 503)

    def test_auth_accepts_the_configured_bearer_key(self):
        with patch.object(memory_proxy, "MEMORY_PROXY_API_KEY", "secret"):
            memory_proxy.require_memory_proxy_auth("Bearer secret")

    def test_auth_rejects_an_invalid_bearer_key(self):
        with patch.object(memory_proxy, "MEMORY_PROXY_API_KEY", "secret"):
            with self.assertRaises(HTTPException) as caught:
                memory_proxy.require_memory_proxy_auth("Bearer wrong")

        self.assertEqual(caught.exception.status_code, 401)


if __name__ == "__main__":
    unittest.main()

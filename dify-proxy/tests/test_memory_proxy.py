import pathlib
import sys
import unittest
import uuid
from unittest.mock import patch

from fastapi import HTTPException
from pydantic import ValidationError


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

    def test_email_user_id_maps_to_same_safe_actor_for_write_and_retrieve(self):
        client = FakeMemoryClient()
        email = "person@example.com"
        expected = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"agentcore-dify-user:{email}",
            )
        )
        write = memory_proxy.MemoryWriteRequest(
            memory_id="memory_example-1234567890",
            user_id=email,
            session_id="session-1",
            user_text="Hello",
            assistant_text="Hi",
        )
        retrieve = memory_proxy.MemoryRetrieveRequest(
            memory_id="memory_example-1234567890",
            strategy_id="semantic_builtin-1234567890",
            user_id=email,
            query="What should I remember?",
        )

        with patch.object(memory_proxy, "get_memory_client", return_value=client):
            memory_proxy._write_memory(write)
            self.assertEqual(client.create_request["actorId"], expected)
            memory_proxy._retrieve_memory(retrieve)

        self.assertEqual(
            client.retrieve_request["namespace"],
            f"/strategies/semantic_builtin-1234567890/actors/{expected}/",
        )

    def test_email_actor_id_uses_the_same_mapping_as_user_id(self):
        email = "person@example.com"
        actor_payload = memory_proxy.MemoryRetrieveRequest(
            memory_id="memory_example-1234567890",
            strategy_id="semantic_builtin-1234567890",
            actor_id=email,
            query="What should I remember?",
        )
        user_payload = memory_proxy.MemoryRetrieveRequest(
            memory_id="memory_example-1234567890",
            strategy_id="semantic_builtin-1234567890",
            user_id=email,
            query="What should I remember?",
        )

        self.assertEqual(
            actor_payload.resolved_actor_id(),
            user_payload.resolved_actor_id(),
        )

    def test_agentcore_safe_actor_id_remains_unchanged(self):
        payload = memory_proxy.MemoryRetrieveRequest(
            memory_id="memory_example-1234567890",
            strategy_id="semantic_builtin-1234567890",
            actor_id="actor-1",
            query="What should I remember?",
        )

        self.assertEqual(payload.resolved_actor_id(), "actor-1")

    def test_memory_identity_requires_exactly_one_supported_identifier(self):
        common = {
            "memory_id": "memory_example-1234567890",
            "session_id": "session-1",
            "user_text": "Hello",
            "assistant_text": "Hi",
        }
        with self.assertRaises(ValidationError):
            memory_proxy.MemoryWriteRequest(**common)
        with self.assertRaises(ValidationError):
            memory_proxy.MemoryWriteRequest(
                **common,
                actor_id="actor-1",
                user_id="person@example.com",
            )
        with self.assertRaises(ValidationError):
            memory_proxy.MemoryWriteRequest(
                **common,
                actor_id=" ",
            )

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

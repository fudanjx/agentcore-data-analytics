"""Authenticated HTTP facade for Amazon Bedrock AgentCore Memory."""

import hmac
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any

import boto3
import botocore.exceptions
from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

logger = logging.getLogger("agentcore-dify-proxy.memory")

REGION = os.environ.get("AWS_DEFAULT_REGION", "ap-southeast-1")
MEMORY_PROXY_API_KEY = os.environ.get("DIFY_MEMORY_PROXY_API_KEY", "").strip()

router = APIRouter(
    prefix="/memory",
    tags=["AgentCore Memory"],
)

_memory_client = None


class MemoryWriteRequest(BaseModel):
    memory_id: str = Field(min_length=12, max_length=2048)
    actor_id: str = Field(min_length=1, max_length=255)
    session_id: str = Field(min_length=1, max_length=100)
    user_text: str = Field(min_length=1, max_length=100_000)
    assistant_text: str = Field(min_length=1, max_length=100_000)
    client_token: str | None = Field(default=None, min_length=1, max_length=256)


class MemoryRetrieveRequest(BaseModel):
    memory_id: str = Field(min_length=12, max_length=2048)
    strategy_id: str = Field(min_length=1, max_length=100)
    actor_id: str = Field(min_length=1, max_length=255)
    query: str = Field(min_length=1, max_length=10_000)
    top_k: int = Field(default=5, ge=1, le=100)


def get_memory_client():
    """Return a process-local AgentCore data-plane client."""
    global _memory_client
    if _memory_client is None:
        _memory_client = boto3.client("bedrock-agentcore", region_name=REGION)
    return _memory_client


def require_memory_proxy_auth(
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Require the dedicated Dify-to-memory-proxy bearer credential."""
    if not MEMORY_PROXY_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="AgentCore Memory proxy is not configured",
        )
    expected = f"Bearer {MEMORY_PROXY_API_KEY}"
    if not authorization or not hmac.compare_digest(authorization, expected):
        raise HTTPException(
            status_code=401,
            detail="Invalid memory proxy credential",
            headers={"WWW-Authenticate": "Bearer"},
        )


def _agentcore_error(operation: str, error: Exception) -> HTTPException:
    """Convert SDK failures to a bounded response without leaking request data."""
    code = type(error).__name__
    if isinstance(error, botocore.exceptions.ClientError):
        code = str(error.response.get("Error", {}).get("Code") or code)
    logger.warning("AgentCore Memory %s failed: %s", operation, code)
    return HTTPException(
        status_code=502,
        detail={"error": "agentcore_memory_error", "code": code},
    )


def _write_memory(payload: MemoryWriteRequest) -> dict[str, Any]:
    try:
        result = get_memory_client().create_event(
            memoryId=payload.memory_id,
            actorId=payload.actor_id,
            sessionId=payload.session_id,
            eventTimestamp=datetime.now(timezone.utc),
            payload=[
                {
                    "conversational": {
                        "content": {"text": payload.user_text},
                        "role": "USER",
                    }
                },
                {
                    "conversational": {
                        "content": {"text": payload.assistant_text},
                        "role": "ASSISTANT",
                    }
                },
            ],
            clientToken=payload.client_token or str(uuid.uuid4()),
        )
    except (botocore.exceptions.BotoCoreError, botocore.exceptions.ClientError) as error:
        raise _agentcore_error("write", error) from error

    event = result.get("event") or {}
    return {
        "event_id": event.get("eventId"),
        "status": "accepted",
    }


def _retrieve_memory(payload: MemoryRetrieveRequest) -> dict[str, Any]:
    namespace = (
        f"/strategies/{payload.strategy_id}/actors/{payload.actor_id}/"
    )
    try:
        result = get_memory_client().retrieve_memory_records(
            memoryId=payload.memory_id,
            namespace=namespace,
            maxResults=payload.top_k,
            searchCriteria={
                "searchQuery": payload.query,
                "memoryStrategyId": payload.strategy_id,
                "topK": payload.top_k,
            },
        )
    except (botocore.exceptions.BotoCoreError, botocore.exceptions.ClientError) as error:
        raise _agentcore_error("retrieve", error) from error

    memories = []
    for record in result.get("memoryRecordSummaries", []):
        content = record.get("content") or {}
        text = content.get("text", "")
        if text:
            memories.append(
                {
                    "text": text,
                    "score": record.get("score"),
                    "memory_record_id": record.get("memoryRecordId"),
                }
            )
    return {
        "memories": memories,
        "context": "\n".join(f"- {memory['text']}" for memory in memories),
    }


@router.post("/write", dependencies=[Depends(require_memory_proxy_auth)])
async def write_memory(payload: MemoryWriteRequest):
    return await run_in_threadpool(_write_memory, payload)


@router.post("/retrieve", dependencies=[Depends(require_memory_proxy_auth)])
async def retrieve_memory(payload: MemoryRetrieveRequest):
    return await run_in_threadpool(_retrieve_memory, payload)

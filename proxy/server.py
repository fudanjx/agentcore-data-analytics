"""
AgentCore OpenAI-compatible proxy.

Accepts standard OpenAI /v1/chat/completions requests and forwards them to
the AgentCore Runtime via boto3 (IAM auth automatic via pod/instance role).
Deploy inside the same VPC as AgentCore for fully private traffic.
"""

import json
import logging
from typing import Any, Annotated, Literal
import uuid

import boto3
from fastapi import FastAPI
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("agentcore-proxy")

RUNTIME_ARN = "arn:aws:bedrock-agentcore:ap-southeast-1:964340114883:runtime/agentcore_poc-iumXW8638m"
REGION = "ap-southeast-1"

app = FastAPI(title="AgentCore Proxy", version="1.0.0")
_client = None


def get_client():
    global _client
    if _client is None:
        _client = boto3.client("bedrock-agentcore", region_name=REGION)
    return _client


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/v1/models")
def models():
    """Return model list — required by Open WebUI and DIFY for connection validation."""
    return {
        "object": "list",
        "data": [{"id": "agentcore", "object": "model", "owned_by": "agentcore"}],
    }

class InvocationPayload(BaseModel):
    messages: list
    model: Literal["agentcore"]
    stream: Annotated[bool, Field(default=False)]
    
class OAIResponse(BaseModel):
    id: str
    object: str
    model: str
    choices: list[dict[str, Any]]
    usage: dict[str, int]

def _sync_invoke(payload: bytes) -> Any:
    resp = get_client().invoke_agent_runtime(
        agentRuntimeArn=RUNTIME_ARN,
        contentType="application/json",
        accept="application/json",
        payload=payload,
    )
    raw = resp["response"].read()
    result_text = json.loads(raw).get("result", "")
    
    return result_text

@app.post("/v1/chat/completions")
async def chat_completions(invocation_payload: InvocationPayload):
    messages = invocation_payload.messages
    if not messages:
        return JSONResponse(status_code=400, content={"error": "messages must not be empty"})

    model = invocation_payload.model
    logger.info("Request: model=%s, messages=%d turns", model, len(messages))

    payload = json.dumps({"messages": messages}).encode()

    try:
        result_text = await run_in_threadpool(_sync_invoke, payload)
    except Exception as e:
        logger.error("AgentCore error: %s", e)
        return JSONResponse(status_code=502, content={"error": str(e)})

    choices_payload = [
            {
                "index": 0,
                "message": {"role": "assistant", "content": result_text},
                "finish_reason": "stop",
            }
    ]
    
    # NOTE: Workaround as agentcore-deployed agent does not support streaming currently
    if invocation_payload.stream:
        choices_payload[0]["text"] = result_text
        
    return OAIResponse(
        id=f"chatcmpl-{uuid.uuid4().hex[:12]}",
        object="chat.completion",
        model=model,
        choices=choices_payload,
        usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    )
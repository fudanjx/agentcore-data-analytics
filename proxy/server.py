"""
AgentCore OpenAI-compatible proxy.

Accepts standard OpenAI /v1/chat/completions requests and forwards them to
the AgentCore Runtime via boto3 (IAM auth automatic via pod/instance role).
Supports both streaming (SSE) and non-streaming responses.
Deploy inside the same VPC as AgentCore for fully private traffic.
"""

import json
import logging
import uuid

import boto3
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

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


def _invoke(messages: list) -> str:
    """Call AgentCore Runtime and return the result text."""
    payload = json.dumps({"messages": messages}).encode()
    resp = get_client().invoke_agent_runtime(
        agentRuntimeArn=RUNTIME_ARN,
        contentType="application/json",
        accept="application/json",
        payload=payload,
    )
    return json.loads(resp["response"].read()).get("result", "")


def _stream_response(result_text: str, model: str, completion_id: str):
    """Yield SSE chunks in OpenAI streaming format."""
    # Send content in one chunk (AgentCore doesn't stream token-by-token)
    chunk = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [{
            "index": 0,
            "delta": {"role": "assistant", "content": result_text},
            "finish_reason": None,
        }],
    }
    yield f"data: {json.dumps(chunk)}\n\n"

    # Send final chunk with finish_reason
    final = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [{
            "index": 0,
            "delta": {},
            "finish_reason": "stop",
        }],
    }
    yield f"data: {json.dumps(final)}\n\n"
    yield "data: [DONE]\n\n"


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


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid JSON body"})

    messages = body.get("messages", [])
    if not messages:
        return JSONResponse(status_code=400, content={"error": "messages must not be empty"})

    model = body.get("model", "agentcore")
    stream = body.get("stream", False)
    logger.info("Request: model=%s, messages=%d turns, stream=%s", model, len(messages), stream)

    try:
        result_text = _invoke(messages)
    except Exception as e:
        logger.error("AgentCore error: %s", e)
        return JSONResponse(status_code=502, content={"error": str(e)})

    completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"

    if stream:
        return StreamingResponse(
            _stream_response(result_text, model, completion_id),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    return {
        "id": completion_id,
        "object": "chat.completion",
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": result_text},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }

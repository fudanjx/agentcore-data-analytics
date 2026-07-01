"""
AgentCore OpenAI-compatible proxy.

Accepts standard OpenAI /v1/chat/completions requests and forwards them to
an AgentCore Runtime via boto3 (IAM auth automatic via pod/instance role).
Supports both streaming (SSE) and non-streaming responses.

Path-prefixed routes allow multiple runtimes on one service:
  /poc/v1/chat/completions     → agentcore_poc runtime
  /harness/v1/chat/completions → harness_harness_e52fs runtime
  /v1/chat/completions         → agentcore_poc (backward-compat)
"""

import json
import logging
import uuid

import boto3
from botocore.config import Config
from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, StreamingResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("agentcore-proxy")

REGION = "ap-southeast-1"

# Runtimes invoked via invoke_agent_runtime
RUNTIMES = {
    "poc": "arn:aws:bedrock-agentcore:ap-southeast-1:964340114883:runtime/agentcore_poc-iumXW8638m",
}

# Harnesses invoked via invoke_harness (managed runtimes cannot be called directly)
HARNESSES = {
    "harness": "arn:aws:bedrock-agentcore:ap-southeast-1:964340114883:harness/harness_e52fs-Du2DM0RxvF",
}

app = FastAPI(title="AgentCore Proxy", version="2.0.0")
_client = None


def get_client():
    global _client
    if _client is None:
        _client = boto3.client(
            "bedrock-agentcore",
            region_name=REGION,
            config=Config(
                read_timeout=15 * 60,
                connect_timeout=10,
                retries={"max_attempts": 0},
            ),
        )
    return _client


def _invoke_runtime(messages: list, runtime_arn: str) -> str:
    payload = json.dumps({"messages": messages}).encode()
    resp = get_client().invoke_agent_runtime(
        agentRuntimeArn=runtime_arn,
        contentType="application/json",
        accept="application/json",
        payload=payload,
    )
    return json.loads(resp["response"].read()).get("result", "")


def _invoke_harness(messages: list, harness_arn: str) -> str:
    # invoke_harness requires messages as [{role, content: [{text: "..."}]}]
    # Convert plain string content from OpenAI format if needed.
    normalized = []
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, str):
            content = [{"text": content}]
        normalized.append({"role": m["role"], "content": content})

    resp = get_client().invoke_harness(
        harnessArn=harness_arn,
        runtimeSessionId=str(uuid.uuid4()),  # min 33 chars; UUID is 36
        messages=normalized,
    )
    # Response is an event stream with contentBlockDelta events.
    parts = []
    for event in resp.get("stream", []):
        delta = event.get("contentBlockDelta", {}).get("delta", {})
        if "text" in delta:
            parts.append(delta["text"])
    return "".join(parts)


def _stream_response(result_text: str, model: str, completion_id: str):
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
    final = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    yield f"data: {json.dumps(final)}\n\n"
    yield "data: [DONE]\n\n"


async def _build_completion(messages: list, slug: str, model: str, stream: bool):
    logger.info("Request [%s]: model=%s, turns=%d, stream=%s", slug, model, len(messages), stream)
    if slug in HARNESSES:
        result_text = await run_in_threadpool(_invoke_harness, messages, HARNESSES[slug])
    else:
        result_text = await run_in_threadpool(_invoke_runtime, messages, RUNTIMES[slug])
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    if stream:
        return StreamingResponse(
            _stream_response(result_text, model, completion_id),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
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


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Per-runtime prefixed routes  /poc/v1/* and /harness/v1/*
# ---------------------------------------------------------------------------

ALL_SLUGS = set(RUNTIMES) | set(HARNESSES)


@app.get("/{slug}/v1/models")
def models_by_slug(slug: str):
    if slug not in ALL_SLUGS:
        return JSONResponse(status_code=404, content={"error": f"Unknown runtime: {slug}"})
    return {
        "object": "list",
        "data": [{"id": slug, "object": "model", "owned_by": "agentcore"}],
    }


@app.post("/{slug}/v1/chat/completions")
async def chat_completions_by_slug(slug: str, request: Request):
    if slug not in ALL_SLUGS:
        return JSONResponse(status_code=404, content={"error": f"Unknown runtime: {slug}"})
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid JSON body"})
    messages = body.get("messages", [])
    if not messages:
        return JSONResponse(status_code=400, content={"error": "messages must not be empty"})
    try:
        return await _build_completion(messages, slug, body.get("model", slug), body.get("stream", False))
    except Exception as e:
        logger.error("AgentCore error [%s]: %s", slug, e)
        return JSONResponse(status_code=502, content={"error": str(e)})


# ---------------------------------------------------------------------------
# Backward-compat bare /v1/* → poc runtime
# ---------------------------------------------------------------------------

@app.get("/v1/models")
def models_compat():
    return {
        "object": "list",
        "data": [{"id": "agentcore", "object": "model", "owned_by": "agentcore"}],
    }


@app.post("/v1/chat/completions")
async def chat_completions_compat(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid JSON body"})
    messages = body.get("messages", [])
    if not messages:
        return JSONResponse(status_code=400, content={"error": "messages must not be empty"})
    try:
        return await _build_completion(messages, "poc", body.get("model", "agentcore"), body.get("stream", False))
    except Exception as e:
        logger.error("AgentCore error [compat]: %s", e)
        return JSONResponse(status_code=502, content={"error": str(e)})

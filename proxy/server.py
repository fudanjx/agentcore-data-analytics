"""
AgentCore OpenAI-compatible proxy.

Accepts standard OpenAI /v1/chat/completions requests and forwards them to
an AgentCore Runtime or Harness via boto3 (IAM auth via pod IRSA).
Supports both streaming (SSE) and non-streaming responses.

Path-prefixed routes allow multiple runtimes on one service:
  /poc/v1/chat/completions     → agentcore_poc runtime (invoke_agent_runtime)
  /harness/v1/chat/completions → harness_harness_e52fs (invoke_harness)
  /v1/chat/completions         → agentcore_poc (backward-compat)

OpenWebUI session/user context is forwarded to AgentCore for memory:
  chat_id                  → runtimeSessionId  (stable per conversation)
  model_item.info.user_id  → actorId / runtimeUserId (stable per user)
"""

import json
import logging
import uuid

import boto3
import botocore.exceptions
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

ALL_SLUGS = set(RUNTIMES) | set(HARNESSES)

app = FastAPI(title="AgentCore Proxy", version="3.0.0")
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


def _extract_session_context(body: dict):
    """Extract stable session and user identifiers from an OpenWebUI request body.

    Returns (session_id, user_id):
      session_id — from chat_id (UUID, always ≥33 chars); falls back to new uuid4
      user_id    — from model_item.info.user_id; None if absent
    """
    session_id = body.get("chat_id") or str(uuid.uuid4())
    user_id = (body.get("model_item") or {}).get("info", {}).get("user_id")
    return session_id, user_id


def _runtime_kwargs(messages: list, runtime_arn: str, session_id: str = None, user_id: str = None) -> dict:
    payload = json.dumps({"messages": messages}).encode()
    kwargs = dict(
        agentRuntimeArn=runtime_arn,
        contentType="application/json",
        accept="text/event-stream",
        payload=payload,
    )
    if session_id:
        kwargs["runtimeSessionId"] = session_id
    if user_id:
        kwargs["runtimeUserId"] = user_id
    return kwargs


def _stream_runtime_events(messages: list, runtime_arn: str, session_id: str, user_id: str = None):
    """Generator: yields text deltas from a streaming AgentCore Runtime response.

    The Phase 2 container emits text/event-stream with OpenAI-format chunks. We read
    the botocore StreamingBody line by line, parse `data: {json}` payloads, and
    forward the delta.content text.

    Retries once on ConnectionClosedError if the error occurs before any token is
    yielded (cold-start container spin-up window).
    """
    kwargs = _runtime_kwargs(messages, runtime_arn, session_id, user_id)

    for attempt in range(2):
        first_token_sent = False
        try:
            resp = get_client().invoke_agent_runtime(**kwargs)
            body = resp["response"]  # botocore StreamingBody

            for raw_line in body.iter_lines():
                if not raw_line:
                    continue
                line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                if not line.startswith("data: "):
                    continue
                payload = line[6:]
                if payload == "[DONE]":
                    return
                try:
                    obj = json.loads(payload)
                except Exception:
                    continue
                choices = obj.get("choices") or []
                if not choices:
                    continue
                delta = (choices[0].get("delta") or {})
                text = delta.get("content")
                if text:
                    first_token_sent = True
                    yield text
            return
        except botocore.exceptions.ConnectionClosedError as e:
            if attempt == 0 and not first_token_sent:
                logger.warning(
                    "Runtime cold-start disconnect (session=%s), retrying...", session_id
                )
                continue
            raise


def _invoke_runtime_buffered(messages: list, runtime_arn: str, session_id: str, user_id: str = None) -> str:
    """Non-streaming path: collect all deltas and return the concatenated string."""
    return "".join(_stream_runtime_events(messages, runtime_arn, session_id, user_id))


async def _sse_runtime_stream(messages: list, runtime_arn: str, session_id: str, user_id, model: str, completion_id: str):
    """Async generator: yield OpenAI SSE chunks from live runtime stream events.

    Wraps the blocking `_stream_runtime_events` sync iterator via `iterate_in_threadpool`
    so each `iter_lines()` read runs off the event loop and streamed chunks are flushed
    to the client immediately (rather than buffered until the generator completes).
    """
    from starlette.concurrency import iterate_in_threadpool

    try:
        sync_iter = _stream_runtime_events(messages, runtime_arn, session_id, user_id)
        async for text in iterate_in_threadpool(sync_iter):
            chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "model": model,
                "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
            }
            yield f"data: {json.dumps(chunk)}\n\n"
    except Exception as e:
        logger.error("Runtime stream error (session=%s): %s", session_id, e)
        yield f"data: {json.dumps({'error': {'message': str(e), 'type': 'stream_error'}})}\n\n"

    final = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    yield f"data: {json.dumps(final)}\n\n"
    yield "data: [DONE]\n\n"


def _normalize_messages(messages: list) -> list:
    """Convert OpenAI string content to [{text: "..."}] format required by invoke_harness."""
    normalized = []
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, str):
            content = [{"text": content}]
        normalized.append({"role": m["role"], "content": content})
    return normalized


def _stream_harness_events(messages: list, harness_arn: str, session_id: str, actor_id: str = None):
    """Generator: yields text strings as contentBlockDelta events arrive from invoke_harness.

    Retries the full call once on cold-start connection close, but only if no token
    has been yielded yet (safe to retry before the SSE response is committed).
    After the first token is yielded, any error propagates to the caller.
    """
    kwargs = dict(
        harnessArn=harness_arn,
        runtimeSessionId=session_id,
        messages=_normalize_messages(messages),
    )
    if actor_id:
        kwargs["actorId"] = actor_id

    for attempt in range(2):
        first_token_sent = False
        try:
            resp = get_client().invoke_harness(**kwargs)
            for event in resp.get("stream", []):
                delta = event.get("contentBlockDelta", {}).get("delta", {})
                if "text" in delta:
                    first_token_sent = True
                    yield delta["text"]
            return  # stream complete normally
        except (botocore.exceptions.ConnectionClosedError, botocore.exceptions.EventStreamError) as e:
            if attempt == 0 and not first_token_sent and "connection" in str(e).lower():
                logger.warning(
                    "Harness cold-start disconnect (session=%s), retrying...", session_id
                )
                continue
            raise


def _invoke_harness_buffered(messages: list, harness_arn: str, session_id: str, actor_id: str = None) -> str:
    """Blocking call: collect all harness stream events and return full text. Used for non-streaming."""
    return "".join(_stream_harness_events(messages, harness_arn, session_id, actor_id))


def _sse_harness_stream(messages: list, harness_arn: str, session_id: str, actor_id, model: str, completion_id: str):
    """Generator: yield OpenAI SSE chunks from live harness stream events."""
    try:
        for text in _stream_harness_events(messages, harness_arn, session_id, actor_id):
            chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "model": model,
                "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
            }
            yield f"data: {json.dumps(chunk)}\n\n"
    except Exception as e:
        logger.error("Harness stream error (session=%s): %s", session_id, e)
        yield f"data: {json.dumps({'error': {'message': str(e), 'type': 'stream_error'}})}\n\n"

    final = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    yield f"data: {json.dumps(final)}\n\n"
    yield "data: [DONE]\n\n"


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


async def _build_completion(
    messages: list,
    slug: str,
    model: str,
    stream: bool,
    session_id: str,
    user_id: str = None,
):
    logger.info(
        "Request [%s]: model=%s, turns=%d, stream=%s, session=%s, actor=%s",
        slug, model, len(messages), stream, session_id, user_id,
    )
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"

    if slug in HARNESSES:
        arn = HARNESSES[slug]
        if stream:
            # Pipe harness token deltas directly to SSE — no buffering.
            return StreamingResponse(
                _sse_harness_stream(messages, arn, session_id, user_id, model, completion_id),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        result_text = await run_in_threadpool(
            _invoke_harness_buffered, messages, arn, session_id, user_id
        )
    else:
        # Runtime path — Phase 2: container emits text/event-stream SSE natively.
        arn = RUNTIMES[slug]
        if stream:
            return StreamingResponse(
                _sse_runtime_stream(messages, arn, session_id, user_id, model, completion_id),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        result_text = await run_in_threadpool(
            _invoke_runtime_buffered, messages, arn, session_id, user_id
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
# Per-runtime prefixed routes  /{slug}/v1/*
# ---------------------------------------------------------------------------

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
    session_id, user_id = _extract_session_context(body)
    try:
        return await _build_completion(
            messages, slug, body.get("model", slug), body.get("stream", False),
            session_id, user_id,
        )
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
    session_id, user_id = _extract_session_context(body)
    try:
        return await _build_completion(
            messages, "poc", body.get("model", "agentcore"), body.get("stream", False),
            session_id, user_id,
        )
    except Exception as e:
        logger.error("AgentCore error [compat]: %s", e)
        return JSONResponse(status_code=502, content={"error": str(e)})

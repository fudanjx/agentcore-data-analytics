"""
AgentCore proxy — OpenAI-compatible + Dify-compatible.

Accepts:
  - OpenAI /v1/chat/completions (OpenWebUI, etc.)
  - Dify /v1/chat-messages       (Dify Apps)

Forwards to an AgentCore Runtime or Harness via boto3 (IAM auth via pod IRSA).
Both streaming (SSE) and blocking responses are supported on both surfaces.

Path-prefixed routes allow multiple backends on one service:
  /poc/v1/chat/completions       → agentcore_poc runtime  (invoke_agent_runtime)
  /harness/v1/chat/completions   → harness_e52fs         (invoke_harness)
  /v1/chat/completions           → agentcore_poc          (OpenAI compat root)
  /dify/poc/v1/chat-messages     → agentcore_poc runtime
  /dify/harness/v1/chat-messages → harness_e52fs

OpenWebUI → AgentCore identity:
  chat_id                  → runtimeSessionId
  model_item.info.user_id  → actorId / runtimeUserId

Dify → AgentCore identity:
  conversation_id          → runtimeSessionId
  user                     → actorId / runtimeUserId
"""

import json
import logging
import time
import uuid

import boto3
import botocore.exceptions
from botocore.config import Config
from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import iterate_in_threadpool

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
    "dify": "arn:aws:bedrock-agentcore:ap-southeast-1:964340114883:harness/harness_dify-LViqrsm86E",
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


def _runtime_kwargs(messages: list, runtime_arn: str, session_id: str = None,
                    user_id: str = None) -> dict:
    body = {"messages": messages}
    if session_id:
        body["chat_id"] = session_id
    if user_id:
        body["model_item"] = {"info": {"user_id": user_id}}
    payload = json.dumps(body).encode()
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


def _stream_runtime_events(messages: list, runtime_arn: str, session_id: str,
                           user_id: str = None):
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


def _invoke_runtime_buffered(messages: list, runtime_arn: str, session_id: str,
                             user_id: str = None) -> str:
    """Non-streaming path: collect all deltas and return the concatenated string."""
    return "".join(_stream_runtime_events(messages, runtime_arn, session_id, user_id))


async def _sse_runtime_stream(messages: list, runtime_arn: str, session_id: str, user_id,
                              model: str, completion_id: str):
    """Async generator: yield OpenAI SSE chunks from live runtime stream events.

    Wraps the blocking `_stream_runtime_events` sync iterator via `iterate_in_threadpool`
    so each `iter_lines()` read runs off the event loop and streamed chunks are flushed
    to the client immediately (rather than buffered until the generator completes).
    """
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


def _normalize_messages(messages: list) -> tuple[list, list]:
    """Split OpenAI-style messages for invoke_harness.

    invoke_harness only accepts roles 'user' and 'assistant' — system messages
    must be hoisted into the separate `systemPrompt` field.

    Returns (harness_messages, system_prompt) where each item is content-normalised
    to the [{text: "..."}] shape the harness expects.
    """
    harness_messages: list = []
    system_prompt: list = []
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, str):
            content_blocks = [{"text": content}]
        else:
            content_blocks = content
        role = m.get("role")
        if role == "system":
            system_prompt.extend(content_blocks)
        else:
            harness_messages.append({"role": role, "content": content_blocks})
    return harness_messages, system_prompt


def _stream_harness_events(messages: list, harness_arn: str, session_id: str, actor_id: str = None):
    """Generator: yields text strings as contentBlockDelta events arrive from invoke_harness.

    Retries the full call once on cold-start connection close, but only if no token
    has been yielded yet (safe to retry before the SSE response is committed).
    After the first token is yielded, any error propagates to the caller.
    """
    harness_messages, system_prompt = _normalize_messages(messages)
    kwargs = dict(
        harnessArn=harness_arn,
        runtimeSessionId=session_id,
        messages=harness_messages,
    )
    if system_prompt:
        kwargs["systemPrompt"] = system_prompt
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


# ---------------------------------------------------------------------------
# Dify Chat App API — /dify/{slug}/v1/chat-messages
#
# Spec: https://docs.dify.ai/api-reference/chats/send-chat-message
# Dify sends a single-turn request per HTTP call; conversation history is
# maintained server-side by echoing conversation_id back to the client.
# ---------------------------------------------------------------------------

def _dify_parse(body: dict):
    """Return (query, user, conversation_id, response_mode).

    Mints a fresh conversation_id when the caller sends an empty one, so the
    first response can echo a stable id the client will reuse on the next turn.
    """
    query = (body.get("query") or "").strip()
    user = body.get("user") or None
    conversation_id = body.get("conversation_id") or str(uuid.uuid4())
    response_mode = body.get("response_mode") or "streaming"
    return query, user, conversation_id, response_mode


def _dify_event(event: str, conversation_id: str, message_id: str,
                task_id: str, **extra) -> str:
    frame = {
        "event": event,
        "conversation_id": conversation_id,
        "message_id": message_id,
        "task_id": task_id,
        "created_at": int(time.time()),
        **extra,
    }
    return f"data: {json.dumps(frame)}\n\n"


async def _dify_sse(sync_iter, conversation_id: str, message_id: str, task_id: str):
    """Wrap a sync text-delta iterator into Dify-format SSE frames.

    Uses iterate_in_threadpool so each blocking read from botocore runs off
    the event loop — same pattern as _sse_runtime_stream.
    """
    try:
        async for text in iterate_in_threadpool(sync_iter):
            yield _dify_event("message", conversation_id, message_id, task_id, answer=text)
    except Exception as e:
        logger.error("Dify stream error (conv=%s): %s", conversation_id, e)
        yield _dify_event(
            "error", conversation_id, message_id, task_id,
            status=500, code="runtime_error", message=str(e),
        )
        return

    yield _dify_event(
        "message_end", conversation_id, message_id, task_id,
        metadata={"usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}},
    )


def _dify_blocking_body(answer: str, conversation_id: str, message_id: str, task_id: str) -> dict:
    return {
        "event": "message",
        "task_id": task_id,
        "id": message_id,
        "message_id": message_id,
        "conversation_id": conversation_id,
        "mode": "chat",
        "answer": answer,
        "metadata": {"usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}},
        "created_at": int(time.time()),
    }


@app.post("/dify/{slug}/v1/chat-messages")
async def dify_chat_messages(slug: str, request: Request):
    if slug not in ALL_SLUGS:
        return JSONResponse(status_code=404, content={"error": f"Unknown backend: {slug}"})
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"code": "invalid_param",
                                                       "message": "invalid JSON body",
                                                       "status": 400})

    query, user, conversation_id, response_mode = _dify_parse(body)
    if not query:
        return JSONResponse(status_code=400, content={"code": "invalid_param",
                                                       "message": "query is required",
                                                       "status": 400})

    message_id = str(uuid.uuid4())
    task_id = str(uuid.uuid4())
    messages = [{"role": "user", "content": query}]

    logger.info("Dify [%s]: mode=%s, conv=%s, user=%s, q_chars=%d",
                slug, response_mode, conversation_id, user, len(query))

    # Build the sync generator that yields text deltas from the appropriate backend
    if slug in HARNESSES:
        def sync_iter():
            yield from _stream_harness_events(messages, HARNESSES[slug], conversation_id, user)
    else:
        def sync_iter():
            yield from _stream_runtime_events(messages, RUNTIMES[slug], conversation_id, user)

    if response_mode == "blocking":
        try:
            answer = await run_in_threadpool(lambda: "".join(sync_iter()))
            return _dify_blocking_body(answer, conversation_id, message_id, task_id)
        except Exception as e:
            logger.error("Dify blocking error [%s] (conv=%s): %s", slug, conversation_id, e)
            return JSONResponse(status_code=502, content={"code": "runtime_error",
                                                           "message": str(e),
                                                           "status": 502})

    return StreamingResponse(
        _dify_sse(sync_iter(), conversation_id, message_id, task_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

"""AgentCore Runtime container HTTP server.

AgentCore passes the caller's request body straight through to POST /invocations.
GET /ping is the AgentCore health check endpoint.

Phase 2:
- /invocations always returns text/event-stream with OpenAI-compatible SSE chunks.
- On startup: sync skills from S3 → /app/skills/, then launch the local SigV4
  proxy for AgentCore Gateway MCP calls on 127.0.0.1:9000.
"""

import asyncio
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app import agent, gateway_proxy, skills_sync

logger = logging.getLogger("agentcore")
logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Startup: syncing skills from S3...")
    skills_sync.sync_skills()
    logger.info("Startup: launching Gateway SigV4 proxy on localhost...")
    gateway_proxy.start_background()
    # Give uvicorn a moment to bind the port before /invocations arrives
    import asyncio
    await asyncio.sleep(1.0)
    yield


app = FastAPI(title="AgentCore Agent", version="2.0.0", lifespan=lifespan)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/ping")
def ping():
    return {"status": "ok"}


def _extract_session_context(request: Request, body: dict) -> tuple[str, str | None]:
    """Return (session_id, actor_id).

    Preferred source: AgentCore Runtime headers forwarded from the boto3
    invoke_agent_runtime API — same identity contract the harness path uses.
    Fallback: OpenWebUI-style body fields, for direct callers like py_sdk.py.

    session_id must be >=33 chars for AgentCore Memory. Pad if shorter.
    """
    session_id = request.headers.get("x-amzn-bedrock-agentcore-runtime-session-id")
    actor_id = request.headers.get("x-amzn-bedrock-agentcore-runtime-user-id")

    if not session_id:
        session_id = body.get("chat_id") or str(uuid.uuid4())
    if not actor_id:
        actor_id = (body.get("model_item") or {}).get("info", {}).get("user_id")

    if len(session_id) < 33:
        session_id = session_id.ljust(33, "x")
    return session_id, actor_id


async def _sse_stream(messages: list[dict], model_slug: str,
                     actor_id: str | None, session_id: str):
    """Emit OpenAI-format SSE chunks from the agent's text deltas."""
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())

    try:
        async for text in agent.stream(messages, actor_id=actor_id, session_id=session_id):
            chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model_slug,
                "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
            }
            yield f"data: {json.dumps(chunk)}\n\n"
    except Exception as e:
        logger.error("Agent stream error: %s", e, exc_info=True)
        err = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model_slug,
            "choices": [{"index": 0, "delta": {"content": f"\n\n[error] {e}"}, "finish_reason": "stop"}],
        }
        yield f"data: {json.dumps(err)}\n\n"
        yield "data: [DONE]\n\n"
        return

    final = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model_slug,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    yield f"data: {json.dumps(final)}\n\n"
    yield "data: [DONE]\n\n"


@app.post("/stream-test")
async def stream_test():
    """Streaming isolation probe.

    Emits 20 SSE chunks with 500ms gaps and no LLM involved. If chunks arrive
    progressively when invoked via boto3.invoke_agent_runtime, transport streaming
    works end-to-end and any buffering we see on /invocations is in the agent path.
    If chunks buffer here too, the problem is on AgentCore's ingress or our
    uvicorn/FastAPI stack.
    """
    async def gen():
        t0 = time.time()
        for i in range(20):
            elapsed = time.time() - t0
            yield f"data: {json.dumps({'chunk': i, 'elapsed_s': round(elapsed, 3)})}\n\n"
            await asyncio.sleep(0.5)
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Content-Encoding": "identity",
        },
    )


@app.post("/invocations")
async def invoke(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Body is not valid JSON"})

    # Streaming isolation probe: `{"test": "stream"}` bypasses the LLM and emits
    # 20 SSE chunks with 500ms gaps. Lets us test transport streaming vs. LLM streaming.
    if body.get("test") == "stream":
        return await stream_test()

    # Support both OpenAI format {"messages": [...]} and AgentCore console {"prompt": "..."}
    messages = body.get("messages")
    if not messages:
        prompt = body.get("prompt") or body.get("input") or body.get("inputText")
        if not prompt:
            return JSONResponse(status_code=400, content={"error": "provide 'messages' or 'prompt'"})
        messages = [{"role": "user", "content": str(prompt)}]

    model_slug = body.get("model", "poc")
    session_id, actor_id = _extract_session_context(request, body)
    header_keys = [k for k in request.headers.keys() if "amzn" in k.lower() or "agentcore" in k.lower() or "session" in k.lower() or "user" in k.lower()]
    logger.info("Invoke: model=%s, turns=%d, actor=%s, session=%s, id_headers=%s",
                model_slug, len(messages), actor_id, session_id, header_keys)

    return StreamingResponse(
        _sse_stream(messages, model_slug, actor_id, session_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Content-Encoding": "identity",
        },
    )

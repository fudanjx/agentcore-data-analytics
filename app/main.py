"""AgentCore Runtime container HTTP server.

AgentCore passes the caller's request body straight through to POST /invocations.
GET /ping is the AgentCore health check endpoint.
"""

import json
import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app import agent

logger = logging.getLogger("agentcore")
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="AgentCore Agent", version="0.1.0")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/ping")
def ping():
    return {"status": "ok"}


@app.post("/invocations")
async def invoke(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Body is not valid JSON"})

    # Support both OpenAI format {"messages": [...]} and AgentCore console {"prompt": "..."}
    messages = body.get("messages")
    if not messages:
        prompt = body.get("prompt") or body.get("input") or body.get("inputText")
        if not prompt:
            return JSONResponse(status_code=400, content={"error": "provide 'messages' or 'prompt'"})
        messages = [{"role": "user", "content": str(prompt)}]

    result = await agent.run(messages)
    return {"result": result}

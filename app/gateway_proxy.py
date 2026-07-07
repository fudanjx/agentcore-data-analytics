"""
Local SigV4-signing HTTP proxy for AgentCore Gateway MCP calls.

Claude Agent SDK's McpHttpServerConfig sends unsigned HTTP; AgentCore Gateway
with authorizerType=AWS_IAM requires each request to be freshly SigV4-signed.
This proxy runs on 127.0.0.1:9000 inside the container and rewrites requests:

    Claude SDK  →  http://localhost:9000/<slug>/mcp  →  signed HTTPS  →  Gateway

Slugs map to gateway URLs in GATEWAYS below. Add or edit entries there to
expose more gateways to the agent.

The proxy loops in a background thread started by main.py's lifespan.
"""

import asyncio
import logging
import threading
from typing import Optional

import boto3
import httpx
import uvicorn
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from fastapi import FastAPI, Request
from fastapi.responses import Response

logger = logging.getLogger(__name__)

REGION = "ap-southeast-1"
SERVICE = "bedrock-agentcore"
LISTEN_PORT = 9000

GATEWAYS = {
    "nuh": "https://nuh-analytics-db-fhbzdmtdta.gateway.bedrock-agentcore.ap-southeast-1.amazonaws.com",
    "ah":  "https://ah-analytics-db-gszih4adsx.gateway.bedrock-agentcore.ap-southeast-1.amazonaws.com",
    "fm":  "https://timesfm-gateway-w4fho4r9um.gateway.bedrock-agentcore.ap-southeast-1.amazonaws.com",
}

_session = boto3.Session()
_httpx_client: Optional[httpx.AsyncClient] = None


def _get_httpx_client() -> httpx.AsyncClient:
    global _httpx_client
    if _httpx_client is None:
        _httpx_client = httpx.AsyncClient(timeout=httpx.Timeout(120.0))
    return _httpx_client


def _sign(method: str, url: str, headers: dict, body: bytes) -> dict:
    """Freshly SigV4-sign the request and return the headers to forward."""
    creds = _session.get_credentials()
    if creds is None:
        raise RuntimeError("No AWS credentials in container — check runtime IAM role")
    frozen = creds.get_frozen_credentials()

    # botocore expects Host to be derivable from url; strip any incoming Host
    clean_headers = {k: v for k, v in headers.items() if k.lower() not in ("host", "content-length")}
    aws_req = AWSRequest(method=method, url=url, data=body, headers=clean_headers)
    SigV4Auth(frozen, SERVICE, REGION).add_auth(aws_req)
    return dict(aws_req.headers.items())


app = FastAPI(title="AgentCore Gateway SigV4 Proxy")


@app.api_route("/{slug}/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
async def proxy(slug: str, path: str, request: Request):
    if slug not in GATEWAYS:
        return Response(status_code=404, content=f"Unknown gateway slug: {slug}")

    target_url = f"{GATEWAYS[slug]}/{path}"
    body = await request.body()

    try:
        signed_headers = _sign(request.method, target_url, dict(request.headers), body)
    except Exception as e:
        logger.error("SigV4 sign failed for %s: %s", target_url, e)
        return Response(status_code=500, content=f"Sign failed: {e}")

    logger.debug("Forwarding %s %s → %s", request.method, request.url.path, target_url)

    client = _get_httpx_client()
    try:
        upstream = await client.request(
            method=request.method,
            url=target_url,
            content=body,
            headers=signed_headers,
        )
    except Exception as e:
        logger.error("Upstream call failed for %s: %s", target_url, e)
        return Response(status_code=502, content=f"Upstream error: {e}")

    # Strip hop-by-hop headers before returning
    resp_headers = {
        k: v for k, v in upstream.headers.items()
        if k.lower() not in ("connection", "transfer-encoding", "content-encoding", "content-length")
    }
    return Response(content=upstream.content, status_code=upstream.status_code, headers=resp_headers)


def _run_server():
    """Blocking uvicorn run — called from a background thread."""
    uvicorn.run(app, host="127.0.0.1", port=LISTEN_PORT, log_level="warning")


def start_background() -> threading.Thread:
    """Start the proxy in a daemon thread. Returns the thread handle."""
    t = threading.Thread(target=_run_server, daemon=True, name="gateway-proxy")
    t.start()
    logger.info("Gateway SigV4 proxy listening on 127.0.0.1:%d", LISTEN_PORT)
    return t


def mcp_urls() -> dict[str, str]:
    """Return the localhost URLs the agent should point McpHttpServerConfig at."""
    return {slug: f"http://127.0.0.1:{LISTEN_PORT}/{slug}/mcp" for slug in GATEWAYS}

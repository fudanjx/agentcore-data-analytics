"""Direct SigV4-authenticated MCP transport for AgentCore Gateways.

The module name is retained for compatibility with the first implementation,
but requests no longer pass through a localhost HTTP proxy. Signing the final
HTTPX request avoids signature mismatches caused by hop-by-hop headers.
"""

from collections.abc import Generator

import boto3
import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials
from mcp.client.streamable_http import streamablehttp_client

from gateway_config import GatewayConfig, load_gateway_configs


SERVICE = "bedrock-agentcore"
GATEWAY_CONFIGS = load_gateway_configs()


class SigV4HTTPXAuth(httpx.Auth):
    """Sign the finalized HTTPX request with AWS Signature Version 4."""

    def __init__(self, credentials: Credentials, service: str, region: str):
        self.signer = SigV4Auth(credentials, service, region)

    def auth_flow(
        self, request: httpx.Request
    ) -> Generator[httpx.Request, httpx.Response, None]:
        headers = dict(request.headers)
        # HTTPX does not forward this hop-by-hop header. Including it in the
        # canonical request makes the signature received by AWS invalid.
        headers.pop("connection", None)
        aws_request = AWSRequest(
            method=request.method,
            url=str(request.url),
            data=request.content,
            headers=headers,
        )
        self.signer.add_auth(aws_request)
        request.headers.update(dict(aws_request.headers))
        yield request


def mcp_transport(gateway: GatewayConfig):
    """Build a directly signed MCP Streamable HTTP transport."""
    session = boto3.Session(region_name=gateway.region)
    credentials = session.get_credentials()
    if credentials is None:
        raise RuntimeError("No AWS credentials available; check the Runtime IAM role")
    return streamablehttp_client(
        url=f"{gateway.url}/mcp",
        timeout=120,
        sse_read_timeout=600,
        auth=SigV4HTTPXAuth(credentials, SERVICE, gateway.region),
    )


def mcp_urls() -> dict[str, str]:
    return {slug: f"{gateway.url}/mcp" for slug, gateway in GATEWAY_CONFIGS.items()}


def mcp_label(slug: str) -> str:
    item = GATEWAY_CONFIGS.get(slug)
    return item.label if item else slug.replace("_", " ").title()

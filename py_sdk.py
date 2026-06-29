"""
External client for AgentCore — two modes:

1. Direct boto3 (simplest, no HTTP overhead):
   Invokes AgentCore Runtime directly via IAM-signed boto3 call.

2. Lambda Function URL with SigV4 (OpenAI-compatible):
   Calls the OpenAI wrapper Lambda URL, signed with AWS SigV4.
   Use this when your frontend expects the OpenAI /v1/chat/completions shape.

Both use your configured AWS credentials (~/.aws/credentials or env vars).

Usage:
    python py_sdk.py                          # direct boto3, default question
    python py_sdk.py "how many tables?"       # direct boto3, custom question
    python py_sdk.py --openai "list tables"   # Lambda URL, OpenAI response shape
"""

import json
import sys
import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
import urllib.request

# ── Config ────────────────────────────────────────────────────────────────────
RUNTIME_ARN = "arn:aws:bedrock-agentcore:ap-southeast-1:964340114883:runtime/agentcore_poc-iumXW8638m"
AGENTCORE_REGION = "ap-southeast-1"

LAMBDA_URL = "https://hx66okhncgcepkwmpk2ljzu5qe0wbtzz.lambda-url.us-east-1.on.aws/"
LAMBDA_REGION = "us-east-1"


# ── Mode 1: Direct boto3 (simplest) ──────────────────────────────────────────

def ask(question: str) -> str:
    """Ask the agent directly via boto3. Returns plain text answer."""
    client = boto3.client("bedrock-agentcore", region_name=AGENTCORE_REGION)

    payload = json.dumps({
        "messages": [{"role": "user", "content": question}]
    }).encode()

    response = client.invoke_agent_runtime(
        agentRuntimeArn=RUNTIME_ARN,
        contentType="application/json",
        accept="application/json",
        payload=payload,
    )

    raw = response["response"].read()
    result = json.loads(raw)
    return result.get("result", str(result))


# ── Mode 2: Lambda URL with SigV4 (OpenAI-compatible) ────────────────────────

def _sigv4_request(url: str, body: bytes, region: str, service: str = "lambda") -> bytes:
    """Make an IAM-signed POST request to a Lambda Function URL."""
    session = boto3.session.Session()
    credentials = session.get_credentials().get_frozen_credentials()

    request = AWSRequest(
        method="POST",
        url=url,
        data=body,
        headers={"Content-Type": "application/json"},
    )
    SigV4Auth(credentials, service, region).add_auth(request)

    req = urllib.request.Request(
        url,
        data=body,
        headers=dict(request.headers),
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        return resp.read()


def ask_openai(question: str) -> dict:
    """Ask the agent via the Lambda Function URL. Returns OpenAI-shaped dict."""
    body = json.dumps({
        "model": "agentcore",
        "messages": [{"role": "user", "content": question}],
    }).encode()

    raw = _sigv4_request(LAMBDA_URL, body, LAMBDA_REGION)
    return json.loads(raw)


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    args = sys.argv[1:]

    if args and args[0] == "--openai":
        question = " ".join(args[1:]) or "list all tables"
        print(f"Question: {question}\n")
        response = ask_openai(question)
        print(json.dumps(response, indent=2))
    else:
        question = " ".join(args) if args else "list all tables"
        print(f"Question: {question}\n")
        print(ask(question))

"""Directly verify that the custom AgentCore Code Interpreter can read S3."""

from __future__ import annotations

import argparse
import json
import shlex
import uuid

import boto3
from botocore.config import Config


REGION = "ap-southeast-1"
CODE_INTERPRETER_ID = "agentcore_user_uploads_ci-iZOyjlk0GA"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--expected-bytes", type=int, required=True)
    args = parser.parse_args()

    client = boto3.client(
        "bedrock-agentcore",
        region_name=REGION,
        config=Config(connect_timeout=10, read_timeout=180),
    )
    session = client.start_code_interpreter_session(
        codeInterpreterIdentifier=CODE_INTERPRETER_ID,
        name=f"s3-smoke-{uuid.uuid4().hex[:12]}",
        sessionTimeoutSeconds=120,
    )
    session_id = session["sessionId"]
    try:
        source = f"s3://{args.bucket}/{args.key}"
        destination = "/tmp/agentcore-s3-smoke"
        command = (
            f"timeout 30s aws s3 cp {shlex.quote(source)} "
            f"{shlex.quote(destination)} --region {REGION} "
            "--only-show-errors --cli-connect-timeout 10 --cli-read-timeout 20 "
            "&& "
            "python3 -c "
            + shlex.quote(
                "from pathlib import Path; "
                f"size=len(Path({destination!r}).read_bytes()); "
                'print(f"S3_CI_BYTES={size}")'
            )
        )
        response = client.invoke_code_interpreter(
            codeInterpreterIdentifier=CODE_INTERPRETER_ID,
            sessionId=session_id,
            name="executeCommand",
            arguments={"command": command},
        )
        events = list(response["stream"])
        rendered = "\n".join(
            json.dumps(event.get("result"), default=str) for event in events
        )
        marker = f"S3_CI_BYTES={args.expected_bytes}"
        if marker not in rendered:
            raise RuntimeError(
                f"Code Interpreter S3 smoke failed: {rendered[:2000]}"
            )
        print("code_interpreter_s3_read=true")
        print(marker)
    finally:
        client.stop_code_interpreter_session(
            codeInterpreterIdentifier=CODE_INTERPRETER_ID,
            sessionId=session_id,
        )


if __name__ == "__main__":
    main()

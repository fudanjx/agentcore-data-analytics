"""Smoke-test the Office Code Interpreter's constrained S3 output path."""

from __future__ import annotations

import base64
import json
import shlex
import uuid

import boto3
from botocore.config import Config


REGION = "ap-southeast-1"
BUCKET = "agentcore-openwebui-insights-964340114883"
CODE_INTERPRETER_ID = "agentcore_insights_office_ci-wNOyRxcsEC"


def main() -> None:
    user_id = "office-ci-smoke"
    chat_id = uuid.uuid4().hex
    filename = "office-ci-smoke.xlsx"
    key = f"openwebui-insights/outputs/{user_id}/{chat_id}/{filename}"
    tags = (
        f"OpenWebUI-User-Id={user_id}&OpenWebUI-Chat-Id={chat_id}"
        "&AgentCore-Artifact=generated"
    )
    client = boto3.client(
        "bedrock-agentcore",
        region_name=REGION,
        config=Config(connect_timeout=10, read_timeout=180),
    )
    session = client.start_code_interpreter_session(
        codeInterpreterIdentifier=CODE_INTERPRETER_ID,
        name=f"office-output-smoke-{uuid.uuid4().hex[:12]}",
        sessionTimeoutSeconds=120,
    )
    session_id = session["sessionId"]
    source = (
        "import docx, openpyxl, pptx, matplotlib\n"
        "from openpyxl import Workbook\n"
        "wb = Workbook()\n"
        "ws = wb.active\n"
        "ws.append(['status', 'value'])\n"
        "ws.append(['office-output-smoke', 1])\n"
        f"wb.save('/tmp/{filename}')\n"
    )
    encoded = base64.b64encode(source.encode()).decode()
    command = (
        f"echo {shlex.quote(encoded)} | base64 -d > /tmp/office_smoke.py"
        " && python3 /tmp/office_smoke.py"
        f" && aws s3api put-object --bucket {BUCKET} --key {shlex.quote(key)}"
        f" --body /tmp/{filename} --region {REGION}"
        " --content-type application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        f" --tagging {shlex.quote(tags)} >/dev/null"
    )
    try:
        response = client.invoke_code_interpreter(
            codeInterpreterIdentifier=CODE_INTERPRETER_ID,
            sessionId=session_id,
            name="executeCommand",
            arguments={"command": command},
        )
        events = list(response["stream"])
        rendered = "\n".join(json.dumps(item, default=str) for item in events)
        failed = any(
            item.get("result", {}).get("isError")
            or item.get("result", {}).get("structuredContent", {}).get("exitCode") not in (0, None)
            for item in events
            if isinstance(item, dict)
        )
        if failed:
            raise RuntimeError(f"Office Code Interpreter failed: {rendered[:3000]}")
    finally:
        client.stop_code_interpreter_session(
            codeInterpreterIdentifier=CODE_INTERPRETER_ID,
            sessionId=session_id,
        )

    s3 = boto3.client("s3", region_name=REGION)
    head = s3.head_object(Bucket=BUCKET, Key=key)
    object_tags = {
        item["Key"]: item["Value"]
        for item in s3.get_object_tagging(Bucket=BUCKET, Key=key)["TagSet"]
    }
    if (
        head["ContentLength"] <= 0
        or object_tags.get("OpenWebUI-User-Id") != user_id
        or object_tags.get("OpenWebUI-Chat-Id") != chat_id
        or object_tags.get("AgentCore-Artifact") != "generated"
    ):
        raise RuntimeError("Office Code Interpreter output did not meet the access contract")
    print("office_code_interpreter_s3_write=true")
    print(f"bytes={head['ContentLength']}")


if __name__ == "__main__":
    main()

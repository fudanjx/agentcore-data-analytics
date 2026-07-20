#!/usr/bin/env python3
"""Exercise OpenWebUI -> S3 -> AgentCore with protected EC2-side credentials."""

from __future__ import annotations

import argparse
import json
import mimetypes
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def request_json(
    url: str,
    *,
    token: str | None = None,
    payload: dict[str, Any] | None = None,
    data: bytes | None = None,
    content_type: str = "application/json",
) -> Any:
    headers = {"Content-Type": content_type}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method="POST" if data is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            raw = response.read()
    except urllib.error.HTTPError as error:
        detail = ""
        try:
            body = json.loads(error.read())
            detail = str(body.get("detail") or "")
        except Exception:
            pass
        raise RuntimeError(
            f"HTTP {error.code} from {url}: {detail[:300]}"
        ) from error
    return json.loads(raw) if raw else None


def multipart_file(
    *,
    field_name: str,
    filename: str,
    contents: bytes,
) -> tuple[bytes, str]:
    boundary = f"----AgentCoreSmoke{uuid.uuid4().hex}"
    mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    prefix = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{field_name}"; '
        f'filename="{filename}"\r\n'
        f"Content-Type: {mime_type}\r\n\r\n"
    ).encode("utf-8")
    suffix = f"\r\n--{boundary}--\r\n".encode("utf-8")
    return prefix + contents + suffix, f"multipart/form-data; boundary={boundary}"


def choose_model(models_response: Any) -> str:
    models = (
        models_response.get("data", [])
        if isinstance(models_response, dict)
        else models_response
    )
    ids = [
        str(item.get("id"))
        for item in (models or [])
        if isinstance(item, dict) and item.get("id")
    ]
    exact = next(
        (
            model_id
            for model_id in ids
            if model_id in {"insights", "agentcore.insights"}
        ),
        None,
    )
    if exact:
        return exact
    fallback = next(
        (model_id for model_id in ids if model_id.endswith("insights")),
        None,
    )
    if not fallback:
        raise RuntimeError(f"Insights model not found; available ids: {ids}")
    return fallback


def completion_text(response: Any) -> str:
    if not isinstance(response, dict):
        return ""
    choices = response.get("choices") or []
    if choices and isinstance(choices[0], dict):
        message = choices[0].get("message") or {}
        return str(message.get("content") or "")
    return str(response.get("content") or response.get("response") or "")


def stored_output_text(output: Any) -> str:
    if not isinstance(output, list):
        return ""
    text: list[str] = []
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if isinstance(part, dict) and part.get("type") == "output_text":
                value = part.get("text")
                if value:
                    text.append(str(value))
    return "\n".join(text)


def wait_for_chat_answer(
    *,
    base_url: str,
    token: str,
    chat_id: str,
    assistant_message_id: str | None,
    timeout_seconds: int = 360,
) -> str:
    deadline = time.monotonic() + timeout_seconds
    last_state: dict[str, Any] = {}
    while time.monotonic() < deadline:
        chat = request_json(
            f"{base_url}/api/v1/chats/{chat_id}",
            token=token,
        )
        history = ((chat or {}).get("chat") or {}).get("history") or {}
        messages = history.get("messages") or {}
        message_id = assistant_message_id or history.get("currentId")
        message = messages.get(message_id) if message_id else None
        if isinstance(message, dict):
            last_state = {
                "message_id": message_id,
                "done": message.get("done"),
                "has_content": bool(message.get("content")),
                "has_error": bool(message.get("error")),
                "keys": sorted(message.keys()),
            }
            error = message.get("error")
            if error:
                raise RuntimeError(f"AgentCore chat error: {str(error)[:300]}")
            content = (
                str(message.get("content") or "").strip()
                or stored_output_text(message.get("output")).strip()
            )
            if message.get("done") and content:
                return content
        time.sleep(5)
    raise RuntimeError(
        f"Timed out waiting for the AgentCore chat result; state={last_state}"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path("/home/ubuntu/app/insights/admin-bootstrap.env"),
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:3001")
    parser.add_argument(
        "--check-chat",
        help="Validate an existing smoke-test chat instead of creating a new one",
    )
    args = parser.parse_args()
    base_url = args.base_url.rstrip("/")

    credentials = load_env(args.env_file)
    signin = request_json(
        f"{base_url}/api/v1/auths/signin",
        payload={
            "email": credentials["ADMIN_EMAIL"],
            "password": credentials["ADMIN_PASSWORD"],
        },
    )
    token = signin.get("token")
    user_id = signin.get("id")
    if not token or not user_id or signin.get("role") != "admin":
        raise RuntimeError("Admin sign-in did not return an admin session")

    if args.check_chat:
        answer = wait_for_chat_answer(
            base_url=base_url,
            token=token,
            chat_id=args.check_chat,
            assistant_message_id=None,
            timeout_seconds=30,
        )
        if "E2E_SUM=6" not in answer.replace(" ", ""):
            raise RuntimeError(f"Unexpected AgentCore answer: {answer[:300]}")
        print("signin_role=admin")
        print(f"user_id={user_id}")
        print(f"chat_id={args.check_chat}")
        print("agentcore_answer=E2E_SUM=6")
        return 0

    model_id = choose_model(
        request_json(f"{base_url}/api/models", token=token)
    )

    filename = "agentcore-insights-e2e.csv"
    csv_contents = b"item,value\nalpha,1\nbeta,2\ngamma,3\n"
    upload_body, upload_type = multipart_file(
        field_name="file",
        filename=filename,
        contents=csv_contents,
    )
    uploaded = request_json(
        f"{base_url}/api/v1/files/?process=false",
        token=token,
        data=upload_body,
        content_type=upload_type,
    )
    file_id = str(uploaded.get("id") or "")
    if not file_id:
        raise RuntimeError("Upload did not return a file id")

    user_message_id = str(uuid.uuid4())
    assistant_message_id = str(uuid.uuid4())
    file_ref = {
        "type": "file",
        "id": file_id,
        "url": f"/api/v1/files/{file_id}/content",
        "name": filename,
    }
    prompt = (
        "Use Code Interpreter to read the attached CSV. "
        "Answer only E2E_SUM=<sum of the value column>."
    )
    chat_document = {
        "title": "AgentCore Insights E2E smoke test",
        "models": [model_id],
        "params": {},
        "history": {
            "messages": {
                user_message_id: {
                    "id": user_message_id,
                    "parentId": None,
                    "childrenIds": [assistant_message_id],
                    "role": "user",
                    "content": prompt,
                    "files": [file_ref],
                },
                assistant_message_id: {
                    "id": assistant_message_id,
                    "parentId": user_message_id,
                    "childrenIds": [],
                    "role": "assistant",
                    "content": "",
                    "model": model_id,
                },
            },
            "currentId": assistant_message_id,
        },
        "messages": [
            {"role": "user", "content": prompt, "files": [file_ref]}
        ],
    }
    chat = request_json(
        f"{base_url}/api/v1/chats/new",
        token=token,
        payload={"chat": chat_document},
    )
    chat_id = str(chat.get("id") or "")
    if not chat_id:
        raise RuntimeError("Chat creation did not return a chat id")

    response = request_json(
        f"{base_url}/api/chat/completions",
        token=token,
        payload={
            "model": model_id,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "chat_id": chat_id,
            "id": assistant_message_id,
            "session_id": str(uuid.uuid4()),
            "files": [file_ref],
            "features": {},
            "params": {},
            "background_tasks": {},
            "user_message": {
                "id": user_message_id,
                "parentId": None,
                "role": "user",
                "content": prompt,
                "files": [file_ref],
            },
            "assistant_message_id": assistant_message_id,
        },
    )
    answer = completion_text(response).strip()
    if not answer:
        if not (
            isinstance(response, dict)
            and response.get("status") is True
            and response.get("task_ids")
        ):
            keys = sorted(response.keys()) if isinstance(response, dict) else []
            raise RuntimeError(
                f"Completion was not dispatched; response keys: {keys}"
            )
        answer = wait_for_chat_answer(
            base_url=base_url,
            token=token,
            chat_id=chat_id,
            assistant_message_id=assistant_message_id,
        )
    if "E2E_SUM=6" not in answer.replace(" ", ""):
        raise RuntimeError(f"Unexpected AgentCore answer: {answer[:300]}")

    print("signin_role=admin")
    print(f"user_id={user_id}")
    print(f"model_id={model_id}")
    print(f"chat_id={chat_id}")
    print(f"file_id={file_id}")
    print("agentcore_answer=E2E_SUM=6")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"smoke_test_error={error}", file=sys.stderr)
        raise SystemExit(1)

"""
title: AgentCore Chat File Context
author: AgentCore POC
version: 2.0.0
description: Isolate OpenWebUI tasks, files, statuses, and artifacts for AgentCore.
"""

import asyncio
import base64
import json
import mimetypes
import os
import re
import urllib.error
import urllib.request
import uuid
from typing import Any


MODEL_SLUGS = {
    "strands": "strands",
    "agentcore-strands.strands": "strands",
    "insights": "insights",
    "agentcore.insights": "insights",
    "insights-office": "insights-office",
    "agentcore-office.insights-office": "insights-office",
    "gmio-pcr-dev": "gmio-pcr-dev",
    "agentcore-gmio.gmio-pcr-dev": "gmio-pcr-dev",
}
AGENTCORE_MODELS = set(MODEL_SLUGS)
AGENTCORE_PROXY_BASE_URL = os.environ.get(
    "AGENTCORE_PROXY_BASE_URL",
    "http://k8s-agentcor-agentcor-a9dbd8956e-c923dee5a7cceccb."
    "elb.ap-southeast-1.amazonaws.com",
).rstrip("/")
_ARTIFACT_MARKER = re.compile(
    r"\s*<agentcore-artifacts>\s*(\[.*?\])\s*</agentcore-artifacts>\s*",
    re.DOTALL,
)
_STATUS_MARKER = re.compile(r"<!--agentcore-status:(\{.*?\})-->", re.DOTALL)
_ARTIFACT_EVENT_MARKER = re.compile(
    r"^<!--agentcore-artifacts:([A-Za-z0-9_-]+={0,2})-->$"
)
_ARTIFACT_EVENT_ERROR_MARKER = "<!--agentcore-artifacts-error-->"


def _file_ids_from_chat(chat_data: dict) -> list[str]:
    history = (chat_data or {}).get("history") or {}
    messages = history.get("messages") or {}
    file_ids: list[str] = []
    seen: set[str] = set()
    for message in messages.values():
        if not isinstance(message, dict):
            continue
        for item in message.get("files") or []:
            if not isinstance(item, dict):
                continue
            nested_file = item.get("file") if isinstance(item.get("file"), dict) else {}
            file_id = item.get("id") or nested_file.get("id")
            if file_id and file_id not in seen:
                seen.add(file_id)
                file_ids.append(str(file_id))
    return file_ids


class Filter:
    async def inlet(
        self,
        body: dict,
        __user__: dict,
        __metadata__: dict,
        __chat_id__: str,
    ) -> dict:
        if body.get("model") not in AGENTCORE_MODELS:
            return body

        user_id = str((__user__ or {}).get("id") or "").strip()
        chat_id = str(__chat_id__ or "").strip()
        if not user_id or not chat_id:
            raise ValueError("AgentCore requires an authenticated OpenWebUI user and chat")

        from open_webui.models.chats import Chats

        chat = await Chats.get_chat_by_id_and_user_id(chat_id, user_id)
        if not chat:
            raise PermissionError("This OpenWebUI chat is not accessible to the current user")

        metadata = body.get("metadata")
        task = None
        if isinstance(__metadata__, dict):
            task = __metadata__.get("task")
        if not task and isinstance(metadata, dict):
            task = metadata.get("task")

        body.pop("files", None)
        if isinstance(metadata, dict):
            metadata.pop("files", None)
        if isinstance(__metadata__, dict):
            __metadata__.pop("files", None)

        if task:
            body["agentcore_request_context"] = {
                "kind": "background",
                "task": str(task),
            }
            body["agentcore_files"] = []
            return body

        body["agentcore_request_context"] = {"kind": "chat"}

        from open_webui.models.files import Files

        manifest: list[dict[str, Any]] = []
        for file_id in _file_ids_from_chat(chat.chat or {}):
            file = await Files.get_file_by_id_and_user_id(file_id, user_id)
            if not file:
                raise PermissionError(
                    f"File {file_id} is not accessible to the current user"
                )
            meta = file.meta if isinstance(file.meta, dict) else {}
            manifest.append(
                {
                    "file_id": file.id,
                    "s3_uri": file.path,
                    "filename": file.filename,
                    "mime_type": meta.get("content_type")
                    or "application/octet-stream",
                    "size": meta.get("size"),
                }
            )

        body["agentcore_files"] = manifest
        return body

    async def stream(
        self,
        event: dict,
        __event_emitter__=None,
        __body__: dict | None = None,
        __user__: dict | None = None,
        __metadata__: dict | None = None,
    ) -> dict:
        """Turn AgentCore proxy markers into statuses and owned file links."""
        model = ((__body__ or {}).get("model"))
        slug = MODEL_SLUGS.get(model)
        if not slug:
            return event
        try:
            choice = (event.get("choices") or [{}])[0]
            delta = choice.get("delta") or {}
            content = delta.get("content")
            if choice.get("finish_reason") == "stop":
                if __event_emitter__:
                    await __event_emitter__(
                        {
                            "type": "status",
                            "data": {
                                "description": "",
                                "done": True,
                                "hidden": True,
                            },
                        }
                    )
                return event

            status_matches = (
                list(_STATUS_MARKER.finditer(content))
                if isinstance(content, str)
                else []
            )
            if status_matches:
                for match in status_matches:
                    try:
                        status = json.loads(match.group(1))
                    except json.JSONDecodeError:
                        continue
                    description = str(status.get("description") or "Working")[:120]
                    if __event_emitter__:
                        await __event_emitter__(
                            {
                                "type": "status",
                                "data": {
                                    "description": description,
                                    "done": bool(status.get("done")),
                                    "hidden": bool(status.get("hidden")),
                                },
                            }
                        )
                # Strands may append status control markers to ordinary text in
                # the same SSE delta. Always remove them from user-visible prose.
                delta["content"] = _STATUS_MARKER.sub("", content)
                return event

            if content == _ARTIFACT_EVENT_ERROR_MARKER:
                delta["content"] = (
                    "\n\nGenerated file could not be made available. Please try again."
                )
                return event

            artifact_match = (
                _ARTIFACT_EVENT_MARKER.match(content)
                if isinstance(content, str)
                else None
            )
            if not artifact_match:
                return event

            user_id = str((__user__ or {}).get("id") or "").strip()
            chat_id = str(
                ((__metadata__ or {}).get("chat_id"))
                or ((__body__ or {}).get("chat_id"))
                or ""
            ).strip()
            if not user_id or not chat_id:
                raise PermissionError("OpenWebUI user or chat context is unavailable")
            candidate_artifacts = json.loads(
                base64.urlsafe_b64decode(artifact_match.group(1)).decode("utf-8")
            )
            artifacts = await asyncio.to_thread(
                _validate_artifacts_with_proxy,
                user_id,
                chat_id,
                candidate_artifacts,
                slug,
            )
            links = []
            for artifact in artifacts:
                file_id = str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"agentcore-artifact:{artifact['s3_uri']}",
                    )
                )
                await _ensure_openwebui_file(user_id, file_id, artifact)
                links.append(
                    f"[Download {artifact['filename']}]"
                    f"(/api/v1/files/{file_id}/content?attachment=true)"
                )
            if not links:
                raise RuntimeError("No downloadable artifacts were registered")
            delta["content"] = "\n\n" + "\n".join(links)
        except Exception:
            # Never expose an opaque artifact marker or S3 URI if registration
            # fails; the prose response remains usable.
            if isinstance(content, str) and (
                _ARTIFACT_EVENT_MARKER.match(content)
                or content == _ARTIFACT_EVENT_ERROR_MARKER
            ):
                delta["content"] = (
                    "\n\nGenerated file could not be made available. Please try again."
                )
        return event

    async def outlet(self, body: dict, __user__: dict) -> dict:
        """Replace validated artifact markers with authenticated file links."""
        slug = MODEL_SLUGS.get(body.get("model"))
        if not slug:
            return body
        user_id = str((__user__ or {}).get("id") or "").strip()
        chat_id = str(body.get("chat_id") or "").strip()
        if not user_id or not chat_id:
            return body

        for message in body.get("messages") or []:
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            content = message.get("content")
            if not isinstance(content, str):
                continue
            xml_match = _ARTIFACT_MARKER.search(content)
            opaque_match = _ARTIFACT_EVENT_MARKER.search(content)
            if not xml_match and not opaque_match:
                continue
            replacement = "\n\nGenerated file could not be made available. Please try again."
            try:
                if opaque_match:
                    candidate_artifacts = json.loads(
                        base64.urlsafe_b64decode(opaque_match.group(1)).decode("utf-8")
                    )
                else:
                    candidate_artifacts = json.loads(xml_match.group(1))
                artifacts = await asyncio.to_thread(
                    _validate_artifacts_with_proxy,
                    user_id,
                    chat_id,
                    candidate_artifacts,
                    slug,
                )
                links = []
                for artifact in artifacts:
                    file_id = str(
                        uuid.uuid5(
                            uuid.NAMESPACE_URL,
                            f"agentcore-artifact:{artifact['s3_uri']}",
                        )
                    )
                    await _ensure_openwebui_file(user_id, file_id, artifact)
                    links.append(
                        f"[Download {artifact['filename']}]"
                        f"(/api/v1/files/{file_id}/content?attachment=true)"
                    )
                if links:
                    replacement = "\n\n" + "\n".join(links)
            except Exception:
                # Do not leave a raw S3 URI in the saved chat if registration
                # or validation fails.
                pass
            marker = _ARTIFACT_EVENT_MARKER if opaque_match else _ARTIFACT_MARKER
            message["content"] = marker.sub(replacement, content, count=1)
        return body


def _validate_artifacts_with_proxy(
    user_id: str,
    chat_id: str,
    artifacts: Any,
    slug: str,
) -> list[dict]:
    payload = json.dumps({"artifacts": artifacts}).encode("utf-8")
    request = urllib.request.Request(
        f"{AGENTCORE_PROXY_BASE_URL}/{slug}/v1/artifacts/register",
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-OpenWebUI-User-Id": user_id,
            "X-OpenWebUI-Chat-Id": chat_id,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.loads(response.read())
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as error:
        raise RuntimeError("AgentCore artifact validation failed") from error
    validated = result.get("artifacts") if isinstance(result, dict) else None
    if not isinstance(validated, list) or not validated:
        raise RuntimeError("AgentCore artifact validation returned no artifacts")
    return validated


async def _ensure_openwebui_file(
    user_id: str,
    file_id: str,
    artifact: dict,
) -> None:
    """Register a generated S3 object as an owner-checked OpenWebUI File."""
    from open_webui.models.files import FileForm, Files

    existing = await Files.get_file_by_id(file_id)
    if existing:
        if existing.user_id != user_id or existing.path != artifact["s3_uri"]:
            raise PermissionError("Generated file registration collision")
        return
    filename = str(artifact["filename"])
    mime_type = str(
        artifact.get("mime_type")
        or mimetypes.guess_type(filename)[0]
        or "application/octet-stream"
    )
    created = await Files.insert_new_file(
        user_id,
        FileForm(
            id=file_id,
            filename=filename,
            path=str(artifact["s3_uri"]),
            data={},
            meta={
                "name": filename,
                "content_type": mime_type,
                "size": int(artifact.get("size") or 0),
                "agentcore_generated": True,
            },
        ),
    )
    if not created:
        raise RuntimeError("OpenWebUI file registration failed")

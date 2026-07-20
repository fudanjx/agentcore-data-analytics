"""
title: AgentCore Chat File Context
author: AgentCore POC
version: 1.1.0
description: Isolate OpenWebUI tasks and forward actor-owned chat files to AgentCore.
"""

from typing import Any


AGENTCORE_MODELS = {"insights", "agentcore.insights"}


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

# OpenWebUI → AgentCore Identity & Request Pipeline

Documents how OpenWebUI passes user ID and chat ID to AgentCore via the proxy.

---

## Overview

```
OpenWebUI UI
  │  user sends message
  ▼
Filter (inlet hook)  [agentcore_file_context.py]
  │  validates user owns the chat
  │  builds agentcore_files manifest (chat kind only)
  │  injects agentcore_request_context into body
  ▼
HTTP POST to proxy
  │  Header: X-OpenWebUI-User-Id: <uuid>   ← added automatically by OpenWebUI
  │  Header: X-OpenWebUI-Chat-Id:  <uuid>   ← added automatically by OpenWebUI
  │  Body:   { agentcore_request_context, agentcore_files, messages }
  ▼
Proxy  [proxy/server.py]
  │  _extract_openwebui_context()  → session_id, actor_id
  │  _prepare_openwebui_messages() → strip history (chat kind only)
  │  _inject_openwebui_file_context() → prepend file manifest to messages
  ▼
AWS invoke_agent_runtime(runtimeSessionId=session_id, runtimeUserId=actor_id)
```

---

## Step 1 — OpenWebUI forwards identity headers automatically

Configured via env var in `docker-compose.yml`:

```yaml
ENABLE_FORWARD_USER_INFO_HEADERS: "true"
```

This is a built-in OpenWebUI setting. When enabled, OpenWebUI appends two headers to
every outbound API call it makes to the configured OpenAI-compatible backend:

| Header | Value |
|---|---|
| `X-OpenWebUI-User-Id` | The authenticated user's UUID |
| `X-OpenWebUI-Chat-Id` | The current chat's UUID |

---

## Step 2 — The Filter enriches the request body

`openwebui-local/functions/agentcore_file_context.py` runs as an OpenWebUI `inlet`
hook before the request leaves OpenWebUI. It receives `__user__`, `__chat_id__`, and
`__metadata__` injected by OpenWebUI's hook system.

### Chat kind (normal user message)

`__metadata__.task` is absent → this is a real user turn.

1. Validates the user owns the chat via `Chats.get_chat_by_id_and_user_id`
2. Collects every file attached across all messages in the chat history
3. Verifies each file is owned by this user via `Files.get_file_by_id_and_user_id`
4. Builds a file manifest with S3 metadata for each file
5. Injects into the body:
   ```json
   {
     "agentcore_request_context": { "kind": "chat" },
     "agentcore_files": [
       { "file_id": "...", "s3_uri": "...", "filename": "...", "mime_type": "...", "size": 1234 }
     ]
   }
   ```

### Background kind (OpenWebUI internal task)

`__metadata__.task` is set → this is an internal OpenWebUI job (e.g. title generation,
auto-tagging, search query rewriting).

1. Strips all file references from the body
2. Injects into the body:
   ```json
   {
     "agentcore_request_context": { "kind": "background", "task": "<task-name>" },
     "agentcore_files": []
   }
   ```

---

## Step 3 — The proxy extracts identity and constructs AgentCore IDs

`_extract_openwebui_context()` in `proxy/server.py`:

```python
raw_user_id = request.headers.get("x-openwebui-user-id")
chat_id     = request.headers.get("x-openwebui-chat-id")
request_kind = body["agentcore_request_context"]["kind"]
```

It then constructs namespaced IDs:

| Kind | session_id | actor_id |
|---|---|---|
| `chat` | `{session_namespace}-{user_id}-{chat_id}` | `{actor_namespace}:{user_id}` |
| `background` | `{session_namespace}-bg-{random_uuid}` | `{actor_namespace}-task:{user_id}` |

**Chat session IDs are stable and deterministic** — the same chat always maps to the
same AgentCore session, preserving memory continuity across turns.

**Background session IDs are ephemeral** — a fresh random ID each time, so internal
tasks never accumulate state or touch user files.

---

## Step 4 — The proxy trims message history (chat kind only)

`_prepare_openwebui_messages()` in `proxy/server.py`:

OpenWebUI sends the full conversation history on every request (stateless API style).
AgentCore's Harness is **stateful** — it already holds the conversation in its session
memory. Replaying the full history would duplicate it.

For `chat` kind, the proxy strips messages down to:
- All `system` role messages
- Only the **latest** `user` message

Background requests pass through untouched.

---

## Step 5 — IDs are forwarded to the AgentCore Runtime API

`_runtime_kwargs()` in `proxy/server.py` passes both IDs to AWS:

```python
kwargs["runtimeSessionId"] = session_id
kwargs["runtimeUserId"]    = actor_id
```

These map to AgentCore's first-class identity parameters on `invoke_agent_runtime`.

---

## Key files

| File | Role |
|---|---|
| `openwebui-local/docker-compose.yml` | Sets `ENABLE_FORWARD_USER_INFO_HEADERS=true` |
| `openwebui-local/functions/agentcore_file_context.py` | OpenWebUI Filter: validates ownership, builds file manifest, sets request kind |
| `proxy/server.py` | Proxy: reads headers, constructs session/actor IDs, trims history, calls AgentCore |

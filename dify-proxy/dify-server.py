"""Dify model-provider proxy for Amazon Bedrock AgentCore backends.

Only the OpenAI-compatible endpoints used by Dify are exposed:

    GET  /{slug}/v1/models
    POST /{slug}/v1/chat/completions

Dify may send ``user`` and a message containing a
``<C_ID>{conversation UUID}<C_ID>`` marker to preserve actor and conversation
identity. OpenAI-compatible provider probes omit them, so the proxy supplies
request-scoped identifiers when absent. Non-UUID user strings are mapped to a
stable UUID. An assistant message carrying the marker is removed completely
before invoking AgentCore, because the model does not support assistant prefill.
Generated Office artifacts from Harnesses are independently validated in S3.
Runtime backends use the Runtime's native OpenAI-compatible SSE response. This
file intentionally contains no OpenWebUI, native Dify App API, or file-upload
proxy routes.
"""

import base64
import json
import logging
import mimetypes
import os
import queue
import re
import threading
import time
import uuid
from contextlib import contextmanager
from urllib.parse import urlparse

import boto3
import botocore.exceptions
from botocore.config import Config
from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, StreamingResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("agentcore-dify-proxy")

REGION = os.environ.get("AWS_DEFAULT_REGION", "ap-southeast-1")


def _bounded_int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    value = int(os.environ.get(name, str(default)))
    return min(maximum, max(minimum, value))


# Keep the AgentCore socket open longer than the Runtime's normal 3600-second
# idle-session window. Heartbeats below keep Dify-facing connections active;
# they do not alter the upstream boto socket timeout.
DIFY_RUNTIME_READ_TIMEOUT_SECONDS = _bounded_int_env(
    "DIFY_RUNTIME_READ_TIMEOUT_SECONDS", 3900, 60, 28800
)
DIFY_RUNTIME_HEARTBEAT_SECONDS = _bounded_int_env(
    "DIFY_RUNTIME_HEARTBEAT_SECONDS", 20, 5, 300
)
DIFY_ARTIFACT_CONNECT_TIMEOUT_SECONDS = _bounded_int_env(
    "DIFY_ARTIFACT_CONNECT_TIMEOUT_SECONDS", 3, 1, 30
)
DIFY_ARTIFACT_READ_TIMEOUT_SECONDS = _bounded_int_env(
    "DIFY_ARTIFACT_READ_TIMEOUT_SECONDS", 10, 1, 60
)

# READY runtimes discovered through the AgentCore control plane, keyed by
# agentRuntimeName. Names beginning with ``agentcore_`` also receive a short
# alias, so ``agentcore_dev`` remains available through the ``dev`` slug.
DIFY_RUNTIMES: dict[str, str] = {}
DIFY_RUNTIME_DISCOVERY_ENABLED = os.environ.get(
    "DIFY_RUNTIME_DISCOVERY_ENABLED",
    "true",
).lower() not in {"0", "false", "no", "off"}
DIFY_RUNTIME_DISCOVERY_TTL_SECONDS = max(
    1,
    int(os.environ.get("DIFY_RUNTIME_DISCOVERY_TTL_SECONDS", "300")),
)

# READY harnesses discovered through the AgentCore control plane, keyed by
# harnessName for use as the endpoint slug.
DIFY_HARNESSES: dict[str, str] = {}
DIFY_HARNESS_DISCOVERY_ENABLED = os.environ.get(
    "DIFY_HARNESS_DISCOVERY_ENABLED",
    "true",
).lower() not in {"0", "false", "no", "off"}
DIFY_HARNESS_DISCOVERY_TTL_SECONDS = max(
    1,
    int(os.environ.get("DIFY_HARNESS_DISCOVERY_TTL_SECONDS", "300")),
)

DIFY_OFFICE_ARTIFACTS_BUCKET = os.environ.get(
    "DIFY_OFFICE_ARTIFACTS_BUCKET",
    "ah-dify",
)
DIFY_OFFICE_ARTIFACTS_PREFIX = (
    os.environ.get("DIFY_OFFICE_ARTIFACTS_PREFIX", "harness_dev/").strip("/")
    + "/"
)
DIFY_OFFICE_SOURCE_PROFILE = {
    "bucket": DIFY_OFFICE_ARTIFACTS_BUCKET,
    "output_prefix": DIFY_OFFICE_ARTIFACTS_PREFIX,
    "output_extensions": {"csv", "docx", "xlsx", "pptx", "pdf"},
}

MAX_ARTIFACT_BYTES = 50 * 1024 * 1024
MAX_ARTIFACTS_PER_RESPONSE = 10
MAX_ARTIFACT_MARKER_BYTES = 64 * 1024
DIFY_ARTIFACT_URL_TTL_SECONDS = 60 * 60
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]")
_STATUS_NAME_UNSAFE_RE = re.compile(r"[^A-Za-z0-9 ._:/()\-]")
_STATUS_ID_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._:\-]")
RUNTIME_STEP_DETAIL_MAX_CHARS = min(
    1_000_000,
    max(1_000, int(os.environ.get("RUNTIME_STEP_DETAIL_MAX_CHARS", "500000"))),
)
_DIFY_CONVERSATION_ID_RE = re.compile(
    r"\s*<C_ID>"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
    r"<C_ID>\s*",
    re.IGNORECASE,
)
_OUTPUT_URL_RE = re.compile(
    r"\s*<output_url>\s*(true|false)\s*<(?:/)?output_url>\s*",
    re.IGNORECASE,
)
_ARTIFACT_START = "<agentcore-artifacts>"
_ARTIFACT_END = "</agentcore-artifacts>"
_ARTIFACT_ERROR_TEXT = (
    "\n\nGenerated file could not be made available. Please try again."
)
_RUNTIME_INTERRUPTED_TEXT = (
    "\n\n[stream interrupted] The upstream agent stream ended before completion. "
    "Please retry the request."
)

app = FastAPI(title="AgentCore Dify Proxy", version="1.1.0")

_agentcore_control_client = None
_s3_client = None
_runtime_discovery_lock = threading.Lock()
_runtime_discovery_attempted = False
_runtime_discovery_refreshed_at = 0.0
_harness_discovery_lock = threading.Lock()
_harness_discovery_attempted = False
_harness_discovery_refreshed_at = 0.0
_session_locks: dict[str, tuple[threading.Lock, int]] = {}
_session_locks_guard = threading.Lock()


def get_agentcore_client():
    """Create an invocation client with a new HTTP connection pool."""
    return boto3.client(
        "bedrock-agentcore",
        region_name=REGION,
        config=Config(
            read_timeout=DIFY_RUNTIME_READ_TIMEOUT_SECONDS,
            connect_timeout=10,
            # Do not automatically duplicate a stateful agent invocation.
            retries={"max_attempts": 0},
        ),
    )


def get_agentcore_control_client():
    global _agentcore_control_client
    if _agentcore_control_client is None:
        _agentcore_control_client = boto3.client(
            "bedrock-agentcore-control",
            region_name=REGION,
            config=Config(
                read_timeout=10,
                connect_timeout=5,
                retries={"mode": "standard", "max_attempts": 2},
            ),
        )
    return _agentcore_control_client


def _runtime_slugs(name: str) -> tuple[str, ...]:
    """Return the canonical runtime name and its optional friendly alias."""
    if name.startswith("agentcore_") and len(name) > len("agentcore_"):
        return name, name[len("agentcore_") :]
    return (name,)


def refresh_dify_runtimes(force: bool = False) -> dict[str, str]:
    """Discover READY runtimes, retaining the last successful result on failure."""
    global _runtime_discovery_attempted
    global _runtime_discovery_refreshed_at

    if not DIFY_RUNTIME_DISCOVERY_ENABLED:
        return dict(DIFY_RUNTIMES)

    with _runtime_discovery_lock:
        now = time.monotonic()
        cache_is_fresh = (
            _runtime_discovery_attempted
            and now - _runtime_discovery_refreshed_at
            < DIFY_RUNTIME_DISCOVERY_TTL_SECONDS
        )
        if not force and cache_is_fresh:
            return dict(DIFY_RUNTIMES)

        _runtime_discovery_attempted = True
        _runtime_discovery_refreshed_at = now
        try:
            discovered = {}
            paginator = get_agentcore_control_client().get_paginator(
                "list_agent_runtimes"
            )
            for page in paginator.paginate():
                for runtime in page.get("agentRuntimes", []):
                    name = runtime.get("agentRuntimeName")
                    arn = runtime.get("agentRuntimeArn")
                    if (
                        runtime.get("status") == "READY"
                        and isinstance(name, str)
                        and name
                        and isinstance(arn, str)
                        and arn
                    ):
                        discovered[name] = arn
            for name, arn in list(discovered.items()):
                for alias in _runtime_slugs(name)[1:]:
                    discovered.setdefault(alias, arn)
        except Exception as error:
            logger.warning(
                "Unable to discover AgentCore runtimes; retaining the last "
                "successful discovery result: %s",
                error,
            )
            return dict(DIFY_RUNTIMES)

        DIFY_RUNTIMES.clear()
        DIFY_RUNTIMES.update(discovered)
        logger.info(
            "Available Dify runtime backends: %s",
            ", ".join(sorted(DIFY_RUNTIMES)),
        )
        return dict(DIFY_RUNTIMES)


def get_dify_runtime_arn(slug: str) -> str | None:
    """Return a cached runtime ARN, refreshing discovery when its TTL expires."""
    runtimes = refresh_dify_runtimes()
    return runtimes.get(slug)


def refresh_dify_harnesses(force: bool = False) -> dict[str, str]:
    """Discover READY harnesses, retaining the last successful result on failure."""
    global _harness_discovery_attempted
    global _harness_discovery_refreshed_at

    if not DIFY_HARNESS_DISCOVERY_ENABLED:
        return dict(DIFY_HARNESSES)

    with _harness_discovery_lock:
        now = time.monotonic()
        cache_is_fresh = (
            _harness_discovery_attempted
            and now - _harness_discovery_refreshed_at
            < DIFY_HARNESS_DISCOVERY_TTL_SECONDS
        )
        if not force and cache_is_fresh:
            return dict(DIFY_HARNESSES)

        _harness_discovery_attempted = True
        _harness_discovery_refreshed_at = now
        try:
            discovered = {}
            paginator = get_agentcore_control_client().get_paginator(
                "list_harnesses"
            )
            for page in paginator.paginate():
                for harness in page.get("harnesses", []):
                    name = harness.get("harnessName")
                    arn = harness.get("arn")
                    if (
                        harness.get("status") == "READY"
                        and isinstance(name, str)
                        and name
                        and isinstance(arn, str)
                        and arn
                    ):
                        discovered[name] = arn
        except Exception as error:
            logger.warning(
                "Unable to discover AgentCore harnesses; retaining the last "
                "successful discovery result: %s",
                error,
            )
            return dict(DIFY_HARNESSES)

        DIFY_HARNESSES.clear()
        DIFY_HARNESSES.update(discovered)
        logger.info(
            "Available Dify harness backends: %s",
            ", ".join(sorted(DIFY_HARNESSES)),
        )
        return dict(DIFY_HARNESSES)


def get_dify_harness_arn(slug: str) -> str | None:
    """Return a cached harness ARN, refreshing discovery when its TTL expires."""
    harnesses = refresh_dify_harnesses()
    return harnesses.get(slug)


def get_dify_backend(slug: str) -> tuple[str, str] | None:
    """Resolve a slug to (backend type, ARN), preferring READY runtimes."""
    runtime_arn = get_dify_runtime_arn(slug)
    if runtime_arn:
        return "runtime", runtime_arn

    harness_arn = get_dify_harness_arn(slug)
    if harness_arn:
        return "harness", harness_arn
    return None


def get_s3_client():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client(
            "s3",
            region_name=REGION,
            config=Config(
                connect_timeout=DIFY_ARTIFACT_CONNECT_TIMEOUT_SECONDS,
                read_timeout=DIFY_ARTIFACT_READ_TIMEOUT_SECONDS,
                retries={"mode": "standard", "total_max_attempts": 1},
            ),
        )
    return _s3_client


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
    )


def _sanitize_filename(name: str) -> str:
    base = os.path.basename(name or "")
    base = _SAFE_NAME_RE.sub("_", base)
    return base[:200] or "unnamed"


class DifyArtifactError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def _extract_dify_session_context(
    request_body: dict,
) -> tuple[str, str, list[dict], bool]:
    """Resolve optional Dify identity and remove an injected C_ID carrier.

    Normal chat requests can supply stable identifiers. Dify's
    OpenAI-compatible credentials probe does not, so missing identity must not
    make provider validation fail. Assistant marker messages are dropped
    entirely so they cannot become an unsupported assistant prefill.
    """
    raw_user = request_body.get("user")
    if raw_user is None or not str(raw_user).strip():
        user_id = str(uuid.uuid4())
    else:
        try:
            user_id = str(uuid.UUID(str(raw_user).strip()))
        except (AttributeError, TypeError, ValueError):
            user_id = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"agentcore-dify-user:{str(raw_user).strip()}",
                )
            )

    messages = request_body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise HTTPException(
            status_code=400,
            detail={
                "error": {
                    "code": "invalid_request",
                    "message": "messages must not be empty",
                }
            },
        )
    if not all(isinstance(message, dict) for message in messages):
        raise HTTPException(
            status_code=400,
            detail={
                "error": {
                    "code": "invalid_request",
                    "message": "Each message must be an object",
                }
            },
        )

    cleaned_messages = [dict(message) for message in messages]
    output_urls = False
    for index in range(len(cleaned_messages) - 1, -1, -1):
        message = cleaned_messages[index]
        content = message.get("content")
        if message.get("role") != "assistant" or not isinstance(content, str):
            continue
        matches = list(_OUTPUT_URL_RE.finditer(content))
        if not matches:
            continue
        output_urls = output_urls or any(
            match.group(1).lower() == "true" for match in matches
        )
        cleaned_content = _OUTPUT_URL_RE.sub("", content)
        if cleaned_content.strip():
            message["content"] = cleaned_content
        else:
            del cleaned_messages[index]

    session_id = None
    for index, message in enumerate(cleaned_messages):
        content = message.get("content")
        if not isinstance(content, str):
            continue
        match = _DIFY_CONVERSATION_ID_RE.search(content)
        if not match:
            continue
        session_id = str(uuid.UUID(match.group(1)))
        cleaned_content = content[: match.start()] + content[match.end() :]
        if message.get("role") == "assistant" or not cleaned_content.strip():
            del cleaned_messages[index]
        else:
            cleaned_messages[index]["content"] = cleaned_content
        break

    if session_id is None:
        session_id = str(uuid.uuid4())
        logger.info(
            "Dify request has no C_ID marker; using an ephemeral session for "
            "OpenAI-compatible provider validation"
        )

    return session_id, user_id, cleaned_messages, output_urls


def _inject_dify_artifact_context(
    messages: list[dict],
    user_id: str,
    conversation_id: str,
    source_profile: dict,
) -> list[dict]:
    """Tell the Harness where generated files may be written for this request."""
    output_prefix = (
        f"s3://{source_profile['bucket']}/{source_profile['output_prefix']}"
        f"{user_id}/{conversation_id}/"
    )
    tagging = (
        f"Dify-User-Id={user_id}&"
        f"Dify-Conversation-Id={conversation_id}&AgentCore-Artifact=generated"
    )
    allowed_formats = ", ".join(
        extension.upper()
        for extension in sorted(source_profile["output_extensions"])
    )
    instruction = f"""## Generated Office files

For this request only, create new files solely under:
`{output_prefix}`
Never overwrite input files. Use a newly generated, safe filename. Upload each
output with `aws s3api put-object` (not `aws s3 cp`, which cannot set object
tags) and the exact S3 object tags below. Use the bucket
`{source_profile['bucket']}`, a key below the stated prefix, `--body` for the
local file, an appropriate `--content-type`, and:
`{tagging}`

Use only these downloadable output formats: {allowed_formats}.
Before the final answer, confirm each upload completed. Then report successful
outputs only inside an `<agentcore-artifacts>` JSON marker, listing the S3 URI
and user-facing filename for each output.

Do not generate a presigned URL and do not expose an S3 URI in user-visible
prose. The trusted proxy validates ownership and returns either its normal
`<agentcore-generated-files>` JSON envelope or short-lived download links,
according to the request's trusted output setting.
For html, just return the html artifact in the chat response don't upload it to s3.
"""
    return [*messages, {"role": "system", "content": instruction}]


def _normalize_messages(messages: list[dict]) -> tuple[list[dict], list[dict]]:
    """Convert OpenAI messages to the shapes accepted by invoke_harness."""
    harness_messages: list[dict] = []
    system_prompt: list[dict] = []
    for message in messages:
        content = message.get("content", "")
        content_blocks = [{"text": content}] if isinstance(content, str) else content
        if message.get("role") == "system":
            system_prompt.extend(content_blocks)
        else:
            harness_messages.append(
                {"role": message.get("role"), "content": content_blocks}
            )
    return harness_messages, system_prompt


@contextmanager
def _serialized_harness_session(session_id: str):
    """Prevent concurrent invoke_harness calls for one stateful session."""
    with _session_locks_guard:
        entry = _session_locks.get(session_id)
        lock, users = entry if entry is not None else (threading.Lock(), 0)
        _session_locks[session_id] = (lock, users + 1)

    try:
        with lock:
            yield
    finally:
        with _session_locks_guard:
            current_lock, users = _session_locks[session_id]
            if users == 1:
                del _session_locks[session_id]
            else:
                _session_locks[session_id] = (current_lock, users - 1)


def _stream_harness_events(
    messages: list[dict],
    harness_arn: str,
    session_id: str,
    actor_id: str,
):
    """Yield text deltas from AgentCore, retrying one pre-token disconnect."""
    harness_messages, system_prompt = _normalize_messages(messages)
    kwargs = {
        "harnessArn": harness_arn,
        "runtimeSessionId": session_id,
        "messages": harness_messages,
        "actorId": actor_id,
    }
    if system_prompt:
        kwargs["systemPrompt"] = system_prompt

    with _serialized_harness_session(session_id):
        for attempt in range(2):
            first_token_sent = False
            client = None
            try:
                client = get_agentcore_client()
                response = client.invoke_harness(**kwargs)
                for event in response.get("stream", []):
                    delta = event.get("contentBlockDelta", {}).get("delta", {})
                    text = delta.get("text")
                    if text:
                        first_token_sent = True
                        yield text
                return
            except (
                botocore.exceptions.ConnectionClosedError,
                botocore.exceptions.EventStreamError,
            ) as error:
                if (
                    attempt == 0
                    and not first_token_sent
                    and "connection" in str(error).lower()
                ):
                    logger.warning(
                        "Harness cold-start disconnect (session=%s), retrying",
                        session_id,
                    )
                    continue
                raise
            finally:
                if client is not None:
                    try:
                        client.close()
                    except Exception:
                        logger.debug(
                            "Unable to close Harness invocation client",
                            exc_info=True,
                        )


def _runtime_kwargs(
    messages: list[dict],
    runtime_arn: str,
    session_id: str,
    user_id: str,
) -> dict:
    """Build the request expected by an AgentCore Runtime backend."""
    payload = {
        "messages": messages,
        "chat_id": session_id,
        "model_item": {"info": {"user_id": user_id}},
    }
    return {
        "agentRuntimeArn": runtime_arn,
        "contentType": "application/json",
        "accept": "text/event-stream",
        "payload": json.dumps(payload).encode(),
        "runtimeSessionId": session_id,
        "runtimeUserId": user_id,
    }


class _RuntimeUsage:
    """Aggregate token usage safe to expose through the OpenAI-compatible API."""

    __slots__ = ("completion_tokens", "prompt_tokens", "total_tokens")

    def __init__(self, prompt_tokens: int, completion_tokens: int):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = prompt_tokens + completion_tokens

    def as_openai(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }


class _RuntimeHeartbeat:
    """Internal marker rendered as an SSE comment for downstream keepalive."""


class _RuntimeStreamFailure:
    __slots__ = ("error",)

    def __init__(self, error: Exception):
        self.error = error


_RUNTIME_STREAM_DONE = object()
_RUNTIME_TRANSPORT_ERRORS = (
    botocore.exceptions.ConnectionClosedError,
    botocore.exceptions.ReadTimeoutError,
    botocore.exceptions.ResponseStreamingError,
)


class _BufferedRuntimeResult:
    __slots__ = ("agent_steps", "text", "usage")

    def __init__(
        self,
        text: str,
        usage: _RuntimeUsage | None,
        agent_steps: list[dict] | None = None,
    ):
        self.text = text
        self.usage = usage
        self.agent_steps = agent_steps or []


def _usage_token_count(value) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        count = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return count if count >= 0 else None


def _runtime_usage(payload) -> _RuntimeUsage | None:
    if not isinstance(payload, dict):
        return None

    prompt_tokens = _usage_token_count(payload.get("total_input_tokens"))
    if prompt_tokens is None:
        prompt_tokens = _usage_token_count(payload.get("prompt_tokens"))
    if prompt_tokens is None:
        input_tokens = _usage_token_count(payload.get("input_tokens"))
        cache_read_tokens = _usage_token_count(
            payload.get("cache_read_input_tokens")
        )
        cache_write_tokens = _usage_token_count(
            payload.get("cache_write_input_tokens")
        )
        if any(
            count is not None
            for count in (input_tokens, cache_read_tokens, cache_write_tokens)
        ):
            prompt_tokens = sum(
                count or 0
                for count in (input_tokens, cache_read_tokens, cache_write_tokens)
            )

    completion_tokens = _usage_token_count(payload.get("output_tokens"))
    if completion_tokens is None:
        completion_tokens = _usage_token_count(payload.get("completion_tokens"))
    if prompt_tokens is None or completion_tokens is None:
        return None
    return _RuntimeUsage(prompt_tokens, completion_tokens)


def _stream_runtime_events(
    messages: list[dict],
    runtime_arn: str,
    session_id: str,
    user_id: str,
):
    """Yield Runtime events and periodic keepalives without replaying requests."""
    kwargs = _runtime_kwargs(messages, runtime_arn, session_id, user_id)
    items: queue.Queue = queue.Queue(maxsize=256)
    stop_requested = threading.Event()
    state = {"body": None}

    def enqueue(item) -> bool:
        while not stop_requested.is_set():
            try:
                items.put(item, timeout=0.25)
                return True
            except queue.Full:
                continue
        return False

    def put_event_items(event: dict) -> None:
        if event.get("event") == "model_usage":
            usage = _runtime_usage(event.get("usage") or event)
            if usage is not None:
                enqueue(usage)
            return
        if event.get("usage") is not None:
            usage = _runtime_usage(event.get("usage"))
            if usage is not None:
                enqueue(usage)
        if event.get("event") == "heartbeat":
            enqueue(_RuntimeHeartbeat())
            return
        if event.get("event") == "agent_step":
            status = _runtime_status(event.get("step"))
            if status is not None:
                enqueue(status)
            return
        choices = event.get("choices") or []
        if not choices:
            return
        text = (choices[0].get("delta") or {}).get("content")
        if text:
            enqueue(text)

    def read_response() -> None:
        body = None
        client = None
        try:
            client = get_agentcore_client()
            response = client.invoke_agent_runtime(**kwargs)
            body = response["response"]
            state["body"] = body
            for raw_line in body.iter_lines():
                if stop_requested.is_set():
                    return
                if not raw_line:
                    continue
                line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                if not line.startswith("data: "):
                    continue
                payload = line[6:]
                if payload == "[DONE]":
                    return
                try:
                    event = json.loads(payload)
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if isinstance(event, dict):
                    put_event_items(event)
        except _RUNTIME_TRANSPORT_ERRORS as error:
            enqueue(_RuntimeStreamFailure(error))
        except Exception as error:
            enqueue(_RuntimeStreamFailure(error))
        finally:
            if body is not None:
                try:
                    body.close()
                except Exception:
                    logger.debug("Unable to close Runtime response body", exc_info=True)
            if client is not None:
                try:
                    client.close()
                except Exception:
                    logger.debug(
                        "Unable to close Runtime invocation client",
                        exc_info=True,
                    )
            state["body"] = None
            enqueue(_RUNTIME_STREAM_DONE)

    reader = threading.Thread(
        target=read_response,
        name=f"agentcore-stream-{session_id[:8]}",
        daemon=True,
    )
    reader.start()
    try:
        while True:
            try:
                item = items.get(timeout=DIFY_RUNTIME_HEARTBEAT_SECONDS)
            except queue.Empty:
                yield _RuntimeHeartbeat()
                continue
            if item is _RUNTIME_STREAM_DONE:
                return
            if isinstance(item, _RuntimeStreamFailure):
                raise item.error
            yield item
    finally:
        stop_requested.set()
        body = state.get("body")
        if body is not None:
            try:
                body.close()
            except Exception:
                logger.debug("Unable to cancel Runtime response body", exc_info=True)
        reader.join(timeout=1)


def _invoke_runtime_buffered(
    messages: list[dict],
    runtime_arn: str,
    session_id: str,
    user_id: str,
) -> _BufferedRuntimeResult:
    """Collect a Runtime SSE response for an OpenAI non-streaming request."""
    parts = []
    usage = None
    agent_steps = []
    for event in _stream_runtime_events(messages, runtime_arn, session_id, user_id):
        if isinstance(event, _RuntimeUsage):
            usage = event
        elif isinstance(event, _RuntimeHeartbeat):
            continue
        elif isinstance(event, _RuntimeStatus):
            agent_steps.append(event.as_dict())
            parts.append(_format_runtime_status(event))
        else:
            parts.append(event)
    return _BufferedRuntimeResult("".join(parts), usage, agent_steps)


class _RuntimeStatus:
    """Validated status metadata received from a Runtime sideband SSE event."""

    __slots__ = ("details", "kind", "name", "status", "step_id")

    def __init__(
        self,
        kind: str,
        name: str,
        status: str,
        step_id: str = "",
        details: dict | None = None,
    ):
        self.kind = kind
        self.name = name
        self.status = status
        self.step_id = step_id
        self.details = details or {}

    def as_dict(self) -> dict:
        step = {
            "type": self.kind,
            "name": self.name,
            "status": self.status,
        }
        if self.step_id:
            step["id"] = self.step_id
        if self.details:
            step["details"] = self.details
        return step


def _runtime_step_details(value) -> dict:
    """Validate and independently bound untrusted Runtime detail metadata."""
    if not isinstance(value, dict):
        return {}
    try:
        rendered = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return {}
    if len(rendered) <= RUNTIME_STEP_DETAIL_MAX_CHARS:
        return json.loads(rendered)
    return {
        "preview": rendered[:RUNTIME_STEP_DETAIL_MAX_CHARS],
        "original_chars": len(rendered),
        "truncated": True,
    }


def _runtime_status(step) -> _RuntimeStatus | None:
    if not isinstance(step, dict):
        return None
    kind = step.get("type")
    status = step.get("status")
    name = step.get("name")
    if kind not in {"skill", "tool"} or status not in {
        "started",
        "completed",
        "failed",
    }:
        return None
    if not isinstance(name, str):
        return None
    name = _STATUS_NAME_UNSAFE_RE.sub("", " ".join(name.split())).strip()[:120]
    if not name:
        return None
    step_id = step.get("id")
    if isinstance(step_id, str):
        step_id = _STATUS_ID_UNSAFE_RE.sub("", step_id).strip()[:200]
    else:
        step_id = ""
    return _RuntimeStatus(
        kind,
        name,
        status,
        step_id,
        _runtime_step_details(step.get("details")),
    )


def _format_runtime_status(event: _RuntimeStatus) -> str:
    """Render the summary and, only when present, details that survive Dify."""
    label = "Skill" if event.kind == "skill" else "Tool"
    state = {
        "started": "running",
        "completed": "completed",
        "failed": "failed",
    }[event.status]
    summary = f"> **{label}:** `{event.name}` - {state}\n\n"
    if not event.details:
        return summary
    encoded_step = base64.b64encode(
        json.dumps(
            event.as_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).decode("ascii")
    return summary + f"<!--agentcore-step:{encoded_step}-->\n\n"


class _ArtifactStreamSanitizer:
    """Remove a possibly chunk-split artifact marker from model-visible text."""

    def __init__(self):
        self._pending = ""
        self._collecting = False
        self._discarding = False
        self._artifact_json = ""
        self.artifacts: list[dict] | None = None
        self.artifact_problem = False

    def _start_discarding(self) -> None:
        self._artifact_json = ""
        self._collecting = False
        self._discarding = True
        self.artifact_problem = True

    def feed(self, text: str) -> list[str]:
        if not isinstance(text, str) or not text:
            return []

        output: list[str] = []
        remaining = self._pending + text
        self._pending = ""
        while remaining:
            if self._discarding:
                end_index = remaining.find(_ARTIFACT_END)
                if end_index < 0:
                    return output
                self._discarding = False
                remaining = remaining[end_index + len(_ARTIFACT_END) :]
                continue

            if self._collecting:
                end_index = remaining.find(_ARTIFACT_END)
                if end_index < 0:
                    self._artifact_json += remaining
                    if (
                        len(self._artifact_json.encode("utf-8"))
                        > MAX_ARTIFACT_MARKER_BYTES
                    ):
                        self._start_discarding()
                    return output

                self._artifact_json += remaining[:end_index]
                remaining = remaining[end_index + len(_ARTIFACT_END) :]
                raw_json = self._artifact_json
                self._artifact_json = ""
                self._collecting = False
                try:
                    if (
                        len(raw_json.encode("utf-8"))
                        > MAX_ARTIFACT_MARKER_BYTES
                    ):
                        raise ValueError("artifact payload exceeds marker limit")
                    artifacts = json.loads(raw_json)
                    if not isinstance(artifacts, list):
                        raise ValueError("artifact payload must be a list")
                    self.artifacts = artifacts
                except (ValueError, TypeError, json.JSONDecodeError):
                    self.artifact_problem = True
                continue

            start_index = remaining.find(_ARTIFACT_START)
            if start_index >= 0:
                if start_index:
                    output.append(remaining[:start_index])
                remaining = remaining[start_index + len(_ARTIFACT_START) :]
                self._collecting = True
                self._artifact_json = ""
                continue

            pending_length = 0
            max_length = min(len(remaining), len(_ARTIFACT_START) - 1)
            for length in range(max_length, 0, -1):
                if _ARTIFACT_START.startswith(remaining[-length:]):
                    pending_length = length
                    break
            if pending_length:
                output.append(remaining[:-pending_length])
                self._pending = remaining[-pending_length:]
            else:
                output.append(remaining)
            return output
        return output

    def finish(self) -> list[str]:
        if self._collecting or self._discarding or self._pending:
            self._collecting = False
            self._discarding = False
            self._artifact_json = ""
            self._pending = ""
            self.artifact_problem = True
        return []


def _validate_dify_artifacts(
    artifacts,
    user_id: str,
    conversation_id: str,
    source_profile: dict,
) -> list[dict]:
    if not isinstance(artifacts, list) or not artifacts:
        raise DifyArtifactError(
            400,
            "invalid_artifact_manifest",
            "artifacts must be a non-empty list",
        )
    if len(artifacts) > MAX_ARTIFACTS_PER_RESPONSE:
        raise DifyArtifactError(
            400,
            "too_many_artifacts",
            f"At most {MAX_ARTIFACTS_PER_RESPONSE} generated files are allowed",
        )

    expected_prefix = (
        f"{source_profile['output_prefix']}{user_id}/{conversation_id}/"
    )
    validated: list[dict] = []
    seen_keys: set[str] = set()
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise DifyArtifactError(
                400,
                "invalid_artifact_manifest",
                "Each artifact must be an object",
            )

        s3_uri = str(artifact.get("s3_uri") or "").strip()
        filename = _sanitize_filename(str(artifact.get("filename") or ""))
        extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if not s3_uri or filename == "unnamed":
            raise DifyArtifactError(
                400,
                "invalid_artifact_manifest",
                "Each artifact requires s3_uri and filename",
            )
        if extension not in source_profile["output_extensions"]:
            raise DifyArtifactError(
                400,
                "unsupported_artifact_type",
                "Generated file type is not allowed",
            )

        parsed = urlparse(s3_uri)
        bucket = parsed.netloc
        key = parsed.path.lstrip("/")
        if (
            parsed.scheme != "s3"
            or bucket != source_profile["bucket"]
            or not key.startswith(expected_prefix)
            or key in seen_keys
        ):
            raise DifyArtifactError(
                403,
                "artifact_not_accessible",
                "Generated file is unavailable",
            )
        seen_keys.add(key)

        try:
            listing = get_s3_client().list_objects_v2(
                Bucket=bucket,
                Prefix=key,
                MaxKeys=1,
            )
            tag_response = get_s3_client().get_object_tagging(
                Bucket=bucket,
                Key=key,
            )
        except botocore.exceptions.ClientError as error:
            raise DifyArtifactError(
                502,
                "artifact_validation_failed",
                "Could not validate generated file",
            ) from error

        exact_object = next(
            (
                item
                for item in listing.get("Contents", [])
                if isinstance(item, dict) and item.get("Key") == key
            ),
            None,
        )
        tags = {
            item.get("Key"): item.get("Value")
            for item in tag_response.get("TagSet", [])
            if isinstance(item, dict)
        }
        object_size = exact_object.get("Size") if exact_object else None
        if (
            not isinstance(object_size, int)
            or object_size <= 0
            or object_size > MAX_ARTIFACT_BYTES
            or tags.get("Dify-User-Id") != user_id
            or tags.get("Dify-Conversation-Id") != conversation_id
            or tags.get("AgentCore-Artifact") != "generated"
        ):
            raise DifyArtifactError(
                403,
                "artifact_not_accessible",
                "Generated file is unavailable",
            )

        mime_type, _ = mimetypes.guess_type(filename)
        validated.append(
            {
                "s3_uri": s3_uri,
                "filename": filename,
                "mime_type": mime_type or "application/octet-stream",
                "size": object_size,
            }
        )
    return validated


def _discover_dify_artifacts(
    user_id: str,
    conversation_id: str,
    source_profile: dict,
    request_started_at: float,
) -> list[dict]:
    prefix = (
        f"{source_profile['output_prefix']}{user_id}/{conversation_id}/"
    )
    listing = get_s3_client().list_objects_v2(
        Bucket=source_profile["bucket"],
        Prefix=prefix,
        MaxKeys=100,
    )
    cutoff = request_started_at - 15
    candidates: list[tuple[float, dict]] = []
    for item in listing.get("Contents", []):
        if not isinstance(item, dict):
            continue
        key = item.get("Key")
        last_modified = item.get("LastModified")
        if not isinstance(key, str) or not key.startswith(prefix):
            continue
        try:
            modified_at = float(last_modified.timestamp())
        except (AttributeError, TypeError, ValueError, OverflowError):
            continue
        filename = _sanitize_filename(key.rsplit("/", 1)[-1])
        extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if (
            modified_at >= cutoff
            and extension in source_profile["output_extensions"]
        ):
            candidates.append(
                (
                    modified_at,
                    {
                        "s3_uri": f"s3://{source_profile['bucket']}/{key}",
                        "filename": filename,
                    },
                )
            )

    candidates.sort(key=lambda entry: entry[0], reverse=True)
    if not candidates:
        return []
    return _validate_dify_artifacts(
        [entry[1] for entry in candidates[:MAX_ARTIFACTS_PER_RESPONSE]],
        user_id,
        conversation_id,
        source_profile,
    )


def _resolve_artifacts(
    sanitizer: _ArtifactStreamSanitizer,
    artifact_context: tuple[str, str, dict],
    request_started_at: float,
) -> list[dict]:
    user_id, conversation_id, source_profile = artifact_context
    if sanitizer.artifacts is not None:
        validated = _validate_dify_artifacts(
            sanitizer.artifacts,
            user_id,
            conversation_id,
            source_profile,
        )
    else:
        validated = _discover_dify_artifacts(
            user_id,
            conversation_id,
            source_profile,
            request_started_at,
        )
    return validated if validated else []


def _format_artifact_references(artifacts: list[dict]) -> str:
    """Render a machine-readable artifact handoff for the frontend."""
    payload = {
        "files": [
            {
                "filename": artifact["filename"],
                "s3_uri": artifact["s3_uri"],
            }
            for artifact in artifacts
        ]
    }
    return (
        "\n<agentcore-generated-files>"
        f"{json.dumps(payload, separators=(',', ':'))}"
        "</agentcore-generated-files>"
    )


def _presign_validated_artifacts(artifacts: list[dict]) -> list[dict]:
    """Create short-lived download URLs only for already validated artifacts."""
    signed: list[dict] = []
    for artifact in artifacts:
        parsed = urlparse(artifact["s3_uri"])
        filename = _sanitize_filename(artifact["filename"])
        url = get_s3_client().generate_presigned_url(
            "get_object",
            Params={
                "Bucket": parsed.netloc,
                "Key": parsed.path.lstrip("/"),
                "ResponseContentDisposition": f'attachment; filename="{filename}"',
                "ResponseContentType": artifact["mime_type"],
            },
            ExpiresIn=DIFY_ARTIFACT_URL_TTL_SECONDS,
        )
        signed.append({**artifact, "download_url": url})
    return signed


def _format_presigned_artifact_links(artifacts: list[dict]) -> str:
    lines = ["", "Generated files:"]
    for artifact in artifacts:
        lines.append(f"- [{artifact['filename']}]({artifact['download_url']})")
    lines.append(
        f"Links expire in {DIFY_ARTIFACT_URL_TTL_SECONDS // 60} minutes."
    )
    return "\n".join(lines)


def _format_artifacts(artifacts: list[dict], output_urls: bool) -> str:
    if output_urls:
        return _format_presigned_artifact_links(
            _presign_validated_artifacts(artifacts)
        )
    return _format_artifact_references(artifacts)


def _artifact_error_label(error: Exception) -> str:
    if isinstance(error, DifyArtifactError):
        return f"{type(error).__name__}:{error.code}"
    return type(error).__name__


def _render_buffered_result(
    result_text: str,
    artifact_context: tuple[str, str, dict],
    request_started_at: float,
    output_urls: bool = False,
) -> str:
    sanitizer = _ArtifactStreamSanitizer()
    clean_text = "".join(sanitizer.feed(result_text))
    clean_text += "".join(sanitizer.finish())
    try:
        artifacts = _resolve_artifacts(
            sanitizer,
            artifact_context,
            request_started_at,
        )
    except Exception as error:
        logger.warning(
            "Dify artifact delivery failed: %s",
            _artifact_error_label(error),
        )
        return clean_text + _ARTIFACT_ERROR_TEXT
    if artifacts:
        try:
            return clean_text + "\n" + _format_artifacts(artifacts, output_urls)
        except Exception as error:
            logger.warning(
                "Dify artifact delivery failed: %s",
                _artifact_error_label(error),
            )
            return clean_text + _ARTIFACT_ERROR_TEXT
    if sanitizer.artifact_problem:
        return clean_text + _ARTIFACT_ERROR_TEXT
    return clean_text


def _sse_artifact_stream(
    events,
    backend_type: str,
    session_id: str,
    model: str,
    completion_id: str,
    artifact_context: tuple[str, str, dict],
    output_urls: bool = False,
):
    sanitizer = _ArtifactStreamSanitizer()
    request_started_at = time.time()
    usage = None
    stream_failed = False

    def stream_chunk(content: str, agent_step: dict | None = None) -> str:
        chunk = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": content},
                    "finish_reason": None,
                }
            ],
        }
        # OpenAI clients ignore unknown top-level extensions. Direct consumers
        # can use this structured field, while the hidden content marker below
        # carries the same data through Dify's text-only model-provider layer.
        if agent_step is not None:
            chunk["agent_step"] = agent_step
        return f"data: {json.dumps(chunk)}\n\n"

    try:
        for item in events:
            if isinstance(item, _RuntimeUsage):
                usage = item
                continue
            if isinstance(item, _RuntimeHeartbeat):
                yield ": keep-alive\n\n"
                continue
            if isinstance(item, _RuntimeStatus):
                yield stream_chunk(_format_runtime_status(item), item.as_dict())
                continue
            for content in sanitizer.feed(item):
                if content:
                    yield stream_chunk(content)
    except Exception as error:
        logger.error(
            "Dify %s stream error (session=%s): %s",
            backend_type,
            session_id,
            error,
        )
        stream_failed = True
        usage = None
        yield stream_chunk(_RUNTIME_INTERRUPTED_TEXT)

    sanitizer.finish()
    if not stream_failed:
        try:
            artifacts = _resolve_artifacts(
                sanitizer,
                artifact_context,
                request_started_at,
            )
            if artifacts:
                yield stream_chunk("\n" + _format_artifacts(artifacts, output_urls))
            elif sanitizer.artifact_problem:
                yield stream_chunk(_ARTIFACT_ERROR_TEXT)
        except Exception as error:
            logger.warning(
                "Dify artifact stream delivery failed (session=%s): %s",
                session_id,
                _artifact_error_label(error),
            )
            yield stream_chunk(_ARTIFACT_ERROR_TEXT)

    final = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    yield f"data: {json.dumps(final)}\n\n"
    if usage is not None:
        usage_chunk = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "model": model,
            "choices": [],
            "usage": usage.as_openai(),
        }
        yield f"data: {json.dumps(usage_chunk)}\n\n"
    logger.info(
        "Dify %s stream finalized (session=%s, interrupted=%s)",
        backend_type,
        session_id,
        stream_failed,
    )
    yield "data: [DONE]\n\n"


async def _build_completion(
    messages: list[dict],
    backend_type: str,
    backend_arn: str,
    slug: str,
    model: str,
    stream: bool,
    session_id: str,
    user_id: str,
    output_urls: bool = False,
):
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    logger.info(
        "Dify request [%s/%s]: model=%s turns=%d stream=%s session=%s actor=%s",
        backend_type,
        slug,
        model,
        len(messages),
        stream,
        session_id,
        user_id,
    )

    artifact_context = (
        user_id,
        session_id,
        DIFY_OFFICE_SOURCE_PROFILE,
    )
    request_started_at = time.time()
    usage = None
    agent_steps = []
    if backend_type == "runtime":
        if stream:
            return StreamingResponse(
                _sse_artifact_stream(
                    _stream_runtime_events(
                        messages,
                        backend_arn,
                        session_id,
                        user_id,
                    ),
                    backend_type,
                    session_id,
                    model,
                    completion_id,
                    artifact_context,
                    output_urls,
                ),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        runtime_result = await run_in_threadpool(
            _invoke_runtime_buffered,
            messages,
            backend_arn,
            session_id,
            user_id,
        )
        result_text = runtime_result.text
        usage = runtime_result.usage
        agent_steps = runtime_result.agent_steps
    else:
        if stream:
            return StreamingResponse(
                _sse_artifact_stream(
                    _stream_harness_events(
                        messages,
                        backend_arn,
                        session_id,
                        user_id,
                    ),
                    backend_type,
                    session_id,
                    model,
                    completion_id,
                    artifact_context,
                    output_urls,
                ),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        result_text = await run_in_threadpool(
            lambda: "".join(
                _stream_harness_events(
                    messages,
                    backend_arn,
                    session_id,
                    user_id,
                )
            )
        )
    result_text = await run_in_threadpool(
        _render_buffered_result,
        result_text,
        artifact_context,
        request_started_at,
        output_urls,
    )
    completion = {
        "id": completion_id,
        "object": "chat.completion",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": result_text},
                "finish_reason": "stop",
            }
        ],
    }
    if usage is not None:
        completion["usage"] = usage.as_openai()
    if agent_steps:
        completion["agent_steps"] = agent_steps
    return completion


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/{slug}/v1/models")
def models_by_slug(slug: str):
    if get_dify_backend(slug) is None:
        return _error(404, "unknown_backend", f"Unknown Dify backend: {slug}")
    return {
        "object": "list",
        "data": [{"id": slug, "object": "model", "owned_by": "agentcore"}],
    }


@app.post("/{slug}/v1/chat/completions")
async def chat_completions_by_slug(slug: str, request: Request):
    backend = await run_in_threadpool(get_dify_backend, slug)
    if backend is None:
        return _error(404, "unknown_backend", f"Unknown Dify backend: {slug}")
    backend_type, backend_arn = backend

    try:
        body = await request.json()
    except Exception:
        return _error(400, "invalid_request", "invalid JSON body")
    if not isinstance(body, dict):
        return _error(400, "invalid_request", "JSON body must be an object")

    try:
        session_id, user_id, messages, output_urls = (
            _extract_dify_session_context(body)
        )
    except HTTPException as error:
        return JSONResponse(status_code=error.status_code, content=error.detail)

    messages = _inject_dify_artifact_context(
        messages,
        user_id,
        session_id,
        DIFY_OFFICE_SOURCE_PROFILE,
    )

    try:
        return await _build_completion(
            messages=messages,
            backend_type=backend_type,
            backend_arn=backend_arn,
            slug=slug,
            model=body.get("model", slug),
            stream=bool(body.get("stream", False)),
            session_id=session_id,
            user_id=user_id,
            output_urls=output_urls,
        )
    except Exception as error:
        logger.error("AgentCore error [%s]: %s", slug, error)
        return _error(502, "agentcore_error", str(error))

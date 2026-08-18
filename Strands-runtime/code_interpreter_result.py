"""Bounded semantic results for AgentCore Code Interpreter stream events."""

from __future__ import annotations

import json
import math
from collections import deque
from collections.abc import Iterable
from typing import Any


RESULT_MARKER = "AGENTCORE_RESULT_JSON="
RESULT_CONTRACT_VERSION = 1

_MAX_COLUMNS = 20
_MAX_METRICS = 20
_MAX_SAMPLE_ROWS = 30
_MAX_ROW_FIELDS = 20
_MAX_ARTIFACTS = 20
_MAX_WARNINGS = 20
_MAX_MARKER_BUFFER_CHARS = 64_000
_PREVIEW_HEAD_CHARS = 2_000
_PREVIEW_TAIL_CHARS = 4_000


def _json_default(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray)):
        return {"binaryBytes": len(value)}
    return str(value)


def render_legacy_events(events: Iterable[dict], max_chars: int) -> str:
    """Render the pre-v0.0.7 raw event list for emergency compatibility."""
    collected = [event.get("result", event) for event in events]
    rendered = json.dumps(collected, default=_json_default, separators=(",", ":"))
    if len(rendered) > max_chars:
        return rendered[:max_chars] + "\n[tool result truncated]"
    return rendered


def _bounded_text(value: Any, limit: int = 1_000) -> str:
    text = str(value if value is not None else "")
    if len(text) <= limit:
        return text
    if limit < 16:
        return text[:limit]
    return text[: limit - 15] + "…[truncated]"


def _scalar(value: Any, *, string_limit: int = 500) -> str | int | float | bool | None:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return _bounded_text(value, string_limit)
    return _bounded_text(value, string_limit)


class _TextPreview:
    """Retain useful text without retaining a whole interpreter stream."""

    def __init__(self) -> None:
        self._head = ""
        self._tail: deque[str] = deque()
        self._tail_length = 0

    def add(self, text: str) -> None:
        if not text:
            return
        if len(self._head) < _PREVIEW_HEAD_CHARS:
            self._head += text[: _PREVIEW_HEAD_CHARS - len(self._head)]
        tail_text = text[-_MAX_MARKER_BUFFER_CHARS:]
        self._tail.append(tail_text)
        self._tail_length += len(tail_text)
        while self._tail and self._tail_length > _MAX_MARKER_BUFFER_CHARS:
            removed = self._tail.popleft()
            self._tail_length -= len(removed)

    def marker_text(self) -> str:
        return "".join(self._tail)

    def preview(self) -> str:
        tail = self.marker_text()
        if len(tail) > _PREVIEW_TAIL_CHARS:
            tail = tail[-_PREVIEW_TAIL_CHARS:]
        if not self._head:
            return tail
        if not tail or tail in self._head:
            return self._head
        return self._head + "\n…[output omitted]…\n" + tail


def _event_texts(result: dict[str, Any]) -> list[str]:
    texts: list[str] = []
    content = result.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, str):
                texts.append(block)
            elif isinstance(block, dict):
                for key in ("text", "stdout", "stderr", "output"):
                    value = block.get(key)
                    if isinstance(value, str):
                        texts.append(value)
    elif isinstance(content, str):
        texts.append(content)

    for key in ("text", "stdout", "stderr", "output", "message", "error"):
        value = result.get(key)
        if isinstance(value, str):
            texts.append(value)
    return texts


def _exit_code(result: dict[str, Any]) -> int | None:
    structured = result.get("structuredContent")
    if not isinstance(structured, dict):
        return None
    value = structured.get("exitCode")
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _last_declared_contract(text: str) -> dict[str, Any] | None:
    decoder = json.JSONDecoder()
    position = 0
    latest: dict[str, Any] | None = None
    while True:
        marker_at = text.find(RESULT_MARKER, position)
        if marker_at < 0:
            return latest
        start = marker_at + len(RESULT_MARKER)
        try:
            candidate, end = decoder.raw_decode(text[start:].lstrip())
        except json.JSONDecodeError:
            position = start
            continue
        if isinstance(candidate, dict):
            latest = candidate
        position = start + end


def _sanitize_declared(contract: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(contract.get("ok"), bool):
        return None
    summary = contract.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        return None

    sanitized: dict[str, Any] = {
        "contract_version": RESULT_CONTRACT_VERSION,
        "source": "declared",
        "ok": contract["ok"],
        "summary": _bounded_text(summary.strip(), 2_000),
    }
    row_count = contract.get("row_count")
    if isinstance(row_count, int) and not isinstance(row_count, bool) and row_count >= 0:
        sanitized["row_count"] = row_count

    columns = contract.get("columns")
    if isinstance(columns, list):
        cleaned_columns = [
            _bounded_text(column, 200)
            for column in columns[:_MAX_COLUMNS]
            if isinstance(column, (str, int, float)) and not isinstance(column, bool)
        ]
        if cleaned_columns:
            sanitized["columns"] = cleaned_columns

    metrics = contract.get("metrics")
    if isinstance(metrics, dict):
        cleaned_metrics: dict[str, str | int | float | bool | None] = {}
        for key, value in metrics.items():
            if len(cleaned_metrics) >= _MAX_METRICS:
                break
            if not isinstance(key, str):
                continue
            cleaned_metrics[_bounded_text(key, 160)] = _scalar(value)
        if cleaned_metrics:
            sanitized["metrics"] = cleaned_metrics

    rows = contract.get("sample_rows")
    if isinstance(rows, list):
        cleaned_rows: list[dict[str, str | int | float | bool | None]] = []
        for row in rows:
            if len(cleaned_rows) >= _MAX_SAMPLE_ROWS or not isinstance(row, dict):
                break
            cleaned_row: dict[str, str | int | float | bool | None] = {}
            for key, value in row.items():
                if len(cleaned_row) >= _MAX_ROW_FIELDS:
                    break
                if isinstance(key, str):
                    cleaned_row[_bounded_text(key, 160)] = _scalar(value)
            if cleaned_row:
                cleaned_rows.append(cleaned_row)
        if cleaned_rows:
            sanitized["sample_rows"] = cleaned_rows

    artifacts = contract.get("artifacts")
    if isinstance(artifacts, list):
        cleaned_artifacts: list[dict[str, str]] = []
        for artifact in artifacts:
            if len(cleaned_artifacts) >= _MAX_ARTIFACTS or not isinstance(artifact, dict):
                break
            clean = {
                key: _bounded_text(artifact[key], 1_000)
                for key in ("s3_uri", "filename", "content_type")
                if isinstance(artifact.get(key), str) and artifact[key].strip()
            }
            if clean:
                cleaned_artifacts.append(clean)
        if cleaned_artifacts:
            sanitized["artifacts"] = cleaned_artifacts

    warnings = contract.get("warnings")
    if isinstance(warnings, list):
        cleaned_warnings = [
            _bounded_text(warning, 500)
            for warning in warnings[:_MAX_WARNINGS]
            if isinstance(warning, (str, int, float)) and not isinstance(warning, bool)
        ]
        if cleaned_warnings:
            sanitized["warnings"] = cleaned_warnings

    error = contract.get("error")
    if error is not None:
        sanitized["error"] = _bounded_text(error, 2_000)
    return sanitized


def _error_contract(preview: str, exit_code: int | None) -> dict[str, Any]:
    contract: dict[str, Any] = {
        "contract_version": RESULT_CONTRACT_VERSION,
        "source": "fallback",
        "ok": False,
        "summary": "Code Interpreter execution failed.",
        "error": _bounded_text(preview or "Code Interpreter reported an error.", 2_000),
    }
    if exit_code is not None:
        contract["exit_code"] = exit_code
    return contract


def _fallback_contract(preview: str, failed: bool, exit_code: int | None) -> dict[str, Any]:
    if failed:
        return _error_contract(preview, exit_code)
    contract: dict[str, Any] = {
        "contract_version": RESULT_CONTRACT_VERSION,
        "source": "fallback",
        "ok": True,
        "summary": "Code Interpreter completed without a declared semantic result.",
        "warnings": ["The program did not emit AGENTCORE_RESULT_JSON."],
    }
    if preview:
        contract["stdout_preview"] = _bounded_text(preview, 6_200)
    if exit_code is not None:
        contract["exit_code"] = exit_code
    return contract


def _serialize_bounded(contract: dict[str, Any], max_chars: int) -> str:
    limit = max(96, int(max_chars))
    candidate = dict(contract)

    def render() -> str:
        return json.dumps(candidate, allow_nan=False, separators=(",", ":"))

    rendered = render()
    for key in ("summary", "error"):
        value = candidate.get(key)
        if not isinstance(value, str):
            continue
        while len(rendered) > limit and len(value) > 160:
            value = _bounded_text(value, max(160, len(value) // 2))
            candidate[key] = value
            rendered = render()

    for key in ("sample_rows", "artifacts", "warnings", "columns"):
        value = candidate.get(key)
        while len(rendered) > limit and isinstance(value, list) and len(value) > 1:
            value.pop()
            rendered = render()

    metrics = candidate.get("metrics")
    while len(rendered) > limit and isinstance(metrics, dict) and len(metrics) > 1:
        metrics.pop(next(reversed(metrics)))
        rendered = render()

    for key in (
        "stdout_preview",
        "sample_rows",
        "artifacts",
        "metrics",
        "columns",
        "warnings",
        "row_count",
        "exit_code",
    ):
        if len(rendered) <= limit:
            return rendered
        candidate.pop(key, None)
        rendered = render()

    for key in ("error", "summary"):
        value = candidate.get(key)
        if isinstance(value, str):
            while len(rendered) > limit and len(value) > 1:
                value = _bounded_text(value, max(1, len(value) // 2))
                candidate[key] = value
                rendered = render()

    if len(rendered) <= limit:
        return rendered
    compact = {
        "contract_version": RESULT_CONTRACT_VERSION,
        "source": candidate.get("source", "fallback"),
        "ok": bool(candidate.get("ok")),
        "summary": "Result truncated.",
    }
    return json.dumps(compact, separators=(",", ":"))


def render_semantic_events(events: Iterable[dict], max_chars: int = 10_000) -> str:
    """Consume AgentCore result events and return one bounded JSON contract."""
    preview = _TextPreview()
    failed = False
    exit_code: int | None = None
    for event in events:
        result = event.get("result", event) if isinstance(event, dict) else event
        if not isinstance(result, dict):
            preview.add(_bounded_text(result, _MAX_MARKER_BUFFER_CHARS))
            continue
        for text in _event_texts(result):
            preview.add(text)
        event_exit_code = _exit_code(result)
        if event_exit_code is not None:
            exit_code = event_exit_code
            failed = failed or event_exit_code != 0
        failed = failed or result.get("isError") is True

    contract = None if failed else _sanitize_declared(_last_declared_contract(preview.marker_text()) or {})
    if contract is None:
        contract = _fallback_contract(preview.preview(), failed, exit_code)
    return _serialize_bounded(contract, max_chars)


def render_runtime_error(error: Exception | str, max_chars: int = 10_000) -> str:
    """Return a safe semantic contract when invoking Code Interpreter fails."""
    return _serialize_bounded(
        {
            "contract_version": RESULT_CONTRACT_VERSION,
            "source": "runtime_error",
            "ok": False,
            "summary": "Code Interpreter invocation failed.",
            "error": _bounded_text(error, 2_000),
        },
        max_chars,
    )


def result_is_error(rendered: str) -> bool:
    """Recognize failures in either semantic object or legacy event-list output."""
    try:
        parsed = json.loads(rendered)
    except (TypeError, json.JSONDecodeError):
        return True
    if isinstance(parsed, dict):
        return parsed.get("ok") is not True
    if not isinstance(parsed, list) or not parsed:
        return True
    for result in parsed:
        if not isinstance(result, dict):
            continue
        if result.get("isError") is True:
            return True
        exit_code = _exit_code(result)
        if exit_code not in {None, 0}:
            return True
    return False

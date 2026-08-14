"""Decode and validate agent-step markers from a Dify response capture."""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

MARKER_RE = re.compile(r"<!--agentcore-step:([A-Za-z0-9+/=]+)-->")
VALID_TYPES = {"skill", "tool"}
VALID_STATUSES = {"started", "completed", "failed"}
TERMINAL_STATUSES = {"completed", "failed"}


def decode_markers(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Decode every agentcore-step marker and return steps plus decode errors."""
    steps: list[dict[str, Any]] = []
    errors: list[str] = []

    for index, match in enumerate(MARKER_RE.finditer(text), start=1):
        try:
            decoded = base64.b64decode(match.group(1), validate=True).decode("utf-8")
            step = json.loads(decoded)
        except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as error:
            errors.append(f"Marker {index}: cannot decode valid UTF-8 JSON: {error}")
            continue
        if not isinstance(step, dict):
            errors.append(f"Marker {index}: decoded value must be a JSON object")
            continue
        steps.append(step)

    if not steps and not errors:
        errors.append("No <!--agentcore-step:...--> markers found")
    return steps, errors


def validate_steps(
    steps: list[dict[str, Any]], *, require_details: bool = True
) -> list[str]:
    """Validate fields, detail output, and lifecycle pairing."""
    errors: list[str] = []
    lifecycle: dict[str, list[str]] = defaultdict(list)

    for index, step in enumerate(steps, start=1):
        prefix = f"Step {index}"
        step_id = step.get("id")
        kind = step.get("type")
        name = step.get("name")
        status = step.get("status")
        details = step.get("details")

        if not isinstance(step_id, str) or not step_id:
            errors.append(f"{prefix}: missing non-empty string 'id'")
        else:
            lifecycle[step_id].append(status if isinstance(status, str) else "")
        if kind not in VALID_TYPES:
            errors.append(f"{prefix}: 'type' must be one of {sorted(VALID_TYPES)}")
        if not isinstance(name, str) or not name:
            errors.append(f"{prefix}: missing non-empty string 'name'")
        if status not in VALID_STATUSES:
            errors.append(
                f"{prefix}: 'status' must be one of {sorted(VALID_STATUSES)}"
            )
        if details is not None and not isinstance(details, dict):
            errors.append(f"{prefix}: 'details' must be a JSON object when present")
            continue
        if require_details and status in TERMINAL_STATUSES and (
            not isinstance(details, dict) or "output" not in details
        ):
            errors.append(
                f"{prefix}: terminal event is missing 'details.output'; "
                "the frontend did not receive the tool result"
            )

    for step_id, statuses in lifecycle.items():
        if "started" not in statuses:
            errors.append(f"Tool use {step_id}: missing started event")
        if not any(status in TERMINAL_STATUSES for status in statuses):
            errors.append(f"Tool use {step_id}: missing completed/failed event")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Decode and validate Dify agentcore-step response markers."
    )
    parser.add_argument(
        "capture",
        nargs="?",
        type=Path,
        default=Path(__file__).with_name("test-result.txt"),
        help="Response capture file (default: test-result.txt beside this script)",
    )
    parser.add_argument(
        "--status-only",
        action="store_true",
        help="Validate lifecycle fields without requiring terminal details.output",
    )
    args = parser.parse_args()

    try:
        text = args.capture.read_text(encoding="utf-8-sig")
    except OSError as error:
        print(f"FAIL: cannot read {args.capture}: {error}")
        return 2

    steps, errors = decode_markers(text)
    errors.extend(validate_steps(steps, require_details=not args.status_only))

    print(f"Decoded {len(steps)} agent step marker(s) from {args.capture}")
    for index, step in enumerate(steps, start=1):
        print(f"\nStep {index}:")
        print(json.dumps(step, ensure_ascii=False, indent=2))

    if errors:
        print("\nValidation: FAIL")
        for error in errors:
            print(f"- {error}")
        return 1

    print("\nValidation: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

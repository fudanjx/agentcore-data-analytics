#!/usr/bin/env python3
"""Safely add the Office provider to an existing Insights Compose deployment."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path


OLD_BASE_URL = (
    "      OPENAI_API_BASE_URLS: "
    "http://k8s-agentcor-agentcor-a9dbd8956e-c923dee5a7cceccb."
    "elb.ap-southeast-1.amazonaws.com/insights/v1\n"
)
NEW_BASE_URL = (
    "      OPENAI_API_BASE_URLS: >-\n"
    "        http://k8s-agentcor-agentcor-a9dbd8956e-c923dee5a7cceccb."
    "elb.ap-southeast-1.amazonaws.com/insights/v1;\n"
    "        http://k8s-agentcor-agentcor-a9dbd8956e-c923dee5a7cceccb."
    "elb.ap-southeast-1.amazonaws.com/insights-office/v1\n"
)
OLD_KEYS = "      OPENAI_API_KEYS: private-vpc-poc\n"
NEW_KEYS = "      OPENAI_API_KEYS: private-vpc-poc;private-vpc-poc\n"
OLD_CONFIG = (
    "        {\"0\":{\"enable\":true,\"prefix_id\":\"agentcore\","
    "\"model_ids\":[\"insights\"],\"tags\":[\"agentcore-test\"],"
    "\"connection_type\":\"external\"}}\n"
)
NEW_CONFIG = (
    "        {\"0\":{\"enable\":true,\"prefix_id\":\"agentcore\","
    "\"model_ids\":[\"insights\"],\"tags\":[\"agentcore-test\"],"
    "\"connection_type\":\"external\"},\"1\":{\"enable\":true,"
    "\"prefix_id\":\"agentcore-office\",\"model_ids\":[\"insights-office\"],"
    "\"tags\":[\"agentcore-office\"],\"connection_type\":\"external\"}}\n"
)


def replace_once_or_verify(source: str, old: str, new: str, label: str) -> str:
    if new in source:
        return source
    if old not in source:
        raise ValueError(f"Could not find expected {label} configuration")
    return source.replace(old, new, 1)


def update_compose(compose: Path) -> Path | None:
    original = compose.read_text()
    candidate = replace_once_or_verify(
        original, OLD_BASE_URL, NEW_BASE_URL, "OpenAI base URL"
    )
    candidate = replace_once_or_verify(candidate, OLD_KEYS, NEW_KEYS, "OpenAI API key")
    candidate = replace_once_or_verify(
        candidate, OLD_CONFIG, NEW_CONFIG, "OpenAI provider config"
    )
    if candidate == original:
        return None

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = compose.with_name(f"{compose.name}.pre-office-{timestamp}")
    shutil.copy2(compose, backup)
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yml", dir=compose.parent, delete=False
    ) as temporary:
        temporary.write(candidate)
        candidate_path = Path(temporary.name)
    try:
        subprocess.run(
            ["docker", "compose", "-f", str(candidate_path), "config", "--quiet"],
            cwd=compose.parent,
            check=True,
        )
        candidate_path.replace(compose)
    except Exception:
        candidate_path.unlink(missing_ok=True)
        raise
    return backup


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--compose", type=Path, required=True)
    parser.add_argument("--function", type=Path, required=True)
    parser.add_argument("--target-function", type=Path, required=True)
    args = parser.parse_args()

    backup = update_compose(args.compose)
    args.target_function.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(args.function, args.target_function)
    print(f"compose_backup={backup}" if backup else "compose_already_updated=true")
    print(f"filter_updated={args.target_function}")


if __name__ == "__main__":
    main()

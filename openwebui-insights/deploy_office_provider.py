#!/usr/bin/env python3
"""Safely configure the AgentCore runtime providers in an Insights deployment."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path


LEGACY_BASE_URL = (
    "      OPENAI_API_BASE_URLS: "
    "http://k8s-agentcor-agentcor-a9dbd8956e-c923dee5a7cceccb."
    "elb.ap-southeast-1.amazonaws.com/insights/v1\n"
)
OFFICE_BASE_URL = (
    "      OPENAI_API_BASE_URLS: >-\n"
    "        http://k8s-agentcor-agentcor-a9dbd8956e-c923dee5a7cceccb."
    "elb.ap-southeast-1.amazonaws.com/insights/v1;\n"
    "        http://k8s-agentcor-agentcor-a9dbd8956e-c923dee5a7cceccb."
    "elb.ap-southeast-1.amazonaws.com/insights-office/v1\n"
)
RUNTIME_BASE_URLS = (
    "      OPENAI_API_BASE_URLS: >-\n"
    "        http://k8s-agentcor-agentcor-a9dbd8956e-c923dee5a7cceccb."
    "elb.ap-southeast-1.amazonaws.com/insights/v1;\n"
    "        http://k8s-agentcor-agentcor-a9dbd8956e-c923dee5a7cceccb."
    "elb.ap-southeast-1.amazonaws.com/strands/v1;\n"
    "        http://k8s-agentcor-agentcor-a9dbd8956e-c923dee5a7cceccb."
    "elb.ap-southeast-1.amazonaws.com/insights-office/v1;\n"
    "        http://k8s-agentcor-agentcor-a9dbd8956e-c923dee5a7cceccb."
    "elb.ap-southeast-1.amazonaws.com/gmio-pcr-dev/v1\n"
)
LEGACY_KEYS = "      OPENAI_API_KEYS: private-vpc-poc\n"
OFFICE_KEYS = "      OPENAI_API_KEYS: private-vpc-poc;private-vpc-poc\n"
RUNTIME_KEYS = (
    "      OPENAI_API_KEYS: "
    "private-vpc-poc;private-vpc-poc;private-vpc-poc;private-vpc-poc\n"
)
LEGACY_CONFIG = (
    "        {\"0\":{\"enable\":true,\"prefix_id\":\"agentcore\","
    "\"model_ids\":[\"insights\"],\"tags\":[\"agentcore-test\"],"
    "\"connection_type\":\"external\"}}\n"
)
OFFICE_CONFIG = (
    "        {\"0\":{\"enable\":true,\"prefix_id\":\"agentcore\","
    "\"model_ids\":[\"insights\"],\"tags\":[\"agentcore-test\"],"
    "\"connection_type\":\"external\"},\"1\":{\"enable\":true,"
    "\"prefix_id\":\"agentcore-office\",\"model_ids\":[\"insights-office\"],"
    "\"tags\":[\"agentcore-office\"],\"connection_type\":\"external\"}}\n"
)
RUNTIME_CONFIG = (
    "        {\"0\":{\"enable\":true,\"prefix_id\":\"agentcore\","
    "\"model_ids\":[],\"tags\":[\"agentcore-legacy\"],"
    "\"connection_type\":\"external\"},\"1\":{\"enable\":true,"
    "\"prefix_id\":\"agentcore-strands\",\"model_ids\":[],"
    "\"tags\":[\"agentcore\"],\"connection_type\":\"external\"},"
    "\"2\":{\"enable\":true,\"prefix_id\":\"agentcore-office\","
    "\"model_ids\":[],\"tags\":[\"agentcore\"],"
    "\"connection_type\":\"external\"},\"3\":{\"enable\":true,"
    "\"prefix_id\":\"agentcore-gmio\",\"model_ids\":[],"
    "\"tags\":[\"agentcore\"],\"connection_type\":\"external\"}}\n"
)


def replace_any_or_verify(
    source: str, old_values: tuple[str, ...], new: str, label: str
) -> str:
    if new in source:
        return source
    for old in old_values:
        if old in source:
            return source.replace(old, new, 1)
    raise ValueError(f"Could not find expected {label} configuration")


def update_compose(compose: Path) -> Path | None:
    original = compose.read_text()
    candidate = replace_any_or_verify(
        original,
        (LEGACY_BASE_URL, OFFICE_BASE_URL),
        RUNTIME_BASE_URLS,
        "OpenAI base URL",
    )
    candidate = replace_any_or_verify(
        candidate, (LEGACY_KEYS, OFFICE_KEYS), RUNTIME_KEYS, "OpenAI API key"
    )
    candidate = replace_any_or_verify(
        candidate,
        (LEGACY_CONFIG, OFFICE_CONFIG),
        RUNTIME_CONFIG,
        "OpenAI provider config",
    )
    if candidate == original:
        return None

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = compose.with_name(f"{compose.name}.pre-runtime-router-{timestamp}")
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

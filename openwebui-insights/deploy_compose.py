#!/usr/bin/env python3
"""Add the Insights service to an existing Compose file after validation."""

import argparse
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path


SERVICE_MARKER = "\n  open-webui-insights:\n"
UPLOAD_PROXY_MARKER = "\n  open-webui-insights-upload-proxy:\n"
VOLUME_MARKER = "\n  open-webui-insights-data:\n"
TOP_LEVEL_VOLUMES = "\nvolumes:\n"
DIRECT_PORT_MAPPING = '    ports:\n      - "3001:8080"\n'


def _service_end(compose: str, start: int) -> int:
    """Return the next top-level service or volumes boundary."""
    current_line_end = compose.find("\n", start + 1)
    search_start = current_line_end + 1 if current_line_end != -1 else len(compose)
    next_service_match = re.search(
        r"^  [^ \n][^\n]*:\n", compose[search_start:], re.MULTILINE
    )
    next_service = (
        search_start + next_service_match.start() if next_service_match else -1
    )
    next_volumes = compose.find(TOP_LEVEL_VOLUMES, start + 1)
    candidates = [position for position in (next_service, next_volumes) if position != -1]
    return min(candidates) if candidates else len(compose)


def _upload_proxy_service(fragment: str) -> str:
    start = fragment.index(UPLOAD_PROXY_MARKER)
    end = _service_end(fragment, start)
    return fragment[start:end]


def add_upload_proxy(existing: str, fragment: str) -> str:
    """Migrate an already-deployed Insights service behind the upload proxy."""
    if UPLOAD_PROXY_MARKER in existing:
        return existing
    if SERVICE_MARKER not in existing:
        raise ValueError("OpenWebUI Insights service is not present")

    start = existing.index(SERVICE_MARKER)
    end = _service_end(existing, start)
    insights_service = existing[start:end]
    if DIRECT_PORT_MAPPING not in insights_service:
        raise ValueError("Expected the direct Insights port mapping before proxy migration")

    migrated_service = insights_service.replace(DIRECT_PORT_MAPPING, "", 1)
    return (
        existing[:start]
        + _upload_proxy_service(fragment)
        + migrated_service
        + existing[end:]
    )


def merge_compose(existing: str, fragment: str) -> str:
    if SERVICE_MARKER in existing:
        return add_upload_proxy(existing, fragment)
    if TOP_LEVEL_VOLUMES not in existing or TOP_LEVEL_VOLUMES not in fragment:
        raise ValueError("Both Compose documents require top-level services and volumes")

    fragment_services, fragment_volumes = fragment.split(TOP_LEVEL_VOLUMES, 1)
    service_body = fragment_services.removeprefix("services:\n")
    volume_body = fragment_volumes

    before_volumes, existing_volumes = existing.split(TOP_LEVEL_VOLUMES, 1)
    return (
        before_volumes.rstrip()
        + "\n"
        + service_body.rstrip()
        + TOP_LEVEL_VOLUMES
        + volume_body.rstrip()
        + "\n"
        + existing_volumes.lstrip("\n")
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--compose", type=Path, required=True)
    parser.add_argument("--fragment", type=Path, required=True)
    args = parser.parse_args()

    original = args.compose.read_text()
    merged = merge_compose(original, args.fragment.read_text())
    if merged == original:
        print("open-webui-insights already present; no Compose change")
        return

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = args.compose.with_name(f"{args.compose.name}.pre-insights-{timestamp}")
    shutil.copy2(args.compose, backup)

    with tempfile.NamedTemporaryFile(
        mode="w",
        prefix="compose-insights-",
        suffix=".yml",
        dir=args.compose.parent,
        delete=False,
    ) as candidate_file:
        candidate_file.write(merged)
        candidate = Path(candidate_file.name)

    try:
        subprocess.run(
            ["docker", "compose", "-f", str(candidate), "config", "--quiet"],
            cwd=args.compose.parent,
            check=True,
        )
        candidate.replace(args.compose)
    except Exception:
        candidate.unlink(missing_ok=True)
        raise

    print(f"Compose updated; backup={backup}")


if __name__ == "__main__":
    main()

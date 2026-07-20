#!/usr/bin/env python3
"""Add the Insights service to an existing Compose file after validation."""

import argparse
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path


SERVICE_MARKER = "\n  open-webui-insights:\n"
VOLUME_MARKER = "\n  open-webui-insights-data:\n"
TOP_LEVEL_VOLUMES = "\nvolumes:\n"


def merge_compose(existing: str, fragment: str) -> str:
    if SERVICE_MARKER in existing:
        return existing
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

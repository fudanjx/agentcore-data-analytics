#!/usr/bin/env bash
# Build an ARM64/Linux Python 3.13 AgentCore source bundle on macOS or Linux.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUTPUT_PATH="${1:-$ROOT/dist/strands_runtime_ds_v0.0.1.zip}"
PLATFORM="${PLATFORM:-linux/arm64/v8}"
PYTHON_IMAGE="${PYTHON_IMAGE:-python:3.13-slim-bookworm}"
STAGING="$(mktemp -d "$ROOT/.bundle-build.XXXXXX")"
trap 'rm -rf "$STAGING"' EXIT

if [[ "$OUTPUT_PATH" != /* ]]; then
  OUTPUT_PATH="$PWD/$OUTPUT_PATH"
fi

for file in agent.py deepseek_openai.py code_interpreter.py code_interpreter_result.py gateway_config.py gateway_proxy.py main.py memory.py requirements.txt skills_sync.py system_prompt.py; do
  [[ -f "$ROOT/$file" ]] || { echo "Missing runtime source: $file" >&2; exit 1; }
done
command -v docker >/dev/null || { echo "Docker Desktop is required" >&2; exit 1; }
docker info >/dev/null

mkdir -p "$(dirname "$OUTPUT_PATH")" "$STAGING/strands_agent"
docker run --rm --platform "$PLATFORM" \
  --mount "type=bind,source=$ROOT,target=/src,readonly" \
  --mount "type=bind,source=$STAGING/strands_agent,target=/bundle" \
  "$PYTHON_IMAGE" sh -lc \
  'python -m pip install --disable-pip-version-check --no-cache-dir --target /bundle -r /src/requirements.txt'

for file in agent.py deepseek_openai.py code_interpreter.py code_interpreter_result.py gateway_config.py gateway_proxy.py main.py memory.py requirements.txt skills_sync.py system_prompt.py; do
  cp "$ROOT/$file" "$STAGING/strands_agent/$file"
done

# Source deployments do not need pip's precompiled bytecode. Removing it keeps
# the artifact smaller and avoids shipping generated cache directories.
find "$STAGING/strands_agent" -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete
find "$STAGING/strands_agent" -depth -type d -name '__pycache__' -empty -delete
find "$STAGING/strands_agent" -depth -type f -path '*/tests/*' -delete
find "$STAGING/strands_agent" -depth -type d -path '*/tests/*' -empty -delete
find "$STAGING/strands_agent" -depth -type d -name 'tests' -empty -delete

# `zip` updates an existing archive in place, which would retain stale entries
# removed from the new staging tree. Always create this exact output afresh.
rm -f "$OUTPUT_PATH"
(cd "$STAGING" && zip -qr "$OUTPUT_PATH" strands_agent)
echo "Bundle created: $OUTPUT_PATH"
shasum -a 256 "$OUTPUT_PATH"
unzip -t "$OUTPUT_PATH" >/dev/null
echo "AgentCore entry point: strands_agent/main.py"

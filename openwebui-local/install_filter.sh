#!/usr/bin/env bash
set -euo pipefail

docker exec \
  agentcore-openwebui-test \
  sh -c '
    export WEBUI_SECRET_KEY="$(cat /app/backend/.webui_secret_key)"
    export PYTHONPATH=/app/backend
    exec python /opt/agentcore-openwebui/install_filter.py
  '

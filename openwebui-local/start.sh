#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib.sh"
load_config
"${SCRIPT_DIR}/ensure_tailscale_route.sh"
assume_openwebui_role

wait_for_openwebui() {
  local state
  for _ in {1..60}; do
    state="$(
      docker inspect \
        --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
        agentcore-openwebui-test 2>/dev/null || true
    )"
    if [[ "${state}" == "healthy" ]]; then
      return 0
    fi
    sleep 2
  done
  echo "OpenWebUI did not become healthy within 120 seconds." >&2
  return 1
}

docker compose \
  --project-directory "${SCRIPT_DIR}" \
  --env-file "${SCRIPT_DIR}/.env" \
  -f "${SCRIPT_DIR}/docker-compose.yml" \
  up -d

wait_for_openwebui
"${SCRIPT_DIR}/install_filter.sh"
docker restart agentcore-openwebui-test >/dev/null
wait_for_openwebui

echo "OpenWebUI is starting at http://localhost:${OPENWEBUI_PORT}"
echo "AgentCore model traffic uses the private Tailscale relay at 100.79.116.60:18080."

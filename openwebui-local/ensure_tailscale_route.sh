#!/usr/bin/env bash
set -euo pipefail

RELAY_IP="${AGENTCORE_RELAY_IP:-100.79.116.60}"
RELAY_PORT="${AGENTCORE_RELAY_PORT:-18080}"

current_interface="$(
  route -n get "${RELAY_IP}" 2>/dev/null |
    awk '/interface:/{print $2; exit}'
)"

if [[ "${current_interface}" != utun* ]]; then
  tailscale_interface="$(
    route -n get 100.100.100.100 2>/dev/null |
      awk '/interface:/{print $2; exit}'
  )"

  if [[ "${tailscale_interface}" != utun* ]]; then
    echo "Tailscale's macOS tunnel interface could not be found." >&2
    echo "Connect Tailscale, then run this script again." >&2
    exit 1
  fi

  echo "Routing only ${RELAY_IP} through Tailscale (${tailscale_interface})."
  echo "macOS may request your administrator password."
  sudo route -n delete -host "${RELAY_IP}" >/dev/null 2>&1 || true
  sudo route -n add -host "${RELAY_IP}" -interface "${tailscale_interface}"
fi

curl -fsS \
  --connect-timeout 5 \
  --max-time 15 \
  "http://${RELAY_IP}:${RELAY_PORT}/health" >/dev/null

echo "AgentCore relay is reachable through Tailscale at ${RELAY_IP}:${RELAY_PORT}."

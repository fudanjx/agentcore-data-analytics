#!/usr/bin/env bash
set -euo pipefail

echo "Fallback only: forwarding local port 18080 to the private AgentCore proxy."
echo "The normal OpenWebUI path uses the EC2 Caddy relay over Tailscale."
echo "Keep this terminal open if you are using this fallback."
kubectl -n agentcore port-forward service/agentcore-proxy 18080:80 \
  --address 127.0.0.1

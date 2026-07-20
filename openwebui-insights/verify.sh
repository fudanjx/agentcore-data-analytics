#!/usr/bin/env bash
set -euo pipefail

docker compose ps open-webui-insights
curl -fsS --max-time 10 http://127.0.0.1:3001/health
docker inspect open-webui-insights \
  --format 'image={{.Config.Image}} health={{.State.Health.Status}}'

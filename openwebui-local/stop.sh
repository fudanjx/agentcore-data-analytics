#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
docker compose \
  --project-directory "${SCRIPT_DIR}" \
  --env-file "${SCRIPT_DIR}/.env" \
  -f "${SCRIPT_DIR}/docker-compose.yml" \
  down

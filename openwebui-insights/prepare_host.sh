#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/home/ubuntu/app"
INSIGHTS_DIR="${APP_DIR}/insights"
BACKUP_DIR="${INSIGHTS_DIR}/backups"
ENV_FILE="${INSIGHTS_DIR}/.env"
ADMIN_FILE="${INSIGHTS_DIR}/admin-bootstrap.env"
DB_NAME="openwebui_insights"
DB_USER="openwebui_insights"

sudo mkdir -p "${BACKUP_DIR}"
sudo chown -R ubuntu:ubuntu "${INSIGHTS_DIR}"

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
sudo docker exec openwebui-postgres \
  pg_dump -U webui -d openwebui -Fc \
  > "${BACKUP_DIR}/openwebui-pre-insights-${timestamp}.dump"
chmod 600 "${BACKUP_DIR}/openwebui-pre-insights-${timestamp}.dump"

if ! sudo swapon --show=NAME --noheadings | grep -q '^/swapfile$'; then
  if [[ ! -f /swapfile ]]; then
    sudo fallocate -l 2G /swapfile
    sudo chmod 600 /swapfile
    sudo mkswap /swapfile >/dev/null
  fi
  sudo swapon /swapfile
fi
if ! grep -q '^/swapfile ' /etc/fstab; then
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi
echo 'vm.swappiness=10' | sudo tee /etc/sysctl.d/99-insights-swap.conf >/dev/null
sudo sysctl -q -p /etc/sysctl.d/99-insights-swap.conf

if [[ ! -f "${ENV_FILE}" ]]; then
  db_password="$(openssl rand -hex 24)"
  webui_secret="$(openssl rand -hex 32)"

  if sudo docker exec openwebui-postgres \
    psql -U webui -d postgres -Atc \
      "select 1 from pg_roles where rolname='${DB_USER}'" |
      grep -qx '1'; then
    sudo docker exec openwebui-postgres \
      psql -U webui -d postgres -v ON_ERROR_STOP=1 -c \
      "alter role ${DB_USER} with login password '${db_password}'" >/dev/null
  else
    sudo docker exec openwebui-postgres \
      psql -U webui -d postgres -v ON_ERROR_STOP=1 -c \
      "create role ${DB_USER} with login password '${db_password}'" >/dev/null
  fi

  if ! sudo docker exec openwebui-postgres \
    psql -U webui -d postgres -Atc \
      "select 1 from pg_database where datname='${DB_NAME}'" |
      grep -qx '1'; then
    sudo docker exec openwebui-postgres \
      createdb -U webui -O "${DB_USER}" "${DB_NAME}"
  fi

  umask 077
  {
    echo "DATABASE_URL=postgresql://${DB_USER}:${db_password}@postgres:5432/${DB_NAME}"
    echo "WEBUI_SECRET_KEY=${webui_secret}"
  } > "${ENV_FILE}"
fi
chmod 600 "${ENV_FILE}"

if [[ ! -f "${ADMIN_FILE}" ]]; then
  admin_email="$(
    sudo docker exec openwebui-postgres \
      psql -U webui -d openwebui -Atc \
      "select email from \"user\" where role='admin' order by created_at limit 1"
  )"
  if [[ -z "${admin_email}" ]]; then
    echo "Existing OpenWebUI administrator email was not found." >&2
    exit 1
  fi
  admin_password="$(openssl rand -hex 16)"
  umask 077
  {
    echo "ADMIN_EMAIL=${admin_email}"
    echo "ADMIN_PASSWORD=${admin_password}"
    echo "ADMIN_NAME=Insights Administrator"
  } > "${ADMIN_FILE}"
fi
chmod 600 "${ADMIN_FILE}"

echo "host_prepared=true"
echo "database=${DB_NAME}"
echo "swap=$(sudo swapon --show=SIZE --noheadings /swapfile | xargs)"

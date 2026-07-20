#!/usr/bin/env bash

load_config() {
  local script_dir
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  set -a
  source "${script_dir}/.env"
  set +a
}

assume_openwebui_role() {
  local credentials
  credentials="$(AWS_REGION="${AWS_REGION}" aws sts assume-role \
    --role-arn "${AWS_ROLE_ARN}" \
    --role-session-name "openwebui-local-$(date +%s)" \
    --duration-seconds 3600 \
    --output json)"

  export AWS_ACCESS_KEY_ID
  export AWS_SECRET_ACCESS_KEY
  export AWS_SESSION_TOKEN
  AWS_ACCESS_KEY_ID="$(jq -r '.Credentials.AccessKeyId' <<<"${credentials}")"
  AWS_SECRET_ACCESS_KEY="$(jq -r '.Credentials.SecretAccessKey' <<<"${credentials}")"
  AWS_SESSION_TOKEN="$(jq -r '.Credentials.SessionToken' <<<"${credentials}")"
  export AWS_REGION AWS_DEFAULT_REGION="${AWS_REGION}"
}

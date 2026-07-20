# OpenWebUI Insights

This additive deployment runs OpenWebUI `v0.10.2-slim` alongside the existing
EC2 Compose services. It uses its own PostgreSQL database, Docker volume, S3
bucket, target group, hostname, OpenWebUI users, AgentCore actor namespace and
AgentCore sessions.

The deployed site is `https://insights.bot-alex.com`. The ALB forwards only
that host name to EC2 port `3001`; the EC2 security group accepts that port
only from the ALB security group.

The existing `open-webui`, `litellm`, `postgres`, and `autoheal` services are
not recreated during deployment. Start or update only the new service with:

```bash
docker compose up -d --no-deps open-webui-insights
```

The service is memory-limited, bypasses local embedding and retrieval, disables
Ollama, and uses the private AgentCore `/insights/v1` endpoint.

Uploaded S3 objects expire after seven days. New users receive the `pending`
role until an administrator approves them.

OpenWebUI automatically disables signup when it promotes the first account to
administrator. After bootstrapping that administrator, restart only
`open-webui-insights` once. With `ENABLE_PERSISTENT_CONFIG=false`, the restart
re-applies `ENABLE_SIGNUP=true` and `DEFAULT_USER_ROLE=pending` from Compose.

## File and identity handoff

The global `agentcore_file_context` filter applies only to
`agentcore.insights`. It resolves files from the authenticated user's stored
chat, rejects an inaccessible chat or file, and forwards the owned manifest to
the private `/insights/v1` proxy route. The proxy independently validates the
S3 bucket, prefix, object metadata, object tags, file id, and owner before
calling AgentCore.

Foreground AgentCore identities use separate namespaces:

- Actor: `openwebui-insights:<OpenWebUI user UUID>`
- Session: `owui-insights-<user UUID>-<chat UUID>`

The POC trusts `X-OpenWebUI-User-Id` and `X-OpenWebUI-Chat-Id` because
OpenWebUI-to-proxy traffic stays on the private VPC route. Before any
internet-facing or cross-trust deployment, replace these plain headers with a
short-lived signed JWT validated by the proxy.

The AgentCore Code Interpreter remains in `SANDBOX` mode. It downloads a
validated object with its execution role by running:

```bash
aws s3 cp "$S3_URI" "/tmp/$FILENAME" \
  --region ap-southeast-1 --only-show-errors
```

It must not try public HTTP, an OpenWebUI API URL, or `pandas`/`s3fs` directly
against the S3 URI.

## Verification

```bash
./insights/verify.sh
python3 /home/ubuntu/app/insights/e2e_smoke.py
python3 /home/ubuntu/app/insights/pending_role_smoke.py
```

The end-to-end smoke test logs in with the protected bootstrap file, uploads a
small CSV, creates a chat, waits for the asynchronous AgentCore response, and
requires `E2E_SUM=6`. The pending-role test creates a temporary account,
requires the `pending` role, and deletes the account immediately.

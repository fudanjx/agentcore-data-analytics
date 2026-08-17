# OpenWebUI Insights

This additive deployment runs OpenWebUI `v0.10.2-slim` alongside the existing
EC2 Compose services. It uses its own PostgreSQL database, Docker volume, S3
bucket, target group, hostname, OpenWebUI users, AgentCore actor namespace and
AgentCore sessions.

The deployed site is `https://insights.bot-alex.com`. The ALB forwards only
that host name to EC2 port `3001`; the EC2 security group accepts that port
only from the ALB security group. A small Caddy sidecar owns that host port and
passes traffic to `open-webui-insights` over the Compose network.

The existing `open-webui`, `litellm`, `postgres`, and `autoheal` services are
not recreated during deployment. Start or update only the new service with:

```bash
docker compose up -d --no-deps open-webui-insights
```

The service is memory-limited, bypasses local embedding and retrieval, disables
Ollama, and uses the private `/strands/v1`, `/insights-office/v1`, and
`/gmio-pcr-dev/v1` AgentCore endpoints. `/insights/v1` remains a temporary
compatibility provider for existing chats.

### Upload processing bypass

OpenWebUI v0.10.2's browser normally appends `process=true` to chat-upload
requests, which starts OpenWebUI's own extraction/RAG background job. Insights
does not use that job: its file filter forwards only the owned S3 manifest to
AgentCore, whose Code Interpreter performs analysis on demand. The Caddy
sidecar therefore changes only `POST /api/v1/files[/]` to `process=false`.
Every other request, query value, request body, and response is proxied without
modification. This prevents the browser from waiting for OpenWebUI's unrelated
file-processing status stream while retaining normal drag-and-drop uploads.

Uploaded S3 objects expire after seven days. New users receive the `pending`
role until an administrator approves them.

OpenWebUI automatically disables signup when it promotes the first account to
administrator. After bootstrapping that administrator, restart only
`open-webui-insights` once. With `ENABLE_PERSISTENT_CONFIG=false`, the restart
re-applies `ENABLE_SIGNUP=true` and `DEFAULT_USER_ROLE=pending` from Compose.

## File and identity handoff

The global `agentcore_file_context` filter applies to Strands, Insights Office,
GMIO PCR Dev, and the temporary Insights alias. It resolves files from the
authenticated user's stored chat, rejects an inaccessible chat or file, and
forwards the owned manifest to the selected private proxy route. The proxy
independently validates the S3 bucket, prefix, object metadata, object tags,
file id, and owner before calling AgentCore.

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

Each agent may generate CSV, DOCX, XLSX, PPTX, PDF, or HTML files under the
current user/chat output prefix. The filter validates each candidate through
`/{slug}/v1/artifacts/register`, creates an owner-scoped OpenWebUI File row,
and renders `/api/v1/files/<id>/content?attachment=true`. HTML is therefore
downloaded rather than executed in the OpenWebUI origin.

Structured runtime tool events are emitted individually as transient native
OpenWebUI statuses. The filter never displays tool arguments or raw results,
and the final stream chunk closes any remaining status.

## Verification

```bash
./insights/verify.sh
python3 /home/ubuntu/app/insights/e2e_smoke.py --verify-upload-bypass
python3 /home/ubuntu/app/insights/e2e_smoke.py
python3 /home/ubuntu/app/insights/pending_role_smoke.py
```

The end-to-end smoke test logs in with the protected bootstrap file, uploads a
small CSV, creates a chat, waits for the asynchronous AgentCore response, and
requires `E2E_SUM=6`. The pending-role test creates a temporary account,
requires the `pending` role, and deletes the account immediately.

`--verify-upload-bypass` sends a >3 MiB CSV to the normal browser-style
`?process=true` endpoint and requires the response to have no pending
OpenWebUI processing status. It validates the Caddy rewrite without invoking
AgentCore.

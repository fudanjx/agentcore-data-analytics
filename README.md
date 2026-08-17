# AgentCore Data Analytics Platform

A production-grade multi-tool agent platform on AWS AgentCore. Answers natural-language analytical questions against two PostgreSQL databases (`nuh-analytics`, `ah-analytics`), and forecasts future values using Google TimesFM. The main EKS proxy exposes an OpenAI-compatible API exclusively to OpenWebUI over private networking. Dify uses the independent `dify-proxy/` service.

## Architecture

```
OpenWebUI v0.10.2
        │  /{slug}/v1/chat/completions
        │  X-OpenWebUI-User-Id + X-OpenWebUI-Chat-Id
        ▼
EKS Fargate: agentcore-proxy      (amd64, namespace: agentcore)
        │  ConfigMap slug registry + selected AgentCore invocation ← IRSA
        │  streams text and individual safe tool lifecycle events
        ▼
AgentCore backends               (ap-southeast-1, private VPC)
        │
        ├─ /strands         → Strands_runtime-mk6uFHBu9d
        ├─ /insights-office → harness_harness_insights_office-trvSEWAuyj
        └─ /gmio-pcr-dev    → gmio_pcr_dev-gSuIMZ4u60
```

## OpenWebUI Insights test frontend

`https://insights.bot-alex.com` is an isolated OpenWebUI v0.10.2-slim test
deployment on the existing EC2 host. It runs alongside the legacy OpenWebUI
service, with a separate Docker volume and PostgreSQL database
(`openwebui_insights`), so no user or configuration migration is required.

It has three canonical private NLB provider routes:

```
Browser → ALB → open-webui-insights → private NLB → agentcore-proxy
        ├─ /strands/v1
        ├─ /insights-office/v1
        └─ /gmio-pcr-dev/v1
```

All routes use the same identity/session mapping: the actor identity is
`openwebui-insights:<OpenWebUI user UUID>` and `runtimeSessionId` is
`owui-insights-<user UUID>-<chat UUID>`. Memory sharing between runtimes is
controlled by their AgentCore memory configuration; the proxy never mixes
identities between users.

### Insights file handoff

OpenWebUI stores uploads in
`s3://agentcore-openwebui-insights-964340114883/openwebui-insights/` using its
S3 storage provider. Uploads are server-mediated (browser → OpenWebUI → S3),
not presigned browser-direct PUTs. Local persistent file cache is disabled and
`BYPASS_EMBEDDING_AND_RETRIEVAL=true` prevents the normal RAG/embedding path.

The `agentcore_file_context` filter applies to all three AgentCore models and
the temporary `insights` compatibility alias. It:

1. requires an authenticated OpenWebUI user and owned chat;
2. finds files already attached anywhere in that chat;
3. retrieves each file through OpenWebUI's user-scoped lookup;
4. replaces raw file metadata with a compact `agentcore_files` manifest
   (`file_id`, `s3_uri`, filename, MIME type, size); and
5. removes raw `files` fields so the proxy never trusts browser-supplied file
   references.

Before invoking AgentCore, the proxy rejects any manifest object outside the
Insights bucket/prefix or whose S3 owner/file tags do not match the caller. It
then gives the validated URI to the Runtime. The agent can invoke AgentCore
Code Interpreter, which downloads it inside its sandbox, for example:

```bash
aws s3 cp "$S3_URI" "/tmp/$FILENAME" --region ap-southeast-1 --only-show-errors
```

The bucket is private, encrypted with SSE-S3, versioned, and expires current
objects after seven days. The proxy-side checks prevent cross-user manifest
sharing; the Code Interpreter role is still broadly scoped to the Insights
prefix for this POC, so object-scoped temporary authorization is a production
enhancement. Plain identity headers are likewise trusted only on the private
OpenWebUI-server → proxy hop; replace them with signed short-lived JWTs before
allowing untrusted callers on that path.

### Generated outputs, authenticated downloads, and work status

Every configured runtime receives the generated-output contract. Its Code
Interpreter may read validated Insights uploads and can write tagged DOCX,
XLSX, PPTX, PDF, CSV, and HTML outputs under
`openwebui-insights/outputs/<user UUID>/<chat UUID>/`; input uploads are never
overwritten.

The proxy validates the exact output prefix, S3 tags, extension, and size. The
OpenWebUI filter then registers each valid object as a File owned by the
requesting user and saves an authenticated
`/api/v1/files/<id>/content?attachment=true` download link in the chat. It
does not expose a raw S3 URL or browser AWS credentials. Links work while the
seven-day object lifecycle retains the file. HTML is always served with
`attachment=true`, never rendered in the OpenWebUI origin.

Each real runtime `agent_step` event is forwarded immediately as its own native
OpenWebUI status. Events are not grouped and the proxy does not invent a
“Preparing final answer” status. Tool inputs, results, credentials, and model
reasoning are excluded.

The OpenWebUI browser normally asks for `process=true`; the Insights Caddy
sidecar rewrites only `POST /api/v1/files[/]` to `process=false`. This enforces
the no-local-extraction behavior without changing the browser UI or any other
OpenWebUI endpoint.

## What's in this repo

| Path | Purpose |
|------|---------|
| `app/` | Agent container for `agentcore_poc` runtime — FastAPI, Claude Agent SDK, MCP `execute_sql` tool |
| `infra/deploy.py` | Provision the `agentcore_poc` Runtime + Endpoint |
| `infra/etl_nuh_analytics.py`, `infra/etl_ah_analytics.py` | ETL parquet → RDS (SAP + EPIC mixed date formats handled) |
| `infra/mask_pii.py` | Mask phone numbers and addresses |
| `mcp_lambda/handler.py` | Shared MCP handler — `execute_sql`, `list_tables`, `describe_table` |
| `mcp_lambda/deploy.py` | Deploy `nuh-analytics-mcp` Lambda + Gateway |
| `mcp_lambda/deploy_ah.py` | Deploy `ah-analytics-mcp` Lambda + Gateway, wire to harness |
| `timesfm_service/` | EKS pod running TimesFM 2.5-200m (CPU, model weights baked into image) |
| `timesfm_mcp/handler.py` | Bridge Lambda: Gateway MCP → HTTP POST to TimesFM NLB |
| `timesfm_mcp/deploy.py` | Provision the bridge Lambda + wire to `timesfm-gateway` + harness |
| `proxy/server.py` | Config-driven OpenWebUI-only FastAPI proxy with identity, S3 artifact, and runtime SSE handling |
| `proxy/k8s/runtime-routes-configmap.yaml` | Slug, display name, and AgentCore runtime ARN registry |
| `openwebui-insights/functions/agentcore_file_context.py` | OpenWebUI filter for owned file manifests, runtime statuses, and authenticated downloads |
| `infra/insights_office_bootstrap.py` | Converts the current Harness memory to shared BYO Memory and creates the Office Harness/sandbox |
| `infra/test_code_interpreter_office_output.py` | Direct tagged Office Code Interpreter output smoke test |
| `infra/user_uploads_bootstrap.py` | Creates the Insights upload bucket/policies/lifecycle and related IAM permissions |
| `infra/test_code_interpreter_s3.py` | Direct AgentCore Code Interpreter S3 download smoke test |
| `proxy/k8s/` | Deployment, ClusterIP + internal NLB service, IRSA ServiceAccount |
| `.claude/Skills/` | Agent Skills — data dictionary routing guide + per-table SQL guidance |
| `him_scripts/DATA_DICTIONARY.md` | Column semantics, filter rules, and inclusion criteria from HIM scripts |
| `README.md` | This file |
| `DEPLOY.md` | Step-by-step deployment guide (all components) |
| `REFLECTION.md` | Lessons learned |

## Quick Start

### Prerequisites

```bash
aws --version                # AWS CLI v2, ap-southeast-1 profile
docker --version             # Docker Desktop
kubectl version              # configured for ai-project EKS cluster
python3 -m pip install boto3
aws eks update-kubeconfig --region ap-southeast-1 --name ai-project
```

### Deploy paths

The four components deploy independently:

```bash
# 1. Agent container for the poc runtime (arm64)
bash infra/build_and_push.sh
python3 infra/deploy.py

# 2. EKS proxy (amd64)
bash proxy/build_and_push.sh
kubectl apply -f proxy/k8s/
kubectl rollout restart deployment/agentcore-proxy -n agentcore

# 3. MCP Lambdas (nuh-analytics + ah-analytics)
python3 mcp_lambda/deploy.py
python3 mcp_lambda/deploy_ah.py

# 4. TimesFM forecasting (EKS pod + bridge Lambda)
bash timesfm_service/build_and_push.sh
kubectl apply -f timesfm_service/k8s/
NLB=$(kubectl get svc timesfm-svc-internal -n agentcore \
  -o jsonpath='{.status.loadBalancer.ingress[0].hostname}')
NLB_ENDPOINT="http://${NLB}" python3 timesfm_mcp/deploy.py
```

See [DEPLOY.md](DEPLOY.md) for full step-by-step instructions.

### Data ETL

Two databases on the same RDS instance:

```bash
# Upload script to S3, run ECS task inside the VPC
aws s3 cp infra/etl_nuh_analytics.py s3://agentcore-tmp-964340114883/etl_nuh_analytics.py
aws ecs run-task --cluster embedded-web-app \
  --task-definition agentcore-nuh-etl:3 \
  --launch-type FARGATE \
  --network-configuration '{"awsvpcConfiguration":{"subnets":["subnet-061205c705e0f41d4"],"securityGroups":["sg-07258677b7e691e48"],"assignPublicIp":"DISABLED"}}'

aws s3 cp infra/etl_ah_analytics.py s3://agentcore-tmp-964340114883/etl_ah_analytics.py
aws ecs run-task --cluster embedded-web-app --task-definition agentcore-ah-etl:1 --launch-type FARGATE ...
```

## API Endpoints

The proxy exposes one OpenAI-compatible OpenWebUI API shape and selects an
AgentCore Runtime or managed Harness by slug.

### Slugs → backend

| Slug | Backend | AWS call |
|------|---------|----------|
| `/strands` | `Strands_runtime-mk6uFHBu9d` | `invoke_agent_runtime` |
| `/insights-office` | `harness_insights_office-NXyYkHT02U` | `invoke_harness` |
| `/gmio-pcr-dev` | `gmio_pcr_dev-gSuIMZ4u60` | `invoke_agent_runtime` |
| `/insights` | Temporary alias for `/strands` | `invoke_agent_runtime` |

Each slug provides `GET /{slug}/v1/models`,
`POST /{slug}/v1/chat/completions`, and
`POST /{slug}/v1/artifacts/register`. Root `/v1` is an alias for `/strands/v1`.

### From inside the EKS cluster

| Backend | Base URL (OpenAI shape) |
|---------|-------------------------|
| strands | `http://agentcore-proxy.agentcore.svc.cluster.local/strands/v1` |
| insights | `http://agentcore-proxy.agentcore.svc.cluster.local/insights/v1` |
| insights-office | `http://agentcore-proxy.agentcore.svc.cluster.local/insights-office/v1` |
| gmio-pcr-dev | `http://agentcore-proxy.agentcore.svc.cluster.local/gmio-pcr-dev/v1` |

### From VPC / peered VPC (Open WebUI)

Replace the cluster DNS with the internal NLB `k8s-agentcor-agentcor-a9dbd8956e-c923dee5a7cceccb.elb.ap-southeast-1.amazonaws.com`. All endpoints are VPC-internal, no auth (any Bearer token is accepted).

Every chat and artifact call requires `X-OpenWebUI-User-Id` and
`X-OpenWebUI-Chat-Id`. Unknown slugs return `404`.

### Direct boto3 (bypasses proxy)

```python
import boto3, json

client = boto3.client("bedrock-agentcore", region_name="ap-southeast-1")
resp = client.invoke_agent_runtime(
    agentRuntimeArn="arn:aws:bedrock-agentcore:ap-southeast-1:964340114883:runtime/agentcore_poc-iumXW8638m",
    contentType="application/json",
    accept="application/json",
    payload=json.dumps({"messages": [{"role": "user", "content": "how many rows in emd?"}]}).encode(),
)
print(json.loads(resp["response"].read())["result"])
```

## Session and Memory Wiring

The proxy carries session/user identity from the frontend into AgentCore. Same conversation → same runtime session → warm container reuse + memory namespace hits.

```
X-OpenWebUI-Chat-Id → namespaced runtimeSessionId
X-OpenWebUI-User-Id → namespaced runtimeUserId and payload identity
```

For the Insights deployment, the OpenWebUI filter supplies the trusted user and
chat values as private-hop headers, and the proxy deliberately namespaces both
values as described in [OpenWebUI Insights test frontend](#openwebui-insights-test-frontend).

AgentCore managed memory can use two strategies (configured on each runtime):
- **Semantic** — `/actors/{actorId}/facts/` — cross-session user facts. Extracted asynchronously (~30–60 s after the turn is saved).
- **Summarization** — `/actors/{actorId}/summaries/{sessionId}/` — per-conversation summaries.

## Streaming (SSE)

All configured backends stream when OpenWebUI asks for `stream=true`.

- Runtime OpenAI delta frames are forwarded token by token.
- Runtime `agent_step` frames become individual sanitized OpenWebUI statuses.
- A cold-start `ConnectionClosedError` before the first event is retried once.
- **Do not block the event loop** — the proxy wraps the blocking botocore stream iterator with `starlette.concurrency.iterate_in_threadpool` so tokens flush to the client as they arrive.

## Agent Tools (Harness Gateway Tools)

The `harness_e52fs` harness has three gateway-provided tools:

| Gateway | Tools | Backend |
|---------|-------|---------|
| `nuh-analytics-db` | `execute_sql`, `list_tables`, `describe_table` | Lambda `nuh-analytics-mcp` → RDS `nuh-analytics` |
| `ah-analytics-db` | `execute_sql`, `list_tables`, `describe_table` | Lambda `ah-analytics-mcp` → RDS `ah-analytics` |
| `timesfm-gateway` | `timesfm_forecast` | Lambda `timesfm-mcp` → NLB → EKS `timesfm-service` pod |

Strands SDK deduplicates tool names by prefixing with target name, so `ah-analytics-db` uses target `ah-rds-tools` (different from `nuh-analytics-db`'s `rds-tools`) to avoid collision.

## Key Design Decisions

| Decision | Choice | Reason |
|----------|--------|--------|
| Agent harness (poc) | `claude-agent-sdk` subprocess | Official SDK with MCP tool support |
| Agent harness (main) | AgentCore Harness (Strands) | Managed model + memory + tool wiring |
| LLM auth | `CLAUDE_CODE_USE_BEDROCK=1` + inference profile ARN | IAM only, no API keys |
| Bedrock model | Application inference profile in us-east-1 | On-demand requires inference profile |
| AgentCore network | VPC mode, private subnets | All traffic on AWS backbone, no internet |
| Agent container | `linux/arm64` | Required by AgentCore Runtime |
| Proxy / MCP Lambda / TimesFM | `linux/amd64` | EKS Fargate nodes are amd64 |
| Frontend auth | EKS proxy with IRSA | Frontends can't do SigV4; org SCP blocks anonymous Lambda URLs |
| Session/memory | `chat_id` → `runtimeSessionId`, `user_id` → `actorId` | Enables AgentCore memory + warm container reuse |
| TimesFM deployment | EKS pod + Lambda MCP bridge | Model too large for Lambda (250MB limit); NLB is HTTP so http.passthrough won't work |
| TimesFM weights | Baked into Docker image (~2GB) | Zero runtime HF dependency; air-gapped-friendly |
| ETL date parsing | Per-value multi-format parser | AH data mixes SAP-era (DD.MM.YYYY, D/M/YYYY) with EPIC-era (YYYY-MM-DD) in same columns |

## AWS Resource Reference

| Resource | Value |
|----------|-------|
| AgentCore poc runtime | `arn:aws:bedrock-agentcore:ap-southeast-1:964340114883:runtime/agentcore_poc-iumXW8638m` |
| Strands runtime | `arn:aws:bedrock-agentcore:ap-southeast-1:964340114883:runtime/Strands_runtime-mk6uFHBu9d` |
| Insights Office harness | `arn:aws:bedrock-agentcore:ap-southeast-1:964340114883:harness/harness_insights_office-NXyYkHT02U` |
| GMIO PCR development runtime | `arn:aws:bedrock-agentcore:ap-southeast-1:964340114883:runtime/gmio_pcr_dev-gSuIMZ4u60` |
| Inference profile | `arn:aws:bedrock:us-east-1:964340114883:application-inference-profile/ji5jakx5lho3` |
| RDS endpoint | `jinxin-postgres.cf7in3efovlt.ap-southeast-1.rds.amazonaws.com` |
| Secrets Manager | `arn:aws:secretsmanager:ap-southeast-1:964340114883:secret:agentcore-rds-credentials-tlv56J` |
| ECR agent image | `964340114883.dkr.ecr.ap-southeast-1.amazonaws.com/agentcore-poc:latest` (arm64) |
| ECR proxy image | `964340114883.dkr.ecr.ap-southeast-1.amazonaws.com/agentcore-proxy:latest` (amd64) |
| ECR timesfm image | `964340114883.dkr.ecr.ap-southeast-1.amazonaws.com/timesfm-service:latest` (amd64) |
| IRSA role | `arn:aws:iam::964340114883:role/agentcore-proxy-irsa` |
| Proxy internal NLB | `k8s-agentcor-agentcor-a9dbd8956e-c923dee5a7cceccb.elb.ap-southeast-1.amazonaws.com` |
| Insights public endpoint | `https://insights.bot-alex.com` |
| Insights upload bucket | `s3://agentcore-openwebui-insights-964340114883/openwebui-insights/` |
| Insights Code Interpreter | `agentcore_user_uploads_ci-iZOyjlk0GA` (SANDBOX) |
| Insights Office Code Interpreter | `agentcore_insights_office_ci-wNOyRxcsEC` (SANDBOX) |
| TimesFM internal NLB | `k8s-agentcor-timesfms-fb1729afc9-4eef87d9ac68417f.elb.ap-southeast-1.amazonaws.com` |
| MCP Gateways | `nuh-analytics-db-fhbzdmtdta`, `ah-analytics-db-gszih4adsx`, `timesfm-gateway-w4fho4r9um` |

## Replicating in a New AWS Account

The minimum path from zero to a working OpenWebUI endpoint. Each step is idempotent — safe to re-run.

**Pre-flight checklist (blockers if missing):**
1. **Bedrock model access** — request `anthropic.claude-*` model access in your target model region (usually `us-east-1`) and create an **application inference profile** pointing at it. AgentCore needs the profile ARN, not the bare model id.
2. **VPC with private + public subnets**, NAT for private egress, and these VPC Interface Endpoints in the private subnets:
   - `com.amazonaws.<region>.bedrock-runtime`
   - `com.amazonaws.<region>.bedrock-agentcore`
   - `com.amazonaws.<region>.bedrock-agentcore.gateway`  ← different from the one above; needed for Gateway MCP calls
   - `com.amazonaws.<region>.secretsmanager`
   - `com.amazonaws.<region>.ecr.api` and `com.amazonaws.<region>.ecr.dkr`
3. **Tag your private subnets** with `kubernetes.io/role/internal-elb = 1` — internal NLBs won't provision without this.
4. **EKS cluster with a Fargate profile** that covers the `agentcore` namespace (wildcard is fine). Note the OIDC provider ARN — IRSA needs it.
5. **RDS PostgreSQL** in the same VPC, private subnets only (`PubliclyAccessible: false`). Credentials in Secrets Manager.
6. **ECR repos**: `agentcore-poc`, `agentcore-proxy`, `timesfm-service`. `aws ecr create-repository` — one per component.
7. **Confirm no org SCP blocks** `AuthType: NONE` on Lambda Function URLs (we don't use them, but check). Confirm `bedrock-agentcore:*` actions are allowed.

**Then, in this order:**

```bash
# ── 1. Update account/region constants ────────────────────────────
# Search & replace 964340114883 → your account id; ap-southeast-1 → your region
# Files to edit: infra/deploy.py, proxy/server.py, mcp_lambda/*.py, timesfm_mcp/deploy.py
grep -rn "964340114883\|ap-southeast-1" infra/ proxy/ mcp_lambda/ timesfm_mcp/ app/

# ── 2. Data plane: ETL + Skills ───────────────────────────────────
# Load your parquet/CSV data into RDS via an ECS Fargate ETL task inside the VPC.
# See infra/etl_nuh_analytics.py for the pattern.
aws s3 sync .claude/Skills/ s3://<your-skills-bucket>/skills/

# ── 3. MCP Lambdas (one per database) ─────────────────────────────
python3 mcp_lambda/deploy.py     # nuh-analytics-mcp + gateway
python3 mcp_lambda/deploy_ah.py  # ah-analytics-mcp  + gateway

# ── 4. TimesFM forecasting (optional) ─────────────────────────────
bash timesfm_service/build_and_push.sh
kubectl apply -f timesfm_service/k8s/
NLB=$(kubectl get svc timesfm-svc-internal -n agentcore -o jsonpath='{.status.loadBalancer.ingress[0].hostname}')
NLB_ENDPOINT="http://${NLB}" python3 timesfm_mcp/deploy.py

# ── 5. AgentCore Runtime (poc) ────────────────────────────────────
bash infra/build_and_push.sh       # arm64 image → ECR
export ECR_IMAGE_URI=<account>.dkr.ecr.<region>.amazonaws.com/agentcore-poc:latest
python3 infra/deploy.py            # creates IAM role + runtime + endpoint

# ── 6. AgentCore runtimes ─────────────────────────────────────────
# Create/configure each Runtime with its gateways, memory, skills and tools.

# ── 7. Proxy (single service, all OpenWebUI runtimes) ─────────────
# Edit proxy/k8s/runtime-routes-configmap.yaml with slug/name/runtime ARN.
bash proxy/build_and_push.sh        # amd64 image → ECR
kubectl apply -f proxy/k8s/         # deployment + service + IRSA SA
```

**Post-deploy verification:**
```bash
curl -sS http://<nlb>/strands/v1/models | jq
```

**Best-practice recap (from real pitfalls in this build):**
- Split architectures: AgentCore Runtime containers are `linux/arm64`; everything on EKS Fargate is `linux/amd64`. Always pass `--platform` to `docker build`.
- Runtime names: `[a-zA-Z][a-zA-Z0-9_]{0,47}` — underscores only, no hyphens.
- Gateway target names must be globally unique across a single harness (tool names are formed as `{target}___{tool}`).
- Every new gateway added to a harness needs THREE things: (a) `update_harness(tools=...)`, (b) add the gateway ARN to `AmazonBedrockAgentCoreHarnessGatewayPolicy_*`, (c) restart/reload.
- Skills path in the harness config is a **plain repo path** like `.claude/Skills`, not a URL fragment like `tree/main/.claude/Skills`.
- IAM propagation for VPC Lambdas: `time.sleep(15–20)` after `put_role_policy` before `create_function`.
- Set `CLAUDE_CODE_USE_BEDROCK=1` via `ClaudeAgentOptions(env=...)`, NOT container env — the SDK spawns a subprocess with its own env.
- `invoke_agent_runtime` IAM policies: use `Resource: "*"` — the check is against the endpoint ARN, not the runtime ARN.
- Change agents by updating the runtime ConfigMap and rolling out the proxy; an image rebuild is not required.

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| AgentCore health check fails | `/ping` returning 404 | Ensure `@app.get("/ping")` exists in `app/main.py` |
| `Not logged in · Please run /login` | Subprocess not using Bedrock | Set `CLAUDE_CODE_USE_BEDROCK=1` in `ClaudeAgentOptions(env=...)` |
| `400 Invocation of model not supported` | Bare model ID | Use application inference profile ARN as model |
| `AccessDeniedException: InvokeAgentRuntime` | Policy scoped to runtime ARN | Use `Resource: "*"` — IAM check uses endpoint ARN |
| Proxy pod crash-looping | Blocking boto3 call on event loop | Wrapped in `run_in_threadpool` — check proxy logs |
| Runtime 502 "Connection was closed" | Cold-start disconnect | Proxy retries once before the first streamed event |
| Unknown runtime 404 | Slug is absent from the runtime registry | Update the ConfigMap and roll out the Deployment |
| MCP tool returns `An internal error occurred` | `datetime`/`Decimal` not JSON-serialisable | Fixed in handler — redeploy Lambda |
| MCP tool "Missing 'tool' field" | Gateway sends args directly, not wrapped | Handler infers tool from event shape — see `mcp_lambda/handler.py` |
| ETL primary date column has >5% null | Mixed SAP/EPIC formats not handled | Use `parse_mixed_date_fast()` in `etl_ah_analytics.py` |
| `ImagePullBackOff: no match for platform` | Wrong image arch | Proxy/MCP = amd64; agent container = arm64 |
| Internal NLB stuck pending | Subnet tags missing | Tag subnets with `kubernetes.io/role/internal-elb=1` |
| Gateway `http.passthrough` rejected | Endpoint must be HTTPS, protocolType in [A2A, CUSTOM, INFERENCE, MCP] | Use `mcp.lambda` bridge instead (see `timesfm_mcp/`) |
| TimesFM Lambda times out | Model needs to warm up on first pod restart | Check `kubectl get pods` — startup probe allows up to 360s for load |

## Further Reading

- [DEPLOY.md](DEPLOY.md) — step-by-step deployment guide for all components
- [REFLECTION.md](REFLECTION.md) — lessons learned building on AgentCore
- [him_scripts/DATA_DICTIONARY.md](him_scripts/DATA_DICTIONARY.md) — column semantics and inclusion criteria
- [.claude/Skills/](.claude/Skills/) — Agent Skills for SQL routing

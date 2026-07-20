# AgentCore Data Analytics Platform

A production-grade multi-tool agent platform on AWS AgentCore. Answers natural-language analytical questions against two PostgreSQL databases (`nuh-analytics`, `ah-analytics`), and forecasts future values using Google TimesFM. Exposes an OpenAI-compatible API. Frontend clients (Open WebUI, DIFY) connect through a VPC-internal EKS proxy — no internet traffic, no API keys.

## Architecture

```
Open WebUI / DIFY / SDK
        │  POST /v1/chat/completions   (+ chat_id + user_id)
        ▼
EKS Fargate: agentcore-proxy      (amd64, namespace: agentcore)
        │  invoke_agent_runtime / invoke_harness  ← IRSA
        │  streams tokens via SSE from harness contentBlockDelta events
        ▼
AgentCore Runtime / Harness      (ap-southeast-1, private VPC)
        │
        ├─ agentcore_poc         Claude Agent SDK + Gateway MCP + S3 Skills
        │                        (VPC-mode container, arm64, native SSE streaming)
        │                        Uses same 3 gateways as the harnesses, via localhost SigV4 proxy
        │
        ├─ harness_e52fs         Strands Agent, model=global.anthropic.claude-sonnet-4-6
        │       │  3 gateway tools mounted:
        │       │
        │       ├──▶ nuh-analytics-db (Gateway) → nuh-analytics-mcp (Lambda) → RDS nuh-analytics
        │       │
        │       ├──▶ ah-analytics-db  (Gateway) → ah-analytics-mcp  (Lambda) → RDS ah-analytics
        │       │
        │       └──▶ timesfm-gateway  (Gateway) → timesfm-mcp       (Lambda)
        │                                                                   │  HTTP POST
        │                                                                   ▼
        │                                                   Internal NLB → EKS pod: timesfm-service
        │                                                                   (TimesFM 2.5-200m, CPU)
        │
        └─ harness_dify          Strands Agent — dedicated harness for DIFY frontend
                                 Same gateway toolset as harness_e52fs
```

## OpenWebUI Insights test frontend

`https://insights.bot-alex.com` is an isolated OpenWebUI v0.10.2-slim test
deployment on the existing EC2 host. It runs alongside the legacy OpenWebUI
service, with a separate Docker volume and PostgreSQL database
(`openwebui_insights`), so no user or configuration migration is required.

Its model endpoint is the private NLB path `/insights/v1`:

```
Browser → ALB / insights.bot-alex.com → open-webui-insights
        → private NLB /insights/v1 → agentcore-proxy
        → harness_e52fs-Du2DM0RxvF → AgentCore Code Interpreter (when needed)
```

The endpoint is a separate identity/session namespace even though it reuses the
same Harness as `/harness`: `ActorID` is
`openwebui-insights:<OpenWebUI user UUID>` and `runtimeSessionId` is
`owui-insights-<user UUID>-<chat UUID>`. This keeps AgentCore memory isolated
by user and chat.

### Insights file handoff

OpenWebUI stores uploads in
`s3://agentcore-openwebui-insights-964340114883/openwebui-insights/` using its
S3 storage provider. Uploads are server-mediated (browser → OpenWebUI → S3),
not presigned browser-direct PUTs. Local persistent file cache is disabled and
`BYPASS_EMBEDDING_AND_RETRIEVAL=true` prevents the normal RAG/embedding path.

The `agentcore_file_context` filter applies only to the `insights` models. It:

1. requires an authenticated OpenWebUI user and owned chat;
2. finds files already attached anywhere in that chat;
3. retrieves each file through OpenWebUI's user-scoped lookup;
4. replaces raw file metadata with a compact `agentcore_files` manifest
   (`file_id`, `s3_uri`, filename, MIME type, size); and
5. removes raw `files` fields so the proxy never trusts browser-supplied file
   references.

Before invoking AgentCore, the proxy rejects any manifest object outside the
Insights bucket/prefix or whose S3 owner/file tags do not match the caller. It
then gives the validated URI to the Harness. The Harness can invoke AgentCore
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

`process=false` was used in the end-to-end validation. Confirm or enforce that
the OpenWebUI browser flow always sets it before claiming a universal guarantee
that OpenWebUI never parses an uploaded file locally.

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
| `proxy/server.py` | OpenAI-compatible FastAPI proxy — routes `/poc`, `/harness`, `/insights`, and `/dify`, with session/user mapping, file-manifest validation, and SSE streaming |
| `openwebui-insights/functions/agentcore_file_context.py` | OpenWebUI filter that creates the authenticated, chat-wide Insights S3 file manifest |
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

The proxy exposes **four backends × two API shapes** on the same service. Pick the slug that matches your backend, and the shape that matches your frontend.

### Slugs → backend

| Slug | Backend | AWS call |
|------|---------|----------|
| `/poc` | `agentcore_poc` runtime (Claude Agent SDK) | `invoke_agent_runtime` |
| `/harness` | `harness_e52fs` (Strands Agent for OpenWebUI) | `invoke_harness` |
| `/insights` | `harness_e52fs` with Insights identity/file validation | `invoke_harness` |
| `/dify` | `harness_dify` (Strands Agent for DIFY) | `invoke_harness` |

### API shapes

- **OpenAI-compatible** (Open WebUI, DIFY Model Provider, LangChain, etc.): `POST {base}/v1/chat/completions`. Also `GET {base}/v1/models`.
- **Dify App Chat API** (embedding as a Dify App): `POST {base}/v1/chat-messages`. Streams Dify SSE events (`message`, `message_end`, `error`).

For OpenAI-compatible clients the base is `http://<host>/{slug}/v1`. For the Dify App shape it is `http://<host>/dify/{slug}/v1`.

### From inside the EKS cluster (DIFY)

| Backend | Base URL (OpenAI shape) |
|---------|-------------------------|
| poc | `http://agentcore-proxy.agentcore.svc.cluster.local/poc/v1` |
| harness | `http://agentcore-proxy.agentcore.svc.cluster.local/harness/v1` |
| insights | `http://agentcore-proxy.agentcore.svc.cluster.local/insights/v1` |
| dify | `http://agentcore-proxy.agentcore.svc.cluster.local/dify/v1` |

### From VPC / peered VPC (Open WebUI)

Replace the cluster DNS with the internal NLB `k8s-agentcor-agentcor-a9dbd8956e-c923dee5a7cceccb.elb.ap-southeast-1.amazonaws.com`. All endpoints are VPC-internal, no auth (any Bearer token is accepted).

**Dify Model Provider config (recommended integration):**
Set `Base URL = http://<nlb>/dify/v1` (or `/harness/v1`, `/poc/v1`). Model type: `LLM`, mode: `Chat`, streaming on. API Key: any value.

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
OpenWebUI chat_id                → runtimeSessionId  (≥ 33 chars, proxy pads if shorter)
OpenWebUI model_item.info.user_id → actorId (harness) / runtimeUserId (runtime)
Dify conversation_id             → runtimeSessionId  (echoed back so client can reuse)
Dify user                        → actorId / runtimeUserId
```

For the Insights deployment, the OpenWebUI filter supplies the trusted user and
chat values as private-hop headers, and the proxy deliberately namespaces both
values as described in [OpenWebUI Insights test frontend](#openwebui-insights-test-frontend).

AgentCore managed memory uses two strategies (configured on the harness):
- **Semantic** — `/actors/{actorId}/facts/` — cross-session user facts. Extracted asynchronously (~30–60 s after the turn is saved).
- **Summarization** — `/actors/{actorId}/summaries/{sessionId}/` — per-conversation summaries.

### Known limitation — memory on `/poc` (Claude Agent SDK) is partial ⚠️

Cross-session memory works reliably on the two **harness** backends (`/harness`, `/dify`) — those are managed by AWS and read `actorId`/`runtimeSessionId` from the boto3 API natively.

On the **`/poc`** backend (Claude Agent SDK inside our container) it is only *partially* wired:
- `runtimeSessionId` is forwarded by AgentCore Runtime as HTTP header `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` — the container reads it.
- `runtimeUserId` is silently dropped by AgentCore Runtime — not forwarded as a header. The proxy works around this by injecting `chat_id`/`user_id` into the payload body, and the container reads them from there.
- OpenWebUI, however, does **not** send `chat_id` or `user_id` in its outbound OpenAI-shaped request body — only `{model, messages, stream}` — so when OpenWebUI calls `/poc`, the container has no user identity and skips both memory save and memory retrieval.
- Direct boto3 invocations that pass `runtimeUserId` do work end-to-end.

Verified end-to-end via direct API test: turn 1 saves `Charlie / ICU team` under a user id, ~1 min later (async fact extractor) turn 2 in a new session recalls it. But **from OpenWebUI, the `/poc` path currently loses user identity at the OpenWebUI-backend → proxy hop**.

**TODO:** either (a) enable an OpenWebUI Function/Filter that injects `user_id`/`chat_id` into outbound bodies, or (b) find/enable OpenWebUI's "Include User Info" toggle so it forwards `X-OpenWebUI-User-Id` as a header, then have the proxy read that header. The two managed harnesses are not affected because AgentCore's `invoke_harness` API takes `actorId` as a first-class parameter that the proxy already sets from any source it has — including a stable derived id when the body lacks one.

## Streaming (SSE)

All three backends stream token-by-token when the client asks for `stream=true` (OpenAI) or `response_mode="streaming"` (Dify).

- **Harness paths (`/harness`, `/dify`)** — the proxy consumes `contentBlockDelta` events from `invoke_harness`'s event stream and emits OpenAI-format SSE chunks.
- **Runtime path (`/poc`)** — the container uses `ClaudeAgentOptions(include_partial_messages=True)` and yields `content_block_delta` `text_delta` events as they arrive. Real progressive delivery — verified with a 300-word essay test (156 SSE lines spread over 7.7 s).
- **Cold-start retry** — if the first `invoke_harness` / `invoke_agent_runtime` call disconnects before the first token (`ConnectionClosedError`, cold container spin-up), the proxy retries once silently. Both connection classes are caught (`ConnectionClosedError`, `EventStreamError`).
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
| AgentCore harness (OpenWebUI) | `arn:aws:bedrock-agentcore:ap-southeast-1:964340114883:harness/harness_e52fs-Du2DM0RxvF` |
| AgentCore harness (DIFY) | `arn:aws:bedrock-agentcore:ap-southeast-1:964340114883:harness/harness_dify-LViqrsm86E` |
| Harness memory | `arn:aws:bedrock-agentcore:ap-southeast-1:964340114883:memory/harness_harness_e52fs_8d3d-vtE3DJC9ia` |
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
| TimesFM internal NLB | `k8s-agentcor-timesfms-fb1729afc9-4eef87d9ac68417f.elb.ap-southeast-1.amazonaws.com` |
| MCP Gateways | `nuh-analytics-db-fhbzdmtdta`, `ah-analytics-db-gszih4adsx`, `timesfm-gateway-w4fho4r9um` |

## Replicating in a New AWS Account

The minimum path from zero to a working OpenWebUI / DIFY endpoint. Each step is idempotent — safe to re-run.

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

# ── 6. AgentCore Harness (one per frontend) ───────────────────────
# Create in AWS console → Bedrock AgentCore → Harnesses.
# Attach the 3 gateway targets. Enable memory (semantic + summarization).
# Load Skills from your S3 bucket (plain repo/prefix path — no URL fragment).
# Repeat once per frontend (harness_e52fs for OpenWebUI, harness_dify for DIFY, etc.)

# ── 7. Proxy (single service, all backends) ───────────────────────
# Edit proxy/server.py RUNTIMES/HARNESSES dicts with your new ARNs.
bash proxy/build_and_push.sh        # amd64 image → ECR
kubectl apply -f proxy/k8s/         # deployment + service + IRSA SA
```

**Post-deploy verification:**
```bash
# OpenAI shape
curl -sS http://<nlb>/harness/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"harness","messages":[{"role":"user","content":"hi"}]}' | jq
# Dify shape
curl -sS http://<nlb>/dify/harness/v1/chat-messages -H 'Content-Type: application/json' \
  -d '{"query":"hi","user":"tester","response_mode":"blocking","conversation_id":"","inputs":{}}' | jq
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
- For Dify integration: point Dify's OpenAI-compatible Model Provider at `/{slug}/v1` (Dify auto-appends `/chat/completions`). Never at `/dify/{slug}/v1`.
- For OpenWebUI cross-chat memory: pick a harness backend (`/harness` or `/dify`), not `/poc` — see the memory-limitation note above.

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| AgentCore health check fails | `/ping` returning 404 | Ensure `@app.get("/ping")` exists in `app/main.py` |
| `Not logged in · Please run /login` | Subprocess not using Bedrock | Set `CLAUDE_CODE_USE_BEDROCK=1` in `ClaudeAgentOptions(env=...)` |
| `400 Invocation of model not supported` | Bare model ID | Use application inference profile ARN as model |
| `AccessDeniedException: InvokeAgentRuntime` | Policy scoped to runtime ARN | Use `Resource: "*"` — IAM check uses endpoint ARN |
| Proxy pod crash-looping | Blocking boto3 call on event loop | Wrapped in `run_in_threadpool` — check proxy logs |
| Harness 502 "Connection was closed" | Cold-start disconnect | Proxy retries once on `ConnectionClosedError` — verify catch is correct exception class |
| Harness "Failed to load tool ... 403 Forbidden" | Harness execution role missing new gateway ARN | Add gateway ARN to `AmazonBedrockAgentCoreHarnessGatewayPolicy_bd7bg` |
| Harness "Tool name X already exists" | Two Gateway targets with same name | Rename target so tool names `{target}___{tool}` are unique |
| Dify validation "Credentials validation failed 404" | Wrong Base URL — Dify appends `/chat/completions` | Set Base URL to `/harness/v1` (or `/poc/v1`, `/dify/v1`), NOT `/dify/harness/v1` |
| Dify "role failed to satisfy enum [user, assistant]" | Dify sends `role: system` messages; `invoke_harness` rejects them | Proxy hoists system messages into the harness `systemPrompt` field — see `_normalize_messages` in `proxy/server.py` |
| Dify "Skill path not found in repository" | Harness skill config points at wrong GitHub path | Fix skill config in AWS console → Edit harness → Skills. Use plain repo path, not URL fragment |
| OpenWebUI `/poc` chat: memory not recalled | OpenWebUI does not forward `user_id` to external OpenAI providers | See "Session and Memory Wiring" — TODO. `/harness`/`/dify` are unaffected |
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

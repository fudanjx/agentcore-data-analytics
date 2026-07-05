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
        ├─ agentcore_poc         Claude Agent SDK + execute_sql MCP tool
        │                        (VPC-mode container, arm64)
        │
        └─ harness_e52fs         Strands Agent, model=global.anthropic.claude-sonnet-4-6
                │  3 gateway tools mounted:
                │
                ├──▶ nuh-analytics-db (Gateway) → nuh-analytics-mcp (Lambda) → RDS nuh-analytics
                │
                ├──▶ ah-analytics-db  (Gateway) → ah-analytics-mcp  (Lambda) → RDS ah-analytics
                │
                └──▶ timesfm-gateway  (Gateway) → timesfm-mcp       (Lambda)
                                                                    │  HTTP POST
                                                                    ▼
                                                    Internal NLB → EKS pod: timesfm-service
                                                                    (TimesFM 2.5-200m, CPU)
```

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
| `proxy/server.py` | OpenAI-compatible FastAPI proxy — routes `/poc`, `/harness`, plus session/user mapping and SSE streaming |
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

Two path prefixes on the same proxy — pick the one that matches your agent.

### From inside the EKS cluster (DIFY)

| Slug | Base URL |
|------|----------|
| `/poc` (Claude Agent SDK) | `http://agentcore-proxy.agentcore.svc.cluster.local/poc` |
| `/harness` (Strands Agent) | `http://agentcore-proxy.agentcore.svc.cluster.local/harness` |

### From VPC / peered VPC (Open WebUI)

| Slug | Base URL |
|------|----------|
| `/poc` | `http://k8s-agentcor-agentcor-a9dbd8956e-c923dee5a7cceccb.elb.ap-southeast-1.amazonaws.com/poc` |
| `/harness` | `http://k8s-agentcor-agentcor-a9dbd8956e-c923dee5a7cceccb.elb.ap-southeast-1.amazonaws.com/harness` |

Append `/v1/chat/completions` for chat and `/v1/models` for the model list. All endpoints are internal-only.

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

The proxy maps OpenWebUI request fields to AgentCore session/memory parameters. Same conversation always reuses the same runtime session → container stays warm, memory namespaces work correctly.

```
OpenWebUI chat_id                → runtimeSessionId  (must be ≥ 33 chars)
OpenWebUI model_item.info.user_id → actorId (harness) / runtimeUserId (runtime)
```

AgentCore managed memory uses two strategies (configured on the harness):
- **Semantic** — `/actors/{actorId}/facts/` — cross-session user facts
- **Summarization** — `/actors/{actorId}/summaries/{sessionId}/` — per-conversation summaries

## Streaming (SSE)

`/harness/v1/chat/completions?stream=true` streams tokens as they arrive from AWS's `invoke_harness` event stream. The proxy consumes `contentBlockDelta` events and emits OpenAI-format SSE chunks, so Open WebUI shows the response typing in real-time.

Cold-start retry: if the first `invoke_harness` call disconnects before the first token (`ConnectionClosedError`, cold container spin-up), the proxy retries once silently.

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
| AgentCore harness | `arn:aws:bedrock-agentcore:ap-southeast-1:964340114883:harness/harness_e52fs-Du2DM0RxvF` |
| Harness memory | `arn:aws:bedrock-agentcore:ap-southeast-1:964340114883:memory/harness_harness_e52fs_8d3d-vtE3DJC9ia` |
| Inference profile | `arn:aws:bedrock:us-east-1:964340114883:application-inference-profile/ji5jakx5lho3` |
| RDS endpoint | `jinxin-postgres.cf7in3efovlt.ap-southeast-1.rds.amazonaws.com` |
| Secrets Manager | `arn:aws:secretsmanager:ap-southeast-1:964340114883:secret:agentcore-rds-credentials-tlv56J` |
| ECR agent image | `964340114883.dkr.ecr.ap-southeast-1.amazonaws.com/agentcore-poc:latest` (arm64) |
| ECR proxy image | `964340114883.dkr.ecr.ap-southeast-1.amazonaws.com/agentcore-proxy:latest` (amd64) |
| ECR timesfm image | `964340114883.dkr.ecr.ap-southeast-1.amazonaws.com/timesfm-service:latest` (amd64) |
| IRSA role | `arn:aws:iam::964340114883:role/agentcore-proxy-irsa` |
| Proxy internal NLB | `k8s-agentcor-agentcor-a9dbd8956e-c923dee5a7cceccb.elb.ap-southeast-1.amazonaws.com` |
| TimesFM internal NLB | `k8s-agentcor-timesfms-fb1729afc9-4eef87d9ac68417f.elb.ap-southeast-1.amazonaws.com` |
| MCP Gateways | `nuh-analytics-db-fhbzdmtdta`, `ah-analytics-db-gszih4adsx`, `timesfm-gateway-w4fho4r9um` |

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

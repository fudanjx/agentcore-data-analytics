# AgentCore Data Analytics Platform

An AWS AgentCore-based data analytics agent that connects to PostgreSQL RDS, answers natural language questions using Claude (via Amazon Bedrock), and exposes an OpenAI-compatible API. Frontend clients (Open WebUI, DIFY) connect through a VPC-internal EKS proxy — no internet traffic, no API keys.

## Architecture

```
Open WebUI / DIFY / SDK
        │  POST /v1/chat/completions
        ▼
EKS Fargate: agentcore-proxy          (amd64, namespace: agentcore)
        │  boto3.invoke_agent_runtime / invoke_harness  ← IRSA
        │  via bedrock-agentcore VPC endpoint
        ▼
AWS AgentCore Runtime  (ap-southeast-1, private VPC)
        │  POST /invocations
        ▼
Agent Container  (ECR: agentcore-poc, arm64)
        │  claude-agent-sdk + MCP execute_sql tool
        │  CLAUDE_CODE_USE_BEDROCK=1
        ▼
Amazon Bedrock  (us-east-1, cross-region inference profile)
        │  global.anthropic.claude-sonnet-4-6
        ▼
RDS PostgreSQL  (ap-southeast-1, private subnets)
        credentials via Secrets Manager
```

Two runtimes are exposed by the proxy:

| Slug | Runtime | Invoke API |
|------|---------|------------|
| `/poc` | `agentcore_poc` — Claude Agent SDK, NUH analytics DB | `invoke_agent_runtime` |
| `/harness` | `harness_harness_e52fs` — Strands Agent, NUH analytics DB | `invoke_harness` |

## Repository Layout

```
app/                    Agent container (FastAPI, claude-agent-sdk, MCP tool)
  main.py               AgentCore container HTTP server (/ping, /invocations)
  agent.py              Claude Agent SDK loop with execute_sql MCP tool
  db.py                 RDS connection via Secrets Manager
  tools.py              MCP tool definition (execute_sql)
infra/
  build_and_push.sh     Build arm64 agent image → ECR
  deploy.py             Provision AgentCore Runtime + Endpoint (idempotent)
  etl_nuh_analytics.py  Load parquet files from S3 into RDS
  mask_pii.py           Mask phone numbers and addresses in loaded data
mcp_lambda/
  handler.py            MCP Lambda — 3 tools: execute_sql, list_tables, describe_table
  deploy.py             Provision Lambda + AgentCore Gateway (idempotent)
proxy/
  server.py             OpenAI-compatible FastAPI proxy (both runtimes)
  Dockerfile            amd64 proxy image
  build_and_push.sh     Build and push proxy → ECR
  k8s/                  Kubernetes manifests (namespace, deployment, service, IRSA SA)
Dockerfile              arm64 agent container image
requirements.txt        Agent container Python dependencies
py_sdk.py               Direct boto3 client example
```

## Quick Start

### Prerequisites

```bash
aws --version       # AWS CLI v2, configured with ap-southeast-1 profile
docker --version
kubectl version     # configured for ai-project EKS cluster
python3 -m pip install boto3
aws eks update-kubeconfig --region ap-southeast-1 --name ai-project
```

### Deploy or update the agent container

```bash
# 1. Build arm64 image and push to ECR
bash infra/build_and_push.sh

# 2. Deploy/update AgentCore Runtime
export ECR_IMAGE_URI=964340114883.dkr.ecr.ap-southeast-1.amazonaws.com/agentcore-poc:latest
export RDS_SECRET_ARN=arn:aws:secretsmanager:ap-southeast-1:964340114883:secret:agentcore-rds-credentials-tlv56J
export RDS_DB_NAME=nuh-analytics
python3 infra/deploy.py
```

### Deploy or update the EKS proxy

```bash
# 1. Build amd64 image and push to ECR
bash proxy/build_and_push.sh

# 2. Apply K8s manifests (first time only)
kubectl apply -f proxy/k8s/namespace.yaml
kubectl apply -f proxy/k8s/serviceaccount.yaml
kubectl apply -f proxy/k8s/deployment.yaml
kubectl apply -f proxy/k8s/service.yaml

# 2b. Subsequent updates — rolling restart picks up new image
kubectl rollout restart deployment/agentcore-proxy -n agentcore
kubectl rollout status deployment/agentcore-proxy -n agentcore
```

### Deploy or update the MCP Lambda + Gateway

```bash
python3 mcp_lambda/deploy.py
```

## API Endpoints

### From inside the EKS cluster (DIFY)

| Runtime | Base URL |
|---------|----------|
| NUH Analytics (poc) | `http://agentcore-proxy.agentcore.svc.cluster.local/poc` |
| Strands Harness | `http://agentcore-proxy.agentcore.svc.cluster.local/harness` |
| Legacy (compat) | `http://agentcore-proxy.agentcore.svc.cluster.local` |

### From VPC / peered VPC (Open WebUI)

| Runtime | Base URL |
|---------|----------|
| NUH Analytics (poc) | `http://k8s-agentcor-agentcor-a9dbd8956e-c923dee5a7cceccb.elb.ap-southeast-1.amazonaws.com/poc` |
| Strands Harness | `http://k8s-agentcor-agentcor-a9dbd8956e-c923dee5a7cceccb.elb.ap-southeast-1.amazonaws.com/harness` |

All endpoints are internal-only. Append `/v1/chat/completions` for the chat endpoint and `/v1/models` for the model list.

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

## Key Design Decisions

| Decision | Choice | Reason |
|----------|--------|--------|
| Agent harness | `claude-agent-sdk` subprocess | Official SDK with MCP tool support |
| LLM auth | `CLAUDE_CODE_USE_BEDROCK=1` + inference profile ARN | IAM only, no API keys |
| Bedrock model | Application inference profile `ji5jakx5lho3` (us-east-1) | On-demand requires inference profile; bare model IDs rejected |
| Bedrock region | `us-east-1` in subprocess env | Inference profile lives in us-east-1; override needed for cross-region container |
| AgentCore network | VPC mode, private subnets | All traffic on AWS backbone, no internet |
| VPC endpoints | bedrock-runtime, bedrock-agentcore, secretsmanager, ecr.api, ecr.dkr | Internet-free operation |
| Agent container platform | `linux/arm64` | Required by AgentCore Runtime |
| Proxy container platform | `linux/amd64` | EKS Fargate nodes in this cluster are amd64 |
| Frontend auth | EKS proxy with IRSA | Frontends don't support SigV4; org SCP blocks anonymous Lambda URLs |
| Memory scoping | `chat_id` → `runtimeSessionId`, `user_id` → `actorId` | AgentCore memory namespaced per user + conversation |

## Session & Memory

The proxy maps OpenWebUI request fields to AgentCore session/memory parameters automatically:

```
OpenWebUI chat_id                → runtimeSessionId  (keeps container warm across turns)
OpenWebUI model_item.info.user_id → actorId (harness) / runtimeUserId (runtime)
```

AgentCore's managed memory uses two strategies:
- **Semantic** (`/actors/{actorId}/facts/`) — cross-session user facts
- **Summarization** (`/actors/{actorId}/summaries/{sessionId}/`) — per-conversation summaries

## MCP Lambda Tools

The `nuh-analytics-mcp` Lambda exposes three read-only tools via AgentCore Gateway (`nuh-analytics-db`):

| Tool | Description |
|------|-------------|
| `execute_sql` | Run a SELECT query, returns rows as JSON |
| `list_tables` | List all tables with column types |
| `describe_table` | Column info + 3 sample rows for a table |

## AWS Resource Reference

| Resource | Value |
|----------|-------|
| AgentCore poc runtime ARN | `arn:aws:bedrock-agentcore:ap-southeast-1:964340114883:runtime/agentcore_poc-iumXW8638m` |
| AgentCore harness ARN | `arn:aws:bedrock-agentcore:ap-southeast-1:964340114883:harness/harness_e52fs-Du2DM0RxvF` |
| Inference profile ARN | `arn:aws:bedrock:us-east-1:964340114883:application-inference-profile/ji5jakx5lho3` |
| RDS endpoint | `jinxin-postgres.cf7in3efovlt.ap-southeast-1.rds.amazonaws.com` |
| Secrets Manager ARN | `arn:aws:secretsmanager:ap-southeast-1:964340114883:secret:agentcore-rds-credentials-tlv56J` |
| ECR agent image | `964340114883.dkr.ecr.ap-southeast-1.amazonaws.com/agentcore-poc:latest` |
| ECR proxy image | `964340114883.dkr.ecr.ap-southeast-1.amazonaws.com/agentcore-proxy:latest` |
| IRSA role | `arn:aws:iam::964340114883:role/agentcore-proxy-irsa` |
| Internal NLB | `k8s-agentcor-agentcor-a9dbd8956e-c923dee5a7cceccb.elb.ap-southeast-1.amazonaws.com` |
| MCP Gateway ID | `nuh-analytics-db-fhbzdmtdta` |

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| AgentCore health check fails | `/ping` returning 404 | Ensure `@app.get("/ping")` exists in `app/main.py` |
| `Not logged in · Please run /login` | Subprocess not using Bedrock | Set `CLAUDE_CODE_USE_BEDROCK=1` in `ClaudeAgentOptions(env=...)` |
| `400 Invocation of model not supported` | Bare model ID | Use application inference profile ARN as model |
| `AccessDeniedException: InvokeAgentRuntime` | Policy scoped to runtime ARN | Use `Resource: "*"` — IAM check uses endpoint ARN |
| Proxy pod crash-looping | Blocking boto3 call on event loop | Wrapped in `run_in_threadpool` — check proxy logs for import errors |
| Harness 502 on first message | Cold-start connection close | Proxy retries once automatically on `ConnectionClosedError` |
| `ImagePullBackOff: no match for platform` | Wrong image arch | Proxy = `linux/amd64`; agent = `linux/arm64` |
| Internal NLB stuck pending | Subnet tags missing | Tag subnets with `kubernetes.io/role/internal-elb=1` |
| MCP tool returns `An internal error occurred` | `datetime`/`Decimal` not JSON-serialisable | Fixed in handler — redeploy Lambda with `python3 mcp_lambda/deploy.py` |

## Further Reading

- [DEPLOY.md](DEPLOY.md) — step-by-step deployment guide
- [PRD.md](PRD.md) — product requirements and full architecture decisions
- [REFLECTION.md](REFLECTION.md) — lessons learned building on AgentCore

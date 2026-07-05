# Deployment Guide

## Overview

Four deployable components:

| Component | Where | Purpose |
|---|---|---|
| `agentcore-poc` container | AWS AgentCore Runtime (ap-southeast-1) | Legacy Claude Agent SDK agent (path prefix `/poc`) |
| `agentcore-proxy` container | EKS Fargate (`agentcore` namespace) | OpenAI-compatible proxy → AgentCore runtime/harness |
| MCP Lambdas | Lambda in VPC | Gateway backends: `nuh-analytics-mcp`, `ah-analytics-mcp`, `timesfm-mcp` |
| `timesfm-service` pod | EKS Fargate (`agentcore` namespace) | TimesFM 2.5-200m forecasting service (CPU) |

Plus supporting infra:
- AgentCore Gateways (3): `nuh-analytics-db`, `ah-analytics-db`, `timesfm-gateway`
- AgentCore Harness: `harness_e52fs` (Strands agent, managed memory)
- RDS PostgreSQL with two databases: `nuh-analytics`, `ah-analytics`

---

## Prerequisites

```bash
aws --version           # AWS CLI v2
docker --version        # Docker Desktop (amd64 + arm64 buildx)
kubectl version         # kubectl configured for ai-project cluster
python3 --version       # Python 3.10+
pip3 install boto3

aws eks update-kubeconfig --region ap-southeast-1 --name ai-project
```

---

## Part 1 — Agent Container (`agentcore_poc` Runtime)

### Step 1 — Configure

```bash
cp .env.example .env
```

```dotenv
AWS_DEFAULT_REGION=ap-southeast-1
CLAUDE_CODE_USE_BEDROCK=1
RDS_SECRET_ARN=arn:aws:secretsmanager:ap-southeast-1:964340114883:secret:agentcore-rds-credentials-tlv56J
RDS_DB_NAME=nuh-analytics
```

### Step 2 — Test locally (optional)

```bash
pip3 install -r requirements.txt
export $(grep -v '^#' .env | xargs)
uvicorn app.main:app --port 8080 --reload

curl -X POST http://localhost:8080/invocations \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"list all tables"}]}'
```

RDS timeout is expected locally (private VPC).

### Step 3 — Build and deploy

```bash
bash infra/build_and_push.sh       # linux/arm64 to ECR
export ECR_IMAGE_URI=964340114883.dkr.ecr.ap-southeast-1.amazonaws.com/agentcore-poc:latest
export RDS_SECRET_ARN=arn:aws:secretsmanager:ap-southeast-1:964340114883:secret:agentcore-rds-credentials-tlv56J
export RDS_DB_NAME=nuh-analytics
python3 infra/deploy.py            # creates Runtime + Endpoint
```

### Step 4 — Verify

AWS Console → Bedrock → AgentCore → `agentcore_poc` → Test.
Or: `python3 py_sdk.py "list all tables"`.

---

## Part 2 — EKS Proxy

The proxy speaks OpenAI-compatible HTTP and forwards to AgentCore runtimes/harnesses.

### Step 1 — Prep IRSA (one-time)

Already created as `agentcore-proxy-irsa`. Trust policy uses OIDC provider on the cluster. Inline policy: `bedrock-agentcore:InvokeAgentRuntime`, `InvokeAgentRuntimeForUser`, `InvokeHarness`, plus S3/SecretsManager for ETL jobs. Use `Resource: "*"` — the IAM check uses endpoint ARN not runtime ARN.

If recreating:
```bash
ACCOUNT=964340114883
OIDC_ID=62A4B3D5B9330B4CE46ADB4CC753DFB3
# See scripts/create_irsa.sh (or one-off inline aws iam create-role commands)
```

### Step 2 — Tag subnets for internal NLB (one-time)

```bash
aws ec2 create-tags \
  --region ap-southeast-1 \
  --resources subnet-061205c705e0f41d4 subnet-0466b6e1fbb8a49f3 \
  --tags Key=kubernetes.io/role/internal-elb,Value=1 \
         Key=kubernetes.io/cluster/ai-project,Value=shared
```

Without these tags: `Failed build model due to unable to resolve at least one subnet`.

### Step 3 — Build and push proxy

```bash
bash proxy/build_and_push.sh       # linux/amd64 (Fargate is amd64)
```

### Step 4 — Deploy manifests

```bash
kubectl apply -f proxy/k8s/namespace.yaml
kubectl apply -f proxy/k8s/serviceaccount.yaml
kubectl apply -f proxy/k8s/deployment.yaml
kubectl apply -f proxy/k8s/service.yaml
```

### Step 5 — Verify

```bash
kubectl get pods -n agentcore
kubectl get svc -n agentcore

# From within cluster:
kubectl run test --rm -i --restart=Never --image=curlimages/curl -n dify \
  -- curl -s http://agentcore-proxy.agentcore.svc.cluster.local/v1/models
```

### Step 6 — Configure frontends

**DIFY** (same cluster):
- Base URL for NUH Analytics agent: `http://agentcore-proxy.agentcore.svc.cluster.local/poc`
- Base URL for Strands Harness (multi-DB + forecasting): `http://agentcore-proxy.agentcore.svc.cluster.local/harness`
- API Key: any value (proxy ignores it)
- Model: match slug (`poc` or `harness`)

**Open WebUI** (EC2 in default VPC via peering):
- Base URL: `http://k8s-agentcor-agentcor-a9dbd8956e-c923dee5a7cceccb.elb.ap-southeast-1.amazonaws.com/harness`
- Send `chat_id` and `model_item.info.user_id` in the request body (Open WebUI does this automatically)

---

## Part 3 — MCP Lambda + Gateway (nuh-analytics)

Deploys `nuh-analytics-mcp` Lambda plus `nuh-analytics-db` Gateway, and wires it to `harness_e52fs`.

```bash
python3 mcp_lambda/deploy.py
```

**What it creates:**

| Resource | Name |
|----------|------|
| IAM role (Lambda) | `nuh-analytics-mcp-role` |
| Lambda function (VPC) | `nuh-analytics-mcp` |
| IAM role (Gateway) | `nuh-analytics-gateway-role` |
| AgentCore Gateway | `nuh-analytics-db` (MCP, AWS_IAM) |
| Gateway Target | `rds-tools` — 3 inline tools: `execute_sql`, `list_tables`, `describe_table` |

The Gateway is auto-added to `harness_e52fs.tools`. The Gateway ARN is also added to the harness's execution IAM policy `AmazonBedrockAgentCoreHarnessGatewayPolicy_bd7bg`.

---

## Part 4 — MCP Lambda + Gateway (ah-analytics)

Same pattern as Part 3, but for the second database. Uses `handler.py` from `mcp_lambda/` (shared code), only `DB_NAME` env var differs.

```bash
python3 mcp_lambda/deploy_ah.py
```

**Important:** The Gateway Target name is `ah-rds-tools`, not `rds-tools`. Strands SDK builds tool names as `{target}___{tool}`, so both DBs having `rds-tools___describe_table` would collide.

---

## Part 5 — TimesFM Forecasting Service (EKS + MCP Lambda)

### 5.1 Build the TimesFM service image

Model weights baked into the image (~1.5GB total) — no runtime HuggingFace download.

```bash
bash timesfm_service/build_and_push.sh
```

CPU-only PyTorch installed via `pip --index-url https://download.pytorch.org/whl/cpu` (do NOT use `timesfm[torch]` — that pulls CUDA).

### 5.2 Deploy TimesFM pod

```bash
kubectl apply -f timesfm_service/k8s/deployment.yaml
kubectl apply -f timesfm_service/k8s/service.yaml
```

This creates:
- `timesfm-service` Deployment (1 replica, 2 CPU / 6-8Gi RAM)
- `timesfm-svc` ClusterIP (in-cluster access)
- `timesfm-svc-internal` LoadBalancer (internal NLB — for the bridge Lambda)

Wait for the pod to become ready (~30-60s for model load):
```bash
kubectl rollout status deployment/timesfm-service -n agentcore --timeout=300s
```

Smoke test:
```bash
kubectl port-forward svc/timesfm-svc 18100:80 -n agentcore &
curl -s -X POST http://localhost:18100/forecast \
  -H 'Content-Type: application/json' \
  -d '{"context":[100,105,98,112,120,115,130,125,140,135,150,145],"horizon":3,"freq":"M"}'
```

### 5.3 Deploy the bridge Lambda + wire to harness

AgentCore Gateway `http.passthrough` requires HTTPS, but our internal NLB is HTTP. Solution: a thin Lambda in the same VPC that forwards Gateway MCP calls to the NLB over HTTP.

```bash
NLB=$(kubectl get svc timesfm-svc-internal -n agentcore \
  -o jsonpath='{.status.loadBalancer.ingress[0].hostname}')

NLB_ENDPOINT="http://${NLB}" python3 timesfm_mcp/deploy.py
```

**What it creates:**

| Resource | Name |
|----------|------|
| IAM role (Lambda) | `timesfm-mcp-role` |
| Lambda function (VPC) | `timesfm-mcp` — 30-line urllib bridge, `TIMESFM_URL` env |
| Gateway Target | `timesfm-forecast` — inline schema for `timesfm_forecast` tool |

The existing `timesfm-gateway` (created by an earlier failed `http.passthrough` attempt) is reused. The tool is added to `harness_e52fs` and the gateway ARN to the harness IAM policy.

### 5.4 Verify end-to-end

```bash
aws lambda invoke \
  --function-name timesfm-mcp \
  --region ap-southeast-1 \
  --payload '{"context":[100,105,98,112,120,115,130,125,140,135,150,145],"horizon":3,"freq":"M","context_dates":["2024-01","2024-02","2024-03","2024-04","2024-05","2024-06","2024-07","2024-08","2024-09","2024-10","2024-11","2024-12"]}' \
  --cli-binary-format raw-in-base64-out \
  /tmp/out.json && cat /tmp/out.json | python3 -m json.tool
```

Should return `{"result": {"forecast": [...], "lower_80": [...], "upper_80": [...], "forecast_dates": [...]}}`.

From Open WebUI, ask: *"Based on monthly admissions [100,105,...,145], forecast the next 3 months."* — the harness should invoke `timesfm_forecast` automatically.

---

## Part 6 — Data Ingestion into RDS

RDS is in private subnets — not reachable from a developer Mac. Use ECS Fargate task inside the VPC.

### 6.1 nuh-analytics

```bash
aws s3 cp infra/etl_nuh_analytics.py \
  s3://agentcore-tmp-964340114883/etl_nuh_analytics.py

aws ecs run-task \
  --region ap-southeast-1 \
  --cluster embedded-web-app \
  --task-definition agentcore-nuh-etl:3 \
  --launch-type FARGATE \
  --network-configuration '{"awsvpcConfiguration":{"subnets":["subnet-061205c705e0f41d4"],"securityGroups":["sg-07258677b7e691e48"],"assignPublicIp":"DISABLED"}}'

aws logs tail /ecs/agentcore-nuh-etl --region ap-southeast-1 --follow
```

### 6.2 ah-analytics

Same pattern, uses `agentcore-ah-etl:1` task definition. The ETL drops and recreates the `ah-analytics` database on every run for clean reloads.

```bash
aws s3 cp infra/etl_ah_analytics.py \
  s3://agentcore-tmp-964340114883/etl_ah_analytics.py

aws ecs run-task \
  --region ap-southeast-1 \
  --cluster embedded-web-app \
  --task-definition agentcore-ah-etl:1 \
  --launch-type FARGATE \
  --network-configuration '{"awsvpcConfiguration":{"subnets":["subnet-061205c705e0f41d4"],"securityGroups":["sg-07258677b7e691e48"],"assignPublicIp":"DISABLED"}}'
```

**Critical:** Both ETL scripts handle mixed SAP-era (`DD.MM.YYYY`, `D/M/YYYY`) and EPIC-era (`YYYY-MM-DD`) date formats in the same column. See `parse_mixed_date_fast()` in `etl_ah_analytics.py`. Without this, ~60% of dates parse to NaT.

### 6.3 PII masking

```bash
python3 infra/mask_pii.py           # requires VPC access — run from an ECS task
```

Masks phone numbers and street addresses in `emd`, `inpatient_movement`, and any table containing `RESIDENT_TEL`, `CONTACT_TEL`, `ADDRESS1`, `ADDRESS2`, `BLOCK_BUILD`. Postal codes preserved.

---

## Updating After Code Changes

| Change | Rebuild + redeploy |
|--------|---------------------|
| `app/` (agent container) | `bash infra/build_and_push.sh && python3 infra/deploy.py` |
| `proxy/server.py` | `bash proxy/build_and_push.sh && kubectl rollout restart deployment/agentcore-proxy -n agentcore` |
| `mcp_lambda/handler.py` (both DBs use same handler) | `python3 mcp_lambda/deploy.py && python3 mcp_lambda/deploy_ah.py` |
| `timesfm_service/server.py` | `bash timesfm_service/build_and_push.sh && kubectl rollout restart deployment/timesfm-service -n agentcore` |
| `timesfm_mcp/handler.py` | `NLB_ENDPOINT=http://... python3 timesfm_mcp/deploy.py` |
| Adding a new tool to a Gateway | Edit `TOOL_SCHEMA` in the deploy script and re-run — `create_gateway_target` is idempotent by name; delete first if updating an existing target |

All deploy scripts are idempotent.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| AgentCore container health check fails on `GET /ping` | Container returns 404 for `/ping` | Add `@app.get("/ping")` to `app/main.py` |
| AgentCore 500: POST /invocations 404 | Container has `/invoke` not `/invocations` | AgentCore uses `/invocations`, not `/invoke` |
| `Not logged in · Please run /login` | `claude` subprocess using Anthropic API, not Bedrock | Set `CLAUDE_CODE_USE_BEDROCK=1` and pass via `ClaudeAgentOptions(env=...)` |
| `400 Invocation of model ... not supported` | Using bare model ID without inference profile | Use `application-inference-profile` ARN as model |
| `403` on Lambda Function URL | Org SCP blocks `AuthType: NONE` | Use `AWS_IAM`; use SigV4 or EKS proxy |
| `Unable to locate credentials` in EKS pod | Fargate pod execution role ≠ container credentials | Use IRSA: create IAM role with OIDC trust, annotate ServiceAccount |
| `AccessDeniedException: no identity-based policy allows InvokeAgentRuntime` | Policy scoped to runtime ARN but check uses endpoint ARN | Use `Resource: "*"` |
| Harness "Failed to load tool ... 403 Forbidden" | Harness execution role missing new gateway ARN | Add ARN to `AmazonBedrockAgentCoreHarnessGatewayPolicy_bd7bg` |
| Harness "Tool name X already exists" | Two Gateway targets named identically | Rename target — tool names are `{target}___{tool}` |
| Harness 502 "Connection was closed before valid response" | Cold-start disconnect | Proxy catches `ConnectionClosedError` and retries once (was catching wrong exception class before fix) |
| MCP tool returns `An internal error occurred` on describe_table | `datetime`/`Decimal` not JSON-serialisable | Redeploy Lambda — handler round-trips through `json.dumps` with `_json_default` |
| MCP tool "Missing 'tool' field" | Gateway sends args directly, not wrapped in `{tool, arguments}` | Handler infers tool from event shape |
| ETL primary date column has >5% null | Mixed SAP/EPIC date formats | Use `parse_mixed_date_fast()` — handles `YYYY-MM-DD [HH:MM:SS]`, `DD.MM.YYYY`, `D/M/YYYY` per-value |
| Gateway `http.passthrough` rejected | Requires `https://` endpoint + protocolType in [A2A, CUSTOM, INFERENCE, MCP] | Use `mcp.lambda` with a bridge Lambda (see Part 5) |
| Internal NLB stuck pending | Subnets missing `kubernetes.io/role/internal-elb=1` tag | Tag both private subnets |
| EKS `ImagePullBackOff: no match for platform` | Wrong image arch | Proxy/MCP/timesfm = amd64; agent = arm64 |
| TimesFM `AttributeError: module 'timesfm' has no attribute 'TimesFm'` | Wrong API version | v2.x uses `TimesFM_2p5_200M_torch`, not `TimesFm` |
| TimesFM `Model is not compiled` | Missing `model.compile()` | Call `model.compile(ForecastConfig(max_horizon=128, per_core_batch_size=1, max_context=512))` |
| TimesFM `OutOfBoundsDatetime` in ETL | Garbage year (e.g. "04.02.0201") | Prefilter parsed years to [1678, 2262] before `datetime64[ns]` cast |
| Cannot connect to RDS from dev Mac | RDS in private subnet | Use ECS Fargate task |
| ECS task can't run `pg_restore` | Alpine image lacks `postgresql-client` | `apk add postgresql-client` at task start; run as `user: root` |

---

## AWS Resource Reference

| Resource | ID / ARN |
|---|---|
| AgentCore poc runtime | `arn:aws:bedrock-agentcore:ap-southeast-1:964340114883:runtime/agentcore_poc-iumXW8638m` |
| AgentCore harness | `arn:aws:bedrock-agentcore:ap-southeast-1:964340114883:harness/harness_e52fs-Du2DM0RxvF` |
| Harness memory | `arn:aws:bedrock-agentcore:ap-southeast-1:964340114883:memory/harness_harness_e52fs_8d3d-vtE3DJC9ia` |
| Inference profile | `arn:aws:bedrock:us-east-1:964340114883:application-inference-profile/ji5jakx5lho3` |
| RDS endpoint | `jinxin-postgres.cf7in3efovlt.ap-southeast-1.rds.amazonaws.com` |
| Secrets Manager | `arn:aws:secretsmanager:ap-southeast-1:964340114883:secret:agentcore-rds-credentials-tlv56J` |
| ECR (agent) | `964340114883.dkr.ecr.ap-southeast-1.amazonaws.com/agentcore-poc` |
| ECR (proxy) | `964340114883.dkr.ecr.ap-southeast-1.amazonaws.com/agentcore-proxy` |
| ECR (timesfm) | `964340114883.dkr.ecr.ap-southeast-1.amazonaws.com/timesfm-service` |
| IRSA role | `arn:aws:iam::964340114883:role/agentcore-proxy-irsa` |
| EKS OIDC | `oidc.eks.ap-southeast-1.amazonaws.com/id/62A4B3D5B9330B4CE46ADB4CC753DFB3` |
| Proxy internal NLB | `k8s-agentcor-agentcor-a9dbd8956e-c923dee5a7cceccb.elb.ap-southeast-1.amazonaws.com` |
| TimesFM internal NLB | `k8s-agentcor-timesfms-fb1729afc9-4eef87d9ac68417f.elb.ap-southeast-1.amazonaws.com` |
| Gateway (NUH) | `nuh-analytics-db-fhbzdmtdta` |
| Gateway (AH) | `ah-analytics-db-gszih4adsx` |
| Gateway (TimesFM) | `timesfm-gateway-w4fho4r9um` |
| Lambda (NUH MCP) | `arn:aws:lambda:ap-southeast-1:964340114883:function:nuh-analytics-mcp` |
| Lambda (AH MCP) | `arn:aws:lambda:ap-southeast-1:964340114883:function:ah-analytics-mcp` |
| Lambda (TimesFM MCP bridge) | `arn:aws:lambda:ap-southeast-1:964340114883:function:timesfm-mcp` |
| Harness gateway policy | `arn:aws:iam::964340114883:policy/service-role/AmazonBedrockAgentCoreHarnessGatewayPolicy_bd7bg` |

## VPC Interface Endpoints (bot-nuhs-vpc, ap-southeast-1)

| Endpoint ID | Service |
|---|---|
| `vpce-0b582d02606dfbe00` | `bedrock-runtime` |
| `vpce-0d7da6165d12a2ae8` | `bedrock-agentcore` |
| `vpce-059f7b6613b722983` | `secretsmanager` |
| `vpce-02600a734df24aff5` | `ecr.api` |
| `vpce-084fe8036d1b6e33b` | `ecr.dkr` |
| `vpce-0cb3dca98becb59a1` | S3 Gateway |

All use security group `sg-0be4a7ae0ed2caf17` (vpc-endpoints-sg).

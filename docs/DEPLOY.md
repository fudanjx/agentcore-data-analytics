# Deployment Guide

## Overview

Four deployable components:

| Component | Where | Purpose |
|---|---|---|
| `agentcore-poc` container | AWS AgentCore Runtime (ap-southeast-1) | Claude Agent SDK agent with Gateway MCP + S3 Skills + Memory (path prefix `/poc`) |
| `agentcore-proxy` container | EKS Fargate (`agentcore` namespace) | Proxy fronting all backends — OpenAI-compatible + Dify App API |
| MCP Lambdas | Lambda in VPC | Gateway backends: `nuh-analytics-mcp`, `ah-analytics-mcp`, `timesfm-mcp` |
| `ah-analytics-s3tables-mcp` Lambda | Lambda (no VPC) | Athena-backed MCP for `ah-analytics` S3 Tables |
| `ah-analytics-s3tables-loader` Lambda | Lambda (container image) | S3-event-triggered: parquet → S3 Tables (Iceberg) |
| `timesfm-service` pod | EKS Fargate (`agentcore` namespace) | TimesFM 2.5-200m forecasting service (CPU) |

Plus AWS-console-managed infra (created once via console, referenced by the proxy):
- AgentCore Gateways (4): `nuh-analytics-db`, `ah-analytics-db`, `ah-analytics-s3tables`, `timesfm-gateway`
- AgentCore Harnesses (2): `harness_e52fs` (Strands, for OpenWebUI), `harness_dify` (Strands, for Dify)
- AgentCore Memory: single shared instance keyed off harness_e52fs
- S3 Skills bucket: `s3://ah-data-analytics/skills/` — synced by the poc container on startup
- RDS PostgreSQL with two databases: `nuh-analytics`, `ah-analytics`
- S3 Tables bucket: `ah-analytics` (Iceberg, 6 tables mirroring `ah-analytics` RDS) — queried via Athena workgroup `ah-s3tables-wg`, federated Glue catalog `s3tablescatalog/ah-analytics`
- S3 Uploads bucket: `agentcore-user-uploads-964340114883` — per-actor prefix `uploads/{actor_id}/{conversation_id}/{filename}`, 24-h lifecycle, read by the shared Code Interpreter sandbox
- Code Interpreter sandbox: `agentcore_user_uploads_ci` — attached to both harnesses; pandas/openpyxl/pypdf/python-docx/python-pptx/matplotlib pre-installed

Proxy exposes three backend slugs — `/poc`, `/harness`, `/dify` — each in two shapes: OpenAI (`/{slug}/v1/chat/completions`) and Dify App (`/dify/{slug}/v1/chat-messages`). File uploads on both surfaces: OpenAI `POST /v1/files` and Dify `POST /dify/{slug}/files/upload`.

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

The poc container no longer talks to RDS directly. It uses the three AgentCore Gateway MCP servers (via an in-process SigV4 signing proxy on `127.0.0.1:9000`), and loads Agent Skills from S3 at startup. Memory is bridged to AgentCore Memory via `app/memory.py`.

### Step 1 — Build and deploy

```bash
bash infra/build_and_push.sh       # linux/arm64 → ECR
export ECR_IMAGE_URI=964340114883.dkr.ecr.ap-southeast-1.amazonaws.com/agentcore-poc:latest
python3 infra/deploy.py            # creates IAM role + Runtime + Endpoint (idempotent)
```

The IAM role provisioned by `infra/deploy.py` includes:
- `bedrock:InvokeModel*` on the inference profile
- `bedrock-agentcore:InvokeGateway` on the three gateway ARNs (nuh, ah, timesfm)
- `s3:GetObject`/`ListBucket` on the Skills bucket
- `bedrock-agentcore:CreateEvent`/`RetrieveMemoryRecords`/`ListEvents` on the shared memory ARN
- `ec2:CreateNetworkInterface`/... for VPC mode
- No RDS or Secrets Manager access — this container no longer needs it

### Step 2 — Verify

AWS Console → Bedrock → AgentCore → `agentcore_poc` → Test with a prompt.
Or via the proxy: `curl -s http://<proxy-nlb>/poc/v1/chat/completions -H 'Content-Type: application/json' -d '{"model":"poc","messages":[{"role":"user","content":"list tables in nuh-analytics"}]}'`.

Container logs (CloudWatch: `/aws/bedrock-agentcore/runtimes/agentcore_poc-*-DEFAULT`) should show on startup:
```
Startup: syncing skills from S3...
Skills sync complete: 7 files in /app/skills
Startup: launching Gateway SigV4 proxy on localhost...
Gateway SigV4 proxy listening on 127.0.0.1:9000
```

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

Base URL pattern is `<host>/<slug>/v1` where `<slug>` is one of `poc`, `harness`, `dify`.

**DIFY** (Model Provider → OpenAI-API-compatible, same cluster or via NLB):
- Base URL: `http://agentcore-proxy.agentcore.svc.cluster.local/dify/v1` (dedicated `harness_dify` backend)
- API Key: any value (proxy ignores it)
- Model name: `dify` (or anything — passed through as label)
- Completion mode: Chat, Streaming: on
- **Do not** use `/dify/dify/v1` — Dify auto-appends `/chat/completions` to whatever you paste.

*(Optional)* If you want to embed us as a Dify **App** rather than as a model provider, hit `POST /dify/dify/v1/chat-messages` directly — that endpoint speaks Dify's App Chat API with `event: message` / `event: message_end` SSE frames.

**Local OpenWebUI** (Docker Desktop through the EC2 Tailscale relay):
- Base URL: `http://100.79.116.60:18080/harness/v1`
- Enable `ENABLE_FORWARD_USER_INFO_HEADERS=true`.
- `/harness` requires `X-OpenWebUI-User-Id` and `X-OpenWebUI-Chat-Id`; missing identity fails closed with `identity_context_required`.
- Proxy mapping: `actorId=openwebui:<user-id>` and `runtimeSessionId=owui-<user-id>-<chat-id>`.
- Foreground requests send only the latest user turn because Harness persists chat history. Calls sharing that session are serialized by the single proxy replica.
- OpenWebUI background tasks use a fresh `owui-bg-*` session, `actorId=openwebui-task:<user-id>`, and no chat file manifest.
- `openwebui-local/start.sh` idempotently installs the global `agentcore_file_context` filter. It modifies only the AgentCore harness model.
- Caddy listens only on the EC2 Tailscale address and has request access logging disabled.

If the proxy is scaled above one replica, replace the in-process per-session
lock with distributed session coordination before accepting concurrent
foreground calls.

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

## Part 4b — S3 Tables backend for `ah-analytics` (Iceberg + Athena)

Parallel path to Part 4. Source parquet files in `s3://ah-data-analytics/` are event-loaded into a managed S3 Tables (Iceberg) bucket and queried by the agent via a new Athena-backed MCP Gateway. No RDS hop.

### 4b.1 Bootstrap (one-off)

```bash
python3 infra/ah_s3tables_bootstrap.py
```

Creates:
- S3 Tables bucket `ah-analytics` (`arn:aws:s3tables:ap-southeast-1:964340114883:bucket/ah-analytics`)
- Glue Data Catalog federation (parent catalog `s3tablescatalog`, child `s3tablescatalog/ah-analytics`)
- Namespace `ah_analytics`
- Adds current caller as Lake Formation admin
- Athena workgroup `ah-s3tables-wg` with results at `s3://agentcore-tmp-964340114883/athena-results/`
- LF grants (DESCRIBE + SELECT) on `ah_analytics.*` to the MCP role once it exists

Idempotent — re-run after deploying the MCP Lambda so the LF grant picks up the new role.

### 4b.2 Loader Lambda (container image, S3-event triggered)

```bash
python3 lambda_s3tables_loader/deploy.py
```

Builds and pushes an x86_64 container to ECR (`ah-analytics-s3tables-loader`), creates the Lambda (10 GB memory, 15-min timeout, 4 GB `/tmp`), and configures S3 notifications on `ah-data-analytics` so each `Combined_*_encoded.parquet.gzip` upload fires one loader invocation per file.

The loader uses PyIceberg with the S3 Tables REST catalog. It reuses `parse_mixed_date_fast` and `sanitise_column_name` from `infra/ah_transforms.py` (shared with the Fargate RDS ETL). All column names are lowercased (Glue federation requirement). Tables are partitioned by `month(<date_col>)`.

Trigger the initial full load without re-uploading source files:

```bash
for KEY in Combined_SOC_encoded.parquet.gzip Combined_UCC_encoded.parquet.gzip \
           Combined_adm_encoded.parquet.gzip Combined_disch_encoded.parquet.gzip \
           Combined_inflight_encoded.parquet.gzip Combined_procedure_encoded.parquet.gzip; do
  jq -n --arg k "$KEY" '{Records:[{s3:{bucket:{name:"ah-data-analytics"},object:{key:$k}}}]}' > /tmp/ev.json
  aws lambda invoke --function-name ah-analytics-s3tables-loader \
    --payload fileb:///tmp/ev.json --cli-binary-format raw-in-base64-out \
    --invocation-type Event --region ap-southeast-1 /tmp/out.json
done
```

### 4b.3 MCP Lambda + Gateway + harness wire-up

```bash
python3 mcp_lambda_s3tables/deploy.py
python3 infra/ah_s3tables_bootstrap.py   # re-run to grant LF to the new MCP role
```

Creates:
- Lambda `ah-analytics-s3tables-mcp` (zip, boto3-only, no VPC — Athena is a public API)
- IAM role `ah-analytics-s3tables-mcp-role` (Athena + Glue + s3tables read + Lake Formation `GetDataAccess`)
- Gateway `ah-analytics-s3tables` (MCP, AWS_IAM auth)
- Gateway target `ah-s3tables-tools` — same 3 tools as `ah-rds-tools`: `execute_sql`, `list_tables`, `describe_table`
- Added to `harness_e52fs` alongside the RDS gateway

Tool names in Strands become `ah-s3tables-tools___execute_sql`, etc — no collision with `ah-rds-tools___execute_sql`.

### Verification

```bash
# List tables
echo '{}' | aws lambda invoke --function-name ah-analytics-s3tables-mcp \
  --payload fileb:///dev/stdin --cli-binary-format raw-in-base64-out \
  --region ap-southeast-1 /dev/stdout

# Athena query with partition pruning (typically scans <1 MB on 1M-row outpatient)
aws athena start-query-execution \
  --query-string "SELECT year(visit_date) y, month(visit_date) m, COUNT(*) c FROM outpatient \
                  WHERE visit_date >= TIMESTAMP '2024-01-01' GROUP BY 1,2 ORDER BY 1,2" \
  --work-group ah-s3tables-wg \
  --query-execution-context Catalog=s3tablescatalog/ah-analytics,Database=ah_analytics \
  --region ap-southeast-1
```

---

## Part 4c — File uploads + Code Interpreter analysis

Lets a user drag a supported file into Dify or local OpenWebUI and have the
harness invoke Code Interpreter conditionally when the prompt requires file
processing.

### 4c.1 Bootstrap (one-off)

```bash
python3 infra/user_uploads_bootstrap.py
```

Creates / ensures:
- S3 bucket `agentcore-user-uploads-964340114883` with layout `uploads/{actor_id}/{conversation_id}/{filename}`, 24-h lifecycle rule, block public access
- IAM role `agentcore-code-interpreter-role` (S3 GetObject on `uploads/*`)
- Code Interpreter sandbox `agentcore_user_uploads_ci` (`networkMode=SANDBOX` — S3 only, no public internet)
- Adds `agentcore_code_interpreter` tool to both `harness_e52fs` and `harness_dify` (existing tools preserved)
- Grants each harness execution role `bedrock-agentcore:StartCodeInterpreterSession / Invoke*` on the CI ARN
- Grants `agentcore-proxy-irsa` `s3:PutObject` on the uploads bucket
- Grants the proxy metadata/tag-only validation (prefix-restricted
  `ListBucket`, plus `GetObjectTagging`) on
  `s3://agentcore-openwebui-test-964340114883/openwebui-test/*`; the proxy has
  no content-read permission on that prefix

Idempotent — re-run safe.

### 4c.2 Proxy endpoints

- **OpenAI-compatible**: `POST /v1/files` — multipart `file` + `purpose` + `user` (actor id) + optional `conversation_id`. Returns `{id, object:"file", bytes, filename, purpose}` where `id` is the S3 key.
- **Dify App API**: `POST /dify/{slug}/files/upload` — multipart `file` + `user`. Returns Dify's schema `{id, name, size, extension, mime_type, created_by, created_at}`.

Both routes stream into `s3://agentcore-user-uploads-964340114883/uploads/{actor_id}/{conversation_id}/{filename}`. The proxy trusts `actor_id` (it authenticated the request) and inserts it into the S3 key.

Allowed extensions: `csv, xlsx, xls, pdf, docx, pptx, txt, md, json`. Max size 50 MB. Bad extension → HTTP 400.

### 4c.3 Message-injection hook

When a chat request (OpenAI or Dify) contains a `files[]` array referencing an upload id, the proxy:

1. **Verifies each file's S3-key prefix matches the requester's `actor_id`** — a mismatch is silently dropped and logged as `Rejected file access: actor=X tried to reference file owned by Y`. Prevents cross-actor data leaks even if a file id is guessed or forged.
2. Prepends a system-visible line to the user message so the agent knows the S3 URI:
   ```
   [Uploaded file: s3://agentcore-user-uploads-.../uploads/{actor}/{conv}/{name}
    (name: sales.xlsx, 24138 bytes, type: application/vnd.openxmlformats-...)]
   <original user query>
   ```

The harness's Code Interpreter tool downloads from S3 with its own execution role, runs the analysis, and streams the answer back.

### 4c.4 Local OpenWebUI S3 handoff and isolation

Run `openwebui-local/start.sh`. Its global filter gathers file ids from the
current chat, resolves each with OpenWebUI's owner-scoped database method, and
sends `agentcore_files` metadata only for the AgentCore harness request. Native
RAG fields are removed from that request.

The proxy validates every manifest entry against the allowlisted S3
bucket/prefix and the object tags `OpenWebUI-User-Id` and
`OpenWebUI-File-Id`. It obtains authoritative object size/existence from the
prefix-restricted S3 listing.
Validation is all-or-nothing: any bad, missing, unowned, unsupported or
over-limit object rejects the whole request before AgentCore invocation.

Files remain available throughout the same chat while attached, with limits of
50 MiB per file, 10 files, and 200 MiB combined. Another user—including a
shared-chat viewer—cannot process the owner's files.

The injected system context tells the harness to access only the validated
URIs, treat file content as untrusted data, invoke Code Interpreter only when
needed, and avoid echoing raw S3 URIs unless requested.

This is trusted-frontend POC isolation. Production must replace the plain
identity headers with a signed, short-lived JWT and narrow the Code Interpreter
role to object-scoped access. Dify compatibility remains a separate phase.

### 4c.5 Verification

```bash
# Health + upload round-trip
kubectl -n agentcore port-forward svc/agentcore-proxy 8080:80 &
curl -X POST http://localhost:8080/dify/harness/files/upload \
  -F 'file=@sales.csv' -F 'user=alice-test'
# → {"id":"uploads/alice-test/{uuid}/sales.csv", "name":..., "size":...}

aws s3 ls s3://agentcore-user-uploads-964340114883/uploads/ --recursive
# → 2026-07-18 11:47:21  63 uploads/alice-test/{uuid}/sales.csv

# End-to-end analysis via the harness (Dify blocking mode)
curl -X POST http://localhost:8080/dify/harness/v1/chat-messages \
  -H 'Content-Type: application/json' \
  -d '{
    "query":"Use the Code Interpreter to load the uploaded CSV, count rows, sum revenue.",
    "user":"alice-test",
    "conversation_id":"conv-<uuid-must-be-33+chars>",
    "response_mode":"blocking",
    "files":[{"type":"document","transfer_method":"local_file","upload_file_id":"<id-from-upload>"}]
  }'
```

Expected: agent returns row count + revenue sum, showing the S3 path it downloaded from.

Note: `runtimeSessionId` (= `conversation_id`) must be ≥ 33 characters — Dify's UI passes UUIDs which meet this; direct curl callers need to pad.

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

## Part 6 — AgentCore Harnesses + Skills Bucket (AWS Console)

The two harnesses (`harness_e52fs`, `harness_dify`) and the shared memory instance are provisioned through the **AWS console**, not code. This is intentional — the console UI is the only interface that exposes all harness configuration knobs (memory strategies, gateway attachment, skills path, model choice, prompt library).

Steps for each harness:

1. **Bedrock → AgentCore → Harnesses → Create harness.** Name: e.g. `harness_e52fs` (OpenWebUI) or `harness_dify` (DIFY).
2. **Model**: pick a cross-region profile like `global.anthropic.claude-sonnet-4-6` or an application inference profile ARN.
3. **Memory**: **Enable**. Add both strategies:
   - Semantic — namespace `/actors/{actorId}/facts/`
   - Summarization — namespace `/actors/{actorId}/summaries/{sessionId}/`

   Both harnesses can point at the SAME memory instance (`harness_harness_e52fs_8d3d-vtE3DJC9ia`) for unified user memory across frontends — this is how the current deployment is configured.
4. **Gateways / Tools**: attach the 3 gateways `nuh-analytics-db`, `ah-analytics-db`, `timesfm-gateway`. Every time you add a gateway to a harness, three things must happen (AWS does the first automatically):
   - `update_harness(tools=...)` — via console
   - Add the gateway ARN to the harness's execution IAM policy `AmazonBedrockAgentCoreHarnessGatewayPolicy_*`
   - Restart/reload (usually implicit)
5. **Skills**: point at the repo path where your `SKILL.md` and `Skill_*.md` files live. **Use a plain path** like `.claude/Skills` — NOT `tree/main/.claude/Skills` (the URL fragment form throws `Skill path 'tree/main/...' not found in repository` at invocation time).

**Skills bucket for the poc runtime** (separate from the harness skills path): the poc container syncs from `s3://ah-data-analytics/skills/` at startup. Upload skills there once:

```bash
aws s3 sync .claude/Skills/ s3://ah-data-analytics/skills/
```

The runtime IAM role provisioned by `infra/deploy.py` already grants `s3:GetObject`/`ListBucket` on this bucket.

After creating each harness, capture the ARN and add it to the proxy's `HARNESSES` dict in `proxy/server.py`, then redeploy the proxy. The proxy is the single place where slug→backend mapping lives; adding a new frontend is one line.

---

## Part 7 — Data Ingestion into RDS

RDS is in private subnets — not reachable from a developer Mac. Use ECS Fargate task inside the VPC.

### 7.1 nuh-analytics

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

### 7.2 ah-analytics

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

### 7.3 PII masking

```bash
python3 infra/mask_pii.py           # requires VPC access — run from an ECS task
```

Masks phone numbers and street addresses in `emd`, `inpatient_movement`, and any table containing `RESIDENT_TEL`, `CONTACT_TEL`, `ADDRESS1`, `ADDRESS2`, `BLOCK_BUILD`. Postal codes preserved.

---

## Updating After Code Changes

| Change | Rebuild + redeploy |
|--------|---------------------|
| `app/` (agent container) | `bash infra/build_and_push.sh && ECR_IMAGE_URI=... python3 infra/deploy.py` |
| `proxy/server.py` | `bash proxy/build_and_push.sh && kubectl rollout restart deployment/agentcore-proxy -n agentcore` |
| `mcp_lambda/handler.py` (both DBs use same handler) | `python3 mcp_lambda/deploy.py && python3 mcp_lambda/deploy_ah.py` |
| `timesfm_service/server.py` | `bash timesfm_service/build_and_push.sh && kubectl rollout restart deployment/timesfm-service -n agentcore` |
| `timesfm_mcp/handler.py` | `NLB_ENDPOINT=http://... python3 timesfm_mcp/deploy.py` |
| Adding a new frontend/backend | Add a line to `RUNTIMES` or `HARNESSES` in `proxy/server.py`, rebuild + roll the proxy. All routes (`/{slug}/v1/...`, `/dify/{slug}/v1/chat-messages`) auto-mount |
| Adding Agent Skills to the poc runtime | `aws s3 sync .claude/Skills/ s3://ah-data-analytics/skills/` then restart the runtime (deploy.py or force new revision) |
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
| Dify Model Provider validation "404 Not Found" | Wrong Base URL — Dify auto-appends `/chat/completions` | Set Base URL to `<host>/{slug}/v1`, NOT `<host>/dify/{slug}/v1` |
| Dify "role failed to satisfy enum [user, assistant]" | Dify sends a `system` message but `invoke_harness` only accepts user/assistant | Fixed — proxy hoists system messages into the harness `systemPrompt` field |
| Harness "Skill path 'tree/main/.claude/Skills' not found" | Skills path in harness config is a GitHub URL fragment | Set to plain repo path like `.claude/Skills` |
| Poc container: `actor=None, session=<random-uuid>` in logs | AgentCore Runtime dropped `runtimeUserId` header; body has no `user_id` | Ensure the caller passes `runtimeUserId` (boto3) or `user_id`/`chat_id` in the JSON body |
| OpenWebUI cross-chat memory doesn't work on `/poc` | OpenWebUI backend strips `user_id` when calling external OpenAI providers | See REFLECTION.md finding 33 — currently TODO. Use `/harness` or `/dify` for OpenWebUI |
| Container can't resolve `<gw-id>.gateway.bedrock-agentcore.<region>.amazonaws.com` | Missing VPC endpoint for the `.gateway` subdomain | Create `com.amazonaws.<region>.bedrock-agentcore.gateway` — different from the plain `bedrock-agentcore` endpoint |
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
| AgentCore harness (OpenWebUI) | `arn:aws:bedrock-agentcore:ap-southeast-1:964340114883:harness/harness_e52fs-Du2DM0RxvF` |
| AgentCore harness (DIFY) | `arn:aws:bedrock-agentcore:ap-southeast-1:964340114883:harness/harness_dify-LViqrsm86E` |
| Shared memory (both harnesses + poc) | `arn:aws:bedrock-agentcore:ap-southeast-1:964340114883:memory/harness_harness_e52fs_8d3d-vtE3DJC9ia` |
| S3 Skills bucket (poc runtime) | `s3://ah-data-analytics/skills/` |
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
| Gateway (AH S3 Tables) | `ah-analytics-s3tables-uhtyjdutj7` |
| Gateway (TimesFM) | `timesfm-gateway-w4fho4r9um` |
| Lambda (NUH MCP) | `arn:aws:lambda:ap-southeast-1:964340114883:function:nuh-analytics-mcp` |
| Lambda (AH MCP) | `arn:aws:lambda:ap-southeast-1:964340114883:function:ah-analytics-mcp` |
| Lambda (AH S3 Tables MCP) | `arn:aws:lambda:ap-southeast-1:964340114883:function:ah-analytics-s3tables-mcp` |
| Lambda (AH S3 Tables loader) | `arn:aws:lambda:ap-southeast-1:964340114883:function:ah-analytics-s3tables-loader` |
| Lambda (TimesFM MCP bridge) | `arn:aws:lambda:ap-southeast-1:964340114883:function:timesfm-mcp` |
| S3 Uploads bucket | `agentcore-user-uploads-964340114883` (layout `uploads/{actor_id}/{conversation_id}/{filename}`) |
| Code Interpreter (uploads) | `arn:aws:bedrock-agentcore:ap-southeast-1:964340114883:code-interpreter-custom/agentcore_user_uploads_ci-iZOyjlk0GA` |
| Code Interpreter role | `arn:aws:iam::964340114883:role/agentcore-code-interpreter-role` |
| S3 Tables bucket (AH) | `arn:aws:s3tables:ap-southeast-1:964340114883:bucket/ah-analytics` |
| Athena workgroup (AH S3 Tables) | `ah-s3tables-wg` |
| Federated Glue catalog (AH S3 Tables) | `s3tablescatalog/ah-analytics` |
| ECR (AH S3 Tables loader) | `964340114883.dkr.ecr.ap-southeast-1.amazonaws.com/ah-analytics-s3tables-loader` |
| Harness gateway policy | `arn:aws:iam::964340114883:policy/service-role/AmazonBedrockAgentCoreHarnessGatewayPolicy_bd7bg` |

## VPC Interface Endpoints (bot-nuhs-vpc, ap-southeast-1)

| Endpoint ID | Service | Purpose |
|---|---|---|
| `vpce-0b582d02606dfbe00` | `bedrock-runtime` | Bedrock InvokeModel calls |
| `vpce-0d7da6165d12a2ae8` | `bedrock-agentcore` | AgentCore control plane + `invoke_agent_runtime` / `invoke_harness` |
| `vpce-0265c2f3efe0f6151` | `bedrock-agentcore.gateway` | AgentCore Gateway MCP calls (`<gw-id>.gateway.bedrock-agentcore...`) — separate from the plain `bedrock-agentcore` endpoint |
| `vpce-059f7b6613b722983` | `secretsmanager` | Currently unused by the poc runtime; kept for ETL / harness |
| `vpce-02600a734df24aff5` | `ecr.api` | ECR image pull |
| `vpce-084fe8036d1b6e33b` | `ecr.dkr` | ECR image pull |
| `vpce-0cb3dca98becb59a1` | S3 Gateway | S3 (Skills sync, ETL staging) |

All Interface Endpoints use security group `sg-0be4a7ae0ed2caf17` (vpc-endpoints-sg), allow 443 inbound from `10.0.0.0/16`.

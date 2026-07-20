# AgentCore Data Analytics Platform — Product Requirements Document

## Goal

Expose natural-language analytics over hospital operations data (`nuh-analytics`, `ah-analytics` PostgreSQL DBs) plus time-series forecasting (Google TimesFM), delivered through both **OpenAI-compatible** and **Dify App API** shapes so any modern chat frontend (Open WebUI, Dify, LangChain, custom SDK) can plug in.

The agent is hosted on **AWS AgentCore**, with three backends behind a single VPC-internal proxy:

- **`agentcore_poc`** — Claude Agent SDK runtime (self-hosted container) with Gateway MCP tools, S3-loaded Skills, and streaming token deltas.
- **`harness_e52fs`** — AWS-managed Strands harness serving OpenWebUI. Managed memory + tool routing.
- **`harness_dify`** — AWS-managed Strands harness serving Dify. Same tool set as `harness_e52fs`, isolated skill/memory scope.

All three share the same four Gateway MCP tool backends: `nuh-analytics-db`, `ah-analytics-db`, `ah-analytics-s3tables` (Athena over Iceberg), `timesfm-gateway`.

## Non-goals

- Public internet exposure. Everything is VPC-internal; auth is not enforced at the proxy layer.
- Fine-grained per-user authorization on tool calls. All frontend users see the same tool set.
- Real-time (< 1 s) tool responses. LLM turns take 2–15 s end-to-end.

---

## Architecture

```
Client (Open WebUI, Dify, custom)
        │
        │  HTTP POST /<slug>/v1/chat/completions       (OpenAI shape)
        │       or  /dify/<slug>/v1/chat-messages       (Dify App shape)
        │  <slug> ∈ { poc, harness, dify }
        ▼
EKS Fargate proxy pod (namespace: agentcore, amd64)
        │
        ├── boto3.invoke_agent_runtime()   → agentcore_poc runtime
        │
        └── boto3.invoke_harness()          → harness_e52fs   (OpenWebUI)
                                              harness_dify    (Dify)
        (IAM via IRSA; VPC endpoints for bedrock-agentcore)

AgentCore backends (ap-southeast-1, private VPC)
        │
        ├── agentcore_poc  (Claude Agent SDK container, arm64)
        │     ├── /app/skills/  ← sync'd from s3://ah-data-analytics/skills/ at boot
        │     ├── in-process SigV4 signing proxy on 127.0.0.1:9000
        │     └── Claude Agent SDK ─→ HTTP MCP servers ─→ localhost:9000 ─→ Gateway MCP endpoints
        │
        └── harness_e52fs / harness_dify  (AWS-managed, Strands)
              ├── memory strategies: semantic + summarization
              ├── skills: loaded from a GitHub repo path
              └── model: global.anthropic.claude-sonnet-4-6

4 Gateway MCP endpoints (shared by all backends)
        │
        ├── nuh-analytics-db      ─→ Lambda nuh-analytics-mcp             ─→ RDS nuh-analytics
        ├── ah-analytics-db       ─→ Lambda ah-analytics-mcp              ─→ RDS ah-analytics
        ├── ah-analytics-s3tables ─→ Lambda ah-analytics-s3tables-mcp     ─→ Athena ─→ S3 Tables (Iceberg) ah-analytics
        └── timesfm-gateway       ─→ Lambda timesfm-mcp (bridge)          ─→ NLB ─→ EKS timesfm-service (CPU)

S3-event data pipeline for the Iceberg backend
        │
        └── S3 ObjectCreated on s3://ah-data-analytics/Combined_*_encoded.parquet.gzip
              └── Lambda ah-analytics-s3tables-loader (container, PyIceberg) ─→ S3 Tables ah-analytics

User-file upload pipeline (per-actor scoped)
        │
        ├── Client upload           →  Proxy /v1/files or /dify/{slug}/files/upload
        │                                 │
        │                                 ▼
        │                             s3://agentcore-user-uploads-.../uploads/{actor_id}/{conv_id}/{filename}
        │                             (24-h lifecycle; block public access)
        │
        └── Chat message w/ file id →  Proxy verifies actor prefix, injects S3 URI into user turn
                                        │
                                        ▼
                                     Harness invokes AgentCore Code Interpreter
                                     (SANDBOX network, pandas/pypdf/python-docx/pptx pre-installed)
                                        │
                                        └─→ downloads from S3 via CI execution role → returns analysis
```

---

## Key Design Decisions

| Decision | Choice | Reason |
|---|---|---|
| Three backends behind one proxy | Slug-based routing (`/poc`, `/harness`, `/dify`) driven by a single `HARNESSES` / `RUNTIMES` dict | New frontend = one line of config, no new deployment |
| Two API shapes on one proxy | OpenAI (`/{slug}/v1/chat/completions`) + Dify App (`/dify/{slug}/v1/chat-messages`) | Dify's OpenAI-compatible provider covers 90% of cases; App shape unlocks native `conversation_id` semantics for future embedders |
| Poc runtime uses Gateway MCP instead of direct RDS | Removed `app/db.py`, `app/tools.py` | Gateway is IAM-gated, versioned, and shared across all backends — single source of truth for tool schemas |
| Container SigV4 for Gateway | In-process signing proxy on `127.0.0.1:9000` (daemon thread) | Claude Agent SDK's `McpHttpServerConfig` only accepts static headers; AgentCore Gateway requires per-request SigV4 |
| Skills sync from S3 at container boot | `app/skills_sync.py` | Update skills without rebuilding the container image; the harness backends load skills from a GitHub path — both patterns are fine |
| Memory bridge for the poc runtime | Manual `app/memory.py` calling `retrieve_memory_records` + `create_event` | Claude Agent SDK has no AgentCore Memory bridge; `ClaudeAgentOptions.session_id`/`resume`/`user` are for the local CLI transcript, unrelated |
| Streaming on `/poc` | `ClaudeAgentOptions(include_partial_messages=True)` + consume `StreamEvent` `content_block_delta` | AgentCore Runtime DOES stream — the SDK just needs the flag; without it, only end-of-turn `AssistantMessage` is emitted |
| Identity forwarding | Header preferred, body fallback | AgentCore forwards `runtimeSessionId` as HTTP header but silently drops `runtimeUserId`; proxy also injects both into the payload body so the container can read either source |
| System-role hoisting for `invoke_harness` | Proxy splits `role: system` into the harness's separate `systemPrompt` field | `invoke_harness` rejects any role outside `[user, assistant]`; Dify sends system messages during Model Provider validation |
| Agent container arch | `linux/arm64` | AgentCore Runtime supported platform |
| Proxy / MCP Lambda / TimesFM arch | `linux/amd64` | EKS Fargate nodes are amd64 |
| No auth on the proxy | Anonymous VPC-internal | The internal NLB is only reachable from the peered VPCs; frontend auth is out of scope |

---

## Backend Feature Matrix

| Capability | `/poc` (Claude Agent SDK) | `/harness` (Strands, OpenWebUI) | `/dify` (Strands, Dify) |
|---|---|---|---|
| Token-level streaming | ✅ | ✅ | ✅ |
| Gateway MCP tools (nuh, ah, timesfm) | ✅ (via localhost SigV4 proxy) | ✅ (native) | ✅ (native) |
| Agent Skills | ✅ from S3 | ✅ from GitHub repo | ✅ from GitHub repo |
| System prompt from frontend (`role: system`) | ✅ merged into base prompt | ✅ hoisted into `systemPrompt` field | ✅ hoisted into `systemPrompt` field |
| Cross-session memory (semantic + summarization) | ⚠️ partial — works via direct boto3, broken from OpenWebUI (see below) | ✅ | ✅ |
| Managed cold-start / scaling | ✅ (AgentCore Runtime) | ✅ (AgentCore Harness) | ✅ (AgentCore Harness) |
| Per-session micro-VM isolation | ✅ | ✅ | ✅ |

### Known limitation — memory on `/poc` from OpenWebUI

OpenWebUI's backend→external-OpenAI-provider outbound request strips `chat_id` and `user_id` (only `{model, messages, stream}` reaches the proxy). Without a user identity, `app/memory.py` short-circuits both save and retrieve. Direct boto3 calls that pass `runtimeUserId` DO work end-to-end.

The local OpenWebUI `/harness` path is fixed: OpenWebUI forwards identity
headers and the proxy derives a namespaced `actorId` plus a chat-scoped
`runtimeSessionId`. `/poc` remains outside this POC filter and still has the
limitation above.

---

## API Surface

### OpenAI-compatible (recommended for most integrations)

```
GET  /{slug}/v1/models
POST /{slug}/v1/chat/completions
```

Where `{slug}` is one of `poc`, `harness`, `dify`. Body is standard OpenAI: `{model, messages, stream}`. On `stream=true`, emits `text/event-stream` SSE with `data: {json}\n\n` chunks and terminating `data: [DONE]\n\n`.

Session/user identity is carried via body fields for legacy/runtime paths:
- `chat_id` → `runtimeSessionId`
- `model_item.info.user_id` → `actorId` (harness) / `runtimeUserId` (runtime)

For local OpenWebUI `/harness`, both `X-OpenWebUI-User-Id` and
`X-OpenWebUI-Chat-Id` are required:
- `X-OpenWebUI-User-Id` → `actorId=openwebui:<user-id>`
- both values → `runtimeSessionId=owui-<user-id>-<chat-id>`

### Dify App Chat API

```
POST /dify/{slug}/v1/chat-messages
```

Body: `{query, user, conversation_id, response_mode, inputs}`. Session mapping:
- `conversation_id` → `runtimeSessionId` (echoed back so client can reuse)
- `user` → `actorId` / `runtimeUserId`

Streaming (`response_mode: "streaming"`) emits Dify SSE events: `event: message` per delta, terminating `event: message_end` with `metadata.usage`. Errors mid-stream → `event: error` at HTTP 200. Blocking mode (`response_mode: "blocking"`) returns a single JSON body matching Dify's spec.

---

## AWS Resources

### AgentCore
- **Runtime:** `agentcore_poc` — ARN `arn:aws:bedrock-agentcore:ap-southeast-1:964340114883:runtime/agentcore_poc-iumXW8638m`
- **Harness (OpenWebUI):** `harness_e52fs` — ARN `arn:aws:bedrock-agentcore:ap-southeast-1:964340114883:harness/harness_e52fs-Du2DM0RxvF`
- **Harness (Dify):** `harness_dify` — ARN `arn:aws:bedrock-agentcore:ap-southeast-1:964340114883:harness/harness_dify-LViqrsm86E`
- **Shared memory:** `arn:aws:bedrock-agentcore:ap-southeast-1:964340114883:memory/harness_harness_e52fs_8d3d-vtE3DJC9ia`
- **Gateways:** `nuh-analytics-db-fhbzdmtdta`, `ah-analytics-db-gszih4adsx`, `ah-analytics-s3tables-uhtyjdutj7`, `timesfm-gateway-w4fho4r9um`
- **S3 Tables bucket (AH Iceberg backend):** `arn:aws:s3tables:ap-southeast-1:964340114883:bucket/ah-analytics`, namespace `ah_analytics`, Athena workgroup `ah-s3tables-wg`, federated Glue catalog `s3tablescatalog/ah-analytics`
- **User uploads bucket:** `agentcore-user-uploads-964340114883` (`uploads/{actor_id}/{conversation_id}/{filename}`, 24-h lifecycle)
- **Code Interpreter (uploads):** `agentcore_user_uploads_ci` — attached to both harnesses; SANDBOX network mode; pandas/openpyxl/pypdf/python-docx/python-pptx/matplotlib pre-installed
- **Inference profile:** `arn:aws:bedrock:us-east-1:964340114883:application-inference-profile/ji5jakx5lho3`

### EKS Proxy
- **Namespace:** `agentcore` (Fargate wildcard profile)
- **Deployment:** `agentcore-proxy` (amd64)
- **ServiceAccount + IRSA:** `agentcore-proxy` / role `agentcore-proxy-irsa`
- **Cluster DNS:** `http://agentcore-proxy.agentcore.svc.cluster.local`
- **Internal NLB:** `http://k8s-agentcor-agentcor-a9dbd8956e-c923dee5a7cceccb.elb.ap-southeast-1.amazonaws.com`

### RDS PostgreSQL
- **Endpoint:** `jinxin-postgres.cf7in3efovlt.ap-southeast-1.rds.amazonaws.com` (PubliclyAccessible: false)
- **Databases:** `nuh-analytics`, `ah-analytics`
- **Credentials:** `arn:aws:secretsmanager:ap-southeast-1:964340114883:secret:agentcore-rds-credentials-tlv56J`

### S3
- **Skills bucket:** `s3://ah-data-analytics/skills/` — poc runtime syncs from here on startup

### VPC Interface Endpoints (bot-nuhs-vpc)

| Endpoint ID | Service | Notes |
|---|---|---|
| `vpce-0b582d02606dfbe00` | `bedrock-runtime` | Bedrock InvokeModel |
| `vpce-0d7da6165d12a2ae8` | `bedrock-agentcore` | `invoke_agent_runtime` / `invoke_harness` |
| `vpce-0265c2f3efe0f6151` | `bedrock-agentcore.gateway` | Gateway MCP subdomain — SEPARATE endpoint |
| `vpce-059f7b6613b722983` | `secretsmanager` | |
| `vpce-02600a734df24aff5` | `ecr.api` | Image pull |
| `vpce-084fe8036d1b6e33b` | `ecr.dkr` | Image pull |
| `vpce-0cb3dca98becb59a1` | S3 Gateway | S3 |

All Interface Endpoints share security group `sg-0be4a7ae0ed2caf17` (443 inbound from `10.0.0.0/16`).

---

## Environment Variables

| Variable | Set in | Value / Description |
|---|---|---|
| `AWS_DEFAULT_REGION` | AgentCore Runtime env | `ap-southeast-1` |
| `CLAUDE_CODE_USE_BEDROCK` | AgentCore Runtime env | `1` |
| `AWS_REGION` / `AWS_DEFAULT_REGION` | `agent.py` subprocess env | `us-east-1` (inference profile region) |
| `MODEL_ARN` | (optional) AgentCore Runtime env | Override inference profile ARN |

RDS credentials, DB name, and DB user are no longer container env vars — Gateway MCP handles DB access.

---

## Frontend Connection Config

### Dify (OpenAI-API-compatible Model Provider)
- **Base URL:** `http://<host>/dify/v1` (dedicated `harness_dify` backend)
- **API Key:** any value (proxy ignores; VPC-internal only)
- **Model:** any label

For Dify hitting `/harness` or `/poc` instead, swap the slug.

### Open WebUI
- **Local base URL:** `http://100.79.116.60:18080/harness/v1`
- `ENABLE_FORWARD_USER_INFO_HEADERS=true`
- The automatically installed global filter adds chat-wide, owner-scoped S3
  file metadata only for the AgentCore harness model.
- The proxy validates ownership tags and limits before placing approved S3 URIs
  in system context for conditional Code Interpreter use.

### Direct Python (boto3)

```python
import boto3, json
client = boto3.client("bedrock-agentcore", region_name="ap-southeast-1")
resp = client.invoke_agent_runtime(
    agentRuntimeArn="arn:aws:bedrock-agentcore:ap-southeast-1:964340114883:runtime/agentcore_poc-iumXW8638m",
    contentType="application/json",
    accept="text/event-stream",
    runtimeSessionId="my-session-" + "x" * 25,   # ≥33 chars
    runtimeUserId="my-user-id",
    payload=json.dumps({"messages": [{"role": "user", "content": "..."}]}).encode(),
)
for line in resp["response"].iter_lines():
    print(line.decode())
```

---

## Open Items

- [ ] **Memory on `/poc` from OpenWebUI** — see "Known limitation" above. Need an OpenWebUI-side filter or setting.
- [ ] Populate real usage/token metrics in the Dify `message_end.metadata.usage` (currently zeros).
- [ ] `event: ping` keep-alive on the Dify streaming path if Dify clients disconnect on long silences (not observed yet).
- [ ] CloudWatch alarms on AgentCore Runtime error rate + p95 latency.
- [ ] Cross-region DR: model + memory strategy for a second region.

# Build Reflection — AgentCore Data Analytics Platform

This document captures what worked, what failed, root causes, and how to do it faster next time.

---

## What We Built

A production-grade multi-tool agent on AWS AgentCore, accessible from DIFY and Open WebUI via OpenAI-compatible API, running 100% within AWS private networking. No internet traffic. Three backends (one Claude Agent SDK runtime + two Strands harnesses — one per frontend), two database backends (`nuh-analytics`, `ah-analytics`), Google TimesFM time-series forecasting, S3-loaded Skills, AgentCore Memory, native token-level streaming, and both OpenAI and Dify API shapes served from a single proxy.

**Total wall-clock time:** ~6 days of iteration across multiple sessions.

## Current Status (as of the phase 2 branch)

| Capability | Status |
|---|---|
| Real token-level streaming from `/poc` | ✅ Verified end-to-end (156 SSE lines / 7.7 s) |
| Streaming from `/harness`, `/dify` | ✅ Native — pipes `contentBlockDelta` events straight through |
| OpenAI-compatible endpoints (`/{slug}/v1/chat/completions`) | ✅ Used by OpenWebUI and Dify Model Provider |
| Dify App-shape endpoints (`/dify/{slug}/v1/chat-messages`) | ✅ Streaming + blocking modes |
| Gateway MCP tools (nuh, ah, timesfm) on `/poc` via localhost SigV4 proxy | ✅ All three return 200 OK |
| S3 Skills sync at container startup | ✅ 7 files loaded on boot |
| System-prompt injection from frontend (role: system) | ✅ Merged into base prompt (poc) / hoisted into `systemPrompt` (harness) |
| AgentCore Memory on `/harness`, `/dify` | ✅ Cross-session recall works (~60 s ingestion lag) |
| **AgentCore Memory on `/poc` (Claude Agent SDK)** | ⚠️ **PARTIAL — TODO** |

### TODO: Cross-chat memory on the `/poc` backend

Direct boto3 tests with an explicit `runtimeUserId` DO save and retrieve memory correctly on `/poc`. But **from OpenWebUI**, memory does not work because OpenWebUI's backend → external-OpenAI outbound request strips user identity (see finding 33 below). Two ways to fix:
1. Add an OpenWebUI Function/Filter that injects `user_id`/`chat_id` into outbound bodies.
2. Find or enable OpenWebUI's "Include User Info" toggle so it emits `X-OpenWebUI-User-Id`, then read that header in the proxy.

The two managed harnesses (`/harness`, `/dify`) are unaffected — AgentCore Harness accepts `actorId` as a first-class API parameter that the proxy sets from whatever identity source it has.

---

## What Worked First Time

- **Claude Agent SDK MCP tool pattern** — `@tool` decorator + `create_sdk_mcp_server` worked exactly as documented
- **boto3 `bedrock-agentcore-control` API** — `create_agent_runtime`, `create_gateway`, `create_gateway_target` all straightforward once naming rules were understood
- **VPC peering between default VPC and bot-nuhs-vpc** — pre-existing, worked immediately
- **EKS wildcard Fargate profile** — covered new `agentcore` namespace with zero config
- **AgentCore Gateway idempotency** — re-running deploy scripts is safe; targets and gateways look themselves up by name
- **Baking model weights into Docker image** — for TimesFM, this eliminated all runtime HuggingFace dependency and was simpler than any S3-download approach

---

## What Failed and Why

### 1. AgentCore container health check path
**Failed:** Container had `POST /invoke` → AgentCore called `GET /ping` and `POST /invocations`
**Fix:** Add `/ping` GET endpoint; rename `/invoke` to `/invocations`
**Lesson:** AgentCore uses the Lambda container interface convention: health = `/ping`, invocations = `/invocations`.

### 2. AgentCore runtime name hyphens rejected
**Failed:** `agentcore-poc` → `ValidationException: pattern [a-zA-Z][a-zA-Z0-9_]{0,47}`
**Fix:** `agentcore_poc`
**Lesson:** Runtime names allow only alphanumeric + underscore. No hyphens.

### 3. `response["body"]` vs `response["response"]`
**Failed:** Wrapper crashed with `KeyError: 'body'`
**Fix:** `raw = response["response"].read()`
**Lesson:** `invoke_agent_runtime` returns the streaming body under `response["response"]`. Always inspect `list(response.keys())` when a new API client returns unexpected structure.

### 4. `ANTHROPIC_BASE_URL` doesn't work for the Claude CLI subprocess
**Failed:** Container returned `Not logged in · Please run /login`
**Fix:** Set `CLAUDE_CODE_USE_BEDROCK=1` and pass via `ClaudeAgentOptions(env={...})`
**Lesson:** `claude-agent-sdk` spawns a subprocess with its own env. Container-level env is NOT auto-inherited — pass explicitly via `ClaudeAgentOptions(env=...)`.

### 5. On-demand Bedrock model ID rejected
**Failed:** `400 Invocation of model ID anthropic.claude-sonnet-4-6 with on-demand throughput isn't supported`
**Fix:** Use inference profile ARN (`arn:aws:bedrock:us-east-1:<account>:application-inference-profile/<id>`) or cross-region profile prefix (`global.anthropic.*`)
**Lesson:** Newer Claude models require inference profiles in most regions.

### 6. Inference profile region mismatch
**Failed:** Profile in `us-east-1`, container env in `ap-southeast-1`
**Fix:** Override in subprocess env: `env={"AWS_REGION": "us-east-1"}`
**Lesson:** When the profile is in a different region from the container, override in `ClaudeAgentOptions(env=...)`.

### 7. IAM policy scope too narrow for InvokeAgentRuntime
**Failed:** `AccessDeniedException` even with policy scoped to `runtime/<name>`
**Root cause:** IAM check happens against endpoint ARN (`runtime/<name>/runtime-endpoint/DEFAULT`), not runtime ARN
**Fix:** `Resource: "*"`
**Lesson:** For `invoke_agent_runtime`, always scope with `*` unless you know the endpoint ARN pattern.

### 8. Lambda Function URL blocked by org SCP
**Failed:** 403 with `AuthType: NONE`
**Fix:** Switch to `AuthType: AWS_IAM`; use SigV4 or EKS proxy
**Lesson:** Check org SCPs early. `NONE` often blocked in org accounts.

### 9. EKS Fargate pod has no AWS credentials from execution role
**Failed:** `Unable to locate credentials` in pod
**Fix:** IRSA — create IAM role with OIDC trust, annotate ServiceAccount
**Lesson:** Fargate execution role and pod credentials are separate. Always set up IRSA.

### 10. Internal NLB stuck in pending
**Failed:** LoadBalancer service stayed `<pending>` indefinitely
**Root cause:** Private subnets missing `kubernetes.io/role/internal-elb=1` tag
**Fix:** Tag both subnets
**Lesson:** Pre-tag subnets when setting up EKS.

### 11. EKS proxy image architecture mismatch
**Failed:** `ImagePullBackOff: no match for platform`
**Fix:** `docker build --platform linux/amd64` (Fargate here is amd64)
**Lesson:** Explicitly set `--platform`. Never rely on default. Also: `agentcore-poc` is arm64 (AgentCore Runtime requirement), everything else is amd64 (Fargate). They diverge.

### 12. RDS not reachable from dev Mac
**Failed:** Timeout from local `pg_restore`
**Fix:** Run data ingestion via ECS Fargate task inside the VPC
**Lesson:** For private RDS: never try to make it temporarily public. Use ECS.

### 13. ECS task couldn't run `pg_restore` — no binary
**Failed:** `FileNotFoundError`
**Fix:** `apk add --no-cache postgresql-client` at task start; `user: root`
**Lesson:** Container tool installs need `user: root` in the task definition.

### 14. Lambda timeout at 120s for long agent queries
**Failed:** Agent queries timed out
**Fix:** Increase Lambda timeout to 300s; add Lambda Function URL (no API GW 29s limit)
**Lesson:** API Gateway has hard 29s max — incompatible with LLM agents.

### 15. Streaming showed all-at-once instead of token-by-token
**Failed:** Open WebUI showed the response appearing in a single burst, not streaming
**Root cause:** Proxy called `.forecast()` synchronously and returned the whole result, then wrapped it in one fake SSE chunk
**Fix:** Rewrote `_stream_harness_events` as a generator that yields each `contentBlockDelta` event as it arrives from AWS
**Lesson:** AWS's `invoke_harness` DOES stream — the naive buffering was the bug, not the API.

### 16. Harness cold-start 502 (silent retry didn't fire)
**Failed:** First message on a new session sometimes returned `502 Connection was closed`
**Root cause:** Retry code caught `EventStreamError` but the actual exception was `ConnectionClosedError` — a completely different class (`HTTPClientError` vs `BotoCoreError`)
**Fix:** `except (botocore.exceptions.ConnectionClosedError, botocore.exceptions.EventStreamError)`
**Lesson:** Never assume the exception type — test the retry logic against a real cold-start failure, or the code path is dead.

### 17. AgentCore Memory silently not working
**Failed:** Semantic memory never retrieved anything across sessions
**Root cause:** Proxy generated a fresh `uuid4()` for `runtimeSessionId` on every call, and passed no `actorId` — so every request was a cold session for the memory system
**Fix:** Map `chat_id` → `runtimeSessionId` and `model_item.info.user_id` → `actorId` from OpenWebUI request body
**Lesson:** AgentCore memory namespaces are keyed by `actorId` and `sessionId`. Without stable values, no memory. Also: this fixed the cold-start problem (same session ID reuses the warm container).

### 18. Second Gateway target had tool-name collision
**Failed:** Adding `ah-analytics-db` Gateway to the same harness caused `Tool name 'rds-tools___describe_table' already exists`
**Root cause:** Strands SDK builds tool names as `{target-name}___{tool-name}`. Both gateways had target `rds-tools` → collision.
**Fix:** Rename `ah-analytics-db`'s target to `ah-rds-tools`
**Lesson:** Every Gateway Target across a single harness must have a unique name.

### 19. Harness execution role missing new gateway ARN
**Failed:** After adding a new gateway to the harness, calls returned `403 Forbidden` from AWS
**Root cause:** `AmazonBedrockAgentCoreHarnessGatewayPolicy_<suffix>` had only the original gateway ARN
**Fix:** Add each new gateway ARN to that policy's `Resource` list
**Lesson:** Whenever a gateway is added to a harness, three things must happen: (a) `update_harness(tools=...)`, (b) add the gateway ARN to the harness execution IAM policy, (c) restart or reload.

### 20. Skills bucket S3 permission missing
**Failed:** `AccessDenied` on `s3:ListBucket` for the skills bucket
**Root cause:** Harness execution role had no S3 access
**Fix:** Add `s3:GetObject`, `s3:ListBucket` for `ah-data-analytics` to `AmazonBedrockAgentCoreHarnessExecutionPolicy_<suffix>`
**Lesson:** Skills stored in S3 require explicit S3 permission on the harness execution role.

### 21. Mixed date formats silently dropped 60% of ETL rows
**Failed:** After loading `ah-analytics`, `Adm_Date` had 56% NULL — but source data had 0% missing dates
**Root cause:** The data mixes three formats in one column:
- SAP era: `"2018-12-31 00:00:00"` (ISO datetime) or `"22.10.2018"` (European DD.MM.YYYY)
- EPIC era: `"2024-06-10"` (ISO date only)
- Plus `inflight.Inflight_Date`: `"5/6/2018"` (D/M/YYYY slash) — pandas parses as May 6 (M/D) → wrong values

pandas 3.x silently fails when ISO datetime and ISO date-only are mixed in the same series without `format="ISO8601"`.
**Fix:** `parse_mixed_date_fast()` — regex-classify each value, then apply the correct parser per group. Also handles `"04.02.0201"` (year 201 — garbage) by pre-filtering to the `[1678, 2262]` datetime64[ns] range.
**Lesson:** Never assume a date column has one format. Sample 20 values from spread positions in the column before writing the parser.

### 22. pandas 3.x StringDtype trip-up
**Failed:** `pd.to_datetime(series)` returned NaT for values that parse fine individually
**Root cause:** pandas 3.x will not silently mix `"YYYY-MM-DD HH:MM:SS"` with `"YYYY-MM-DD"` — they're different ISO variants
**Fix:** `pd.to_datetime(vals, format="ISO8601")` — handles both variants explicitly
**Lesson:** Always pass explicit `format="ISO8601"` when a column could have mixed ISO precisions.

### 23. Docker Desktop pulled 5GB of CUDA when we needed CPU-only PyTorch
**Failed:** First TimesFM image build silently pulled `nvidia-cublas`, `cuda-toolkit`, etc.
**Root cause:** `timesfm[torch]` extra pulls `torch` from PyPI which auto-picks GPU wheels
**Fix:** In Dockerfile, install `torch --index-url https://download.pytorch.org/whl/cpu` FIRST, then `pip install timesfm` (no `[torch]` extra)
**Lesson:** For CPU-only PyTorch in a container, always install torch from the CPU index explicitly BEFORE any package that depends on it.

### 24. TimesFM 2.x has completely different API from 1.x
**Failed:** `AttributeError: module 'timesfm' has no attribute 'TimesFm'`
**Fix:** Use `TimesFM_2p5_200M_torch.from_pretrained(...)` + `.compile(ForecastConfig(...))` before calling `.forecast(horizon, inputs)`
**Lesson:** Check the installed version's `dir(timesfm)` output before writing wrapper code.

### 25. TimesFM inputs must be equal-length
**Failed:** `ValueError: inhomogeneous shape` when calling `forecast()` with `per_core_batch_size=32` and a single input
**Fix:** Use `per_core_batch_size=1` for single-input inference; specify `max_context=512` (training context length for the 200m model)
**Lesson:** For low-QPS on-demand inference, batch_size=1 is simplest.

### 26. AgentCore Gateway `http.passthrough` requires HTTPS + specific `protocolType`
**Failed:** `ValidationException: endpoint must match ^https://... ; protocolType must be [A2A, CUSTOM, INFERENCE, MCP]`
**Root cause:** The internal NLB is HTTP-only; adding HTTPS requires ACM cert + custom domain
**Fix:** Use `mcp.lambda` target with a thin bridge Lambda in the VPC. The Lambda forwards Gateway MCP calls to the NLB over HTTP (fine for VPC-internal traffic)
**Lesson:** `http.passthrough` is for public internet endpoints with TLS. For internal EKS services, always use the Lambda-bridge pattern.

### 27. MCP `inputSchema` doesn't allow `default` field
**Failed:** `Unknown parameter in ... properties.horizon: "default"`
**Fix:** Move defaults into the description text instead of using JSON Schema `default`
**Lesson:** AgentCore's MCP schema is a subset of JSON Schema — `default`, `enum`, `oneOf` etc. may not be supported. Test with a minimal schema first.

### 28. `inlinePayload` — list for MCP, string for HTTP
**Failed:** Same field, different accepted types across target types
**Fix:**
- `mcp.lambda.toolSchema.inlinePayload` — list of dicts
- `http.passthrough.schema.source.inlinePayload` — JSON-stringified list

**Lesson:** When schema errors are cryptic, dig into `botocore` service model:
```python
ctrl._service_model.operation_model('CreateGatewayTarget').input_shape
```

### 29. IAM policy version limit (5 versions max)
**Failed:** `LimitExceeded: A managed policy can have up to 5 versions`
**Fix:** In deploy scripts, delete oldest non-default version before creating a new one
**Lesson:** Managed policy versioning is limited. Always trim in idempotent scripts.

### 30. IAM propagation timing for VPC Lambdas
**Failed:** `The provided execution role does not have permissions to call CreateNetworkInterface on EC2` — even after `put_role_policy` returned success
**Fix:** `time.sleep(20)` after creating role + attaching managed policy before `create_function` in VPC mode
**Lesson:** Role creation is eventually consistent. For VPC Lambda creation specifically, allow 15-20 seconds.

---

## Phase 2 Additions — Streaming, Memory, Gateway MCP, Multi-frontend

### 31. "AgentCore Runtime doesn't stream" — false alarm
**Failed:** Chunks arrived in a single burst 15 seconds after invocation on the `/poc` path. Initial assumption: AgentCore's ingress buffers.
**Root cause:** Two separate bugs stacked:
- The Claude Agent SDK only emitted `AssistantMessage` at end-of-turn — its `StreamEvent` `content_block_delta` events (token-level deltas) were not consumed.
- The proxy's sync stream iterator was called directly from the async event loop, so tokens buffered until the whole generator finished.

**Fix:** `ClaudeAgentOptions(include_partial_messages=True)` + consume `StreamEvent` events (`evt.event.type == "content_block_delta"`, `delta.type == "text_delta"`), AND wrap the botocore blocking iterator in `starlette.concurrency.iterate_in_threadpool`.
**Lesson:** Before blaming platform infrastructure, isolate the transport layer. A `/stream-test` endpoint that emits 20 chunks with `asyncio.sleep(0.5)` between them (no LLM) proved AgentCore streams correctly — the bug was 100% in our container.

### 32. `runtimeSessionId` is forwarded to the container, `runtimeUserId` is silently dropped
**Failed:** Container-side memory used `actor_id=None` even when the proxy correctly set `runtimeUserId` on `invoke_agent_runtime`.
**Root cause:** boto3's service model marks both fields as `location=header`, but AgentCore Runtime only forwards `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` to the container. The `-User-Id` header is consumed by AgentCore's own control-plane and NOT propagated. Verified by logging `request.headers` inside the container.
**Fix:** Belt-and-suspenders — read the session-id header when present, AND have the proxy inject `chat_id`/`user_id` into the payload body so the container reads them from either source.
**Lesson:** Don't trust the boto3 service-model definition to describe what the container actually receives. Log the incoming request headers on your container's first day and check.

### 33. OpenWebUI does NOT forward user identity to external OpenAI providers
**Failed:** Even with the proxy's fix for #32, OpenWebUI-driven cross-chat memory on `/poc` still failed.
**Root cause:** OpenWebUI's browser → backend request DOES carry `chat_id` and `model_item.info.user_id` (visible in devtools). But OpenWebUI's backend → external-OpenAI outbound request strips them — the wire only carries `{model, messages, stream}` + `Authorization: Bearer <token>`. No `X-*-User-Id` header either.
**Fix (not yet applied — see README TODO):** either enable an OpenWebUI Function/Filter that adds `user_id`/`chat_id` to the outbound body, or find/enable OpenWebUI's "Include User Info" toggle. The harness backends (`/harness`, `/dify`) work today because `invoke_harness` takes `actorId` as a first-class API parameter and the proxy can derive one from whatever it has.
**Lesson:** What the frontend HAS is not what the frontend SENDS. Log the exact wire body/headers before designing anything that depends on identity flowing through.

### 34. `invoke_harness` rejects `role: system`
**Failed:** Dify's OpenAI-shaped call included a system message → AWS returned `Value at 'messages.1.member.role' failed to satisfy constraint: Member must satisfy enum value set: [user, assistant]`.
**Root cause:** `invoke_harness` accepts only `user`/`assistant` in `messages`. System prompts belong in a **separate** `systemPrompt` field on the API call.
**Fix:** Rewrote `_normalize_messages` in the proxy to split system messages into `systemPrompt: [{text: "..."}]` and pass them alongside `messages`.
**Lesson:** OpenAI-compatible ≠ OpenAI. When adapting an OpenAI shape to a non-OpenAI backend, always check the target's role enum and hoist the incompatible role somewhere valid.

### 35. Dify Model Provider validation hits `/chat/completions`, not `/chat-messages`
**Failed:** Registering `http://<nlb>/dify/harness/v1` in Dify → `Credentials validation failed with status code 404`.
**Root cause:** Dify's OpenAI-API-compatible provider auto-appends `/chat/completions` to whatever Base URL you enter. It has zero awareness of Dify's own `/chat-messages` App API — those are two different surfaces.
**Fix:** Point Dify at the plain OpenAI-shaped path (`/harness/v1` or `/poc/v1` or `/dify/v1`) and let it append `/chat/completions`.
**Lesson:** The `/chat-messages` route we built for Dify is only for callers that talk to us **as a Dify App** (e.g. someone embedding this in another Dify-based product). For "register as a model provider" — the far more common use case — Dify uses the OpenAI protocol and only needs the existing OpenAI endpoints.

### 36. Async memory fact-extraction lag makes retrieval look broken
**Failed:** Turn 1 saved "my name is Charlie", turn 2 (new session) 5 seconds later couldn't recall it.
**Root cause:** AgentCore Memory's semantic strategy extracts facts asynchronously. Documented as "eventually consistent" but the actual lag is ~30–60 s in ap-southeast-1.
**Fix:** In tests, wait ~60 s before opening the second chat; in production this is fine because users don't normally start a second chat 5 s later.
**Lesson:** Distinguish "event was saved" (immediate, checkable via `list-events`) from "fact was extracted and searchable" (async). Verify both stages independently when debugging.

### 37. Local SigV4 signing proxy for Gateway MCP (Claude Agent SDK can't sign)
**Failed:** Claude Agent SDK's `McpHttpServerConfig` only accepts static headers, but AgentCore Gateway requires a fresh SigV4 signature per HTTPS request.
**Fix:** In-process localhost SigV4 proxy on `127.0.0.1:9000` started as a daemon thread from the container's lifespan. The SDK talks HTTP to localhost, our proxy signs and forwards HTTPS to the Gateway.
**Lesson:** For SDKs that can't do per-request auth signing, a localhost signing proxy is a clean solution — much simpler than forking the SDK. Bind to 127.0.0.1 only, no auth needed, single process.

### 38. Gateway DNS resolution needs a *separate* VPC endpoint
**Failed:** Container inside VPC couldn't resolve `<gateway-id>.gateway.bedrock-agentcore.ap-southeast-1.amazonaws.com`.
**Root cause:** `com.amazonaws.<region>.bedrock-agentcore` VPC endpoint covers the control-plane only. Gateway MCP calls use the `bedrock-agentcore.gateway` subdomain, which requires a separate VPC Interface Endpoint: `com.amazonaws.<region>.bedrock-agentcore.gateway`.
**Fix:** Create the extra endpoint.
**Lesson:** AWS service subdomains often have their own endpoints. If DNS fails, check whether the specific subdomain has a discrete endpoint before rechecking route tables.

### 39. Multi-frontend architecture: one harness per frontend, one slug per route
**Won:** Adding a Dify-specific harness (`harness_dify`) with distinct memory + skills config was a two-step change: (a) create the harness in the AWS console, (b) add one entry to the proxy's `HARNESSES` dict. All existing routes (`/{slug}/v1/chat/completions`, `/dify/{slug}/v1/chat-messages`) light up automatically because they're parameterised on the slug set.
**Lesson:** Keep the route → backend map in a single dict at the top of the proxy. New frontend = one line of config.

---

## Architecture Decisions We'd Make Differently

### ❌ Starting with API Gateway as the main entry point
API Gateway's 29s timeout is incompatible with LLM agents. Should have started with a Lambda Function URL or direct boto3.

### ❌ Trusting the first Gateway target type name
`http.passthrough` sounded like "call any HTTP endpoint". It actually means "call any HTTPS endpoint with a specific `protocolType`". Should have probed the SDK schema before designing around it.

### ✅ VPC-internal EKS proxy (right call)
Rather than fighting SigV4 auth in every frontend, a dumb proxy pod solves it for all internal clients at once.

### ✅ IRSA over instance roles
Standard EKS pattern. Easier to reason about than instance-level shared credentials.

### ✅ Bridge Lambda for HTTPS-required Gateway → HTTP EKS service
Simpler than adding TLS + domain to an internal NLB. Also gives us a clean place to log/transform tool calls.

### ✅ Model weights baked into TimesFM image
Zero runtime dependency on HuggingFace. Works in the air-gapped VPC. ~2GB image but that's fine for a service pod.

### ✅ Per-value date format detection in ETL
The naive approach (single format per column) silently dropped 60% of the AH data. The correct approach — classify each value by regex, then vectorised parse per group — is 10-50x faster than per-value `.apply` and handles the mixed SAP/EPIC eras.

---

## Speed-Up Checklist for Next Time

```markdown
## AWS AgentCore + EKS + RDS Project Checklist

### Naming
- [ ] AgentCore Runtime name: alphanumeric + underscore only (no hyphens)
- [ ] Gateway Target names must be UNIQUE across all gateways on a harness (tool names become {target}___{tool})
- [ ] ECR repo names: can use hyphens

### Container Architecture
- [ ] AgentCore Runtime container: `linux/arm64`
- [ ] EKS Fargate containers: `linux/amd64`
- [ ] Verify Fargate node arch: `kubectl get nodes -o jsonpath='{.items[*].status.nodeInfo.architecture}'`

### AgentCore Container Interface
- [ ] `GET /ping` — health check
- [ ] `POST /invocations` (NOT `/invoke`)

### Bedrock / Claude Agent SDK
- [ ] Check if model requires inference profile via `boto3.client('bedrock').list_inference_profiles(...)`
- [ ] Set `CLAUDE_CODE_USE_BEDROCK=1` via `ClaudeAgentOptions(env=...)` (NOT container env)
- [ ] Override `AWS_REGION` in subprocess env if profile region differs from container region
- [ ] Use inference profile ARN as model string (not bare ID)

### IAM
- [ ] Runtime role: `bedrock:InvokeModel*`, `secretsmanager:GetSecretValue`, `ecr:*`, `logs:*`, `ec2:CreateNetworkInterface`/`Describe*`/`Delete*` (VPC mode)
- [ ] `InvokeAgentRuntime` policy: use `Resource: "*"` (endpoint ARN != runtime ARN)
- [ ] For every new Gateway added to a harness: add its ARN to `AmazonBedrockAgentCoreHarnessGatewayPolicy_*`
- [ ] Lambda role for VPC mode: wait 15-20s after role creation before `create_function`
- [ ] `AuthType: NONE` on Lambda Function URLs typically blocked by org SCP → use `AWS_IAM`

### VPC / Networking
- [ ] 5 VPC Interface Endpoints: `bedrock-runtime`, `bedrock-agentcore`, `secretsmanager`, `ecr.api`, `ecr.dkr`
- [ ] Tag private subnets: `kubernetes.io/role/internal-elb=1`
- [ ] Never trust "make RDS temporarily public" — private subnet route tables won't cooperate

### EKS
- [ ] Use IRSA for pod AWS credentials (execution role ≠ pod credentials)
- [ ] `imagePullPolicy: Always` when using `:latest` tags
- [ ] Set `failureThreshold` on liveness probes to avoid premature kills on slow starts
- [ ] Wrap blocking boto3 calls in `run_in_threadpool` to keep the FastAPI event loop responsive

### boto3 quirks
- [ ] `invoke_agent_runtime` response body: `response["response"].read()` (not `response["body"]`)
- [ ] `ConnectionClosedError` and `EventStreamError` are DIFFERENT exception classes — catch both
- [ ] `list_repo_files` and other HF calls fail silently in offline mode — check `HF_HUB_OFFLINE` env

### AgentCore Gateway Target types
- [ ] `mcp.lambda` — Lambda backend with inline tool schema; use for anything hosted on Lambda or reachable only via Lambda
- [ ] `http.passthrough` — requires HTTPS endpoint + protocolType from [A2A, CUSTOM, INFERENCE, MCP]
- [ ] For internal EKS services (HTTP): use a bridge Lambda with `mcp.lambda` — do NOT try to add HTTPS to internal NLBs unless you have a good reason

### Session/Memory Wiring
- [ ] Map upstream `chat_id` → `runtimeSessionId` (stable per conversation, ≥33 chars)
- [ ] Map upstream `user_id` → `actorId` (harness) or `runtimeUserId` (runtime)
- [ ] Fresh UUID per request → memory doesn't work AND container cold-starts every time

### Streaming
- [ ] For harness streaming, consume `contentBlockDelta` events from `invoke_harness` response stream
- [ ] Yield SSE chunks directly — don't buffer and fake-stream at the end
- [ ] Retry retry on cold-start disconnect ONLY before first token is yielded

### ETL / Data
- [ ] Never assume single date format per column — sample 20 values spread across the file
- [ ] pandas 3.x: use `format="ISO8601"` when mixing datetime and date-only
- [ ] European DD.MM.YYYY: `pd.to_datetime(..., dayfirst=True)`
- [ ] Filter parsed years to `[1678, 2262]` before `datetime64[ns]` cast to avoid `OutOfBoundsDatetime`
- [ ] For private RDS: ECS Fargate task inside the VPC, `user: root` for apk installs

### TimesFM / PyTorch
- [ ] Install CPU-only PyTorch first: `pip install torch --index-url https://download.pytorch.org/whl/cpu`
- [ ] Then `pip install timesfm` (no `[torch]` extra — that pulls CUDA)
- [ ] For TimesFM 2.x: `TimesFM_2p5_200M_torch.from_pretrained(...)` + `.compile(ForecastConfig(...))`
- [ ] `per_core_batch_size=1` for single-input on-demand inference
- [ ] Bake model weights into Docker image to avoid runtime HF dependency
```

---

## Time Spent Per Problem Area

| Area | Approx time | Notes |
|---|---|---|
| Container interface (`/ping`, `/invocations`) | 45 min | Trial and error via CloudWatch logs |
| Bedrock auth (`CLAUDE_CODE_USE_BEDROCK` for subprocess) | 30 min | Documented but not obvious it applies to subprocess |
| Inference profile requirement | 20 min | Clear error, fast fix |
| `response["response"]` key | 15 min | One CloudWatch log check |
| IAM `Resource: "*"` for InvokeAgentRuntime | 40 min | IAM simulator was misleading |
| Lambda Function URL SCP block | 25 min | Switch to AWS_IAM |
| EKS IRSA setup | 30 min | Standard but multi-step |
| Internal NLB subnet tags | 20 min | Clear error message |
| Image arch mismatch (amd64 vs arm64) | 15 min | Fast once identified |
| RDS ECS-based ingestion | 60 min | Multiple iterations |
| Streaming (contentBlockDelta pipe-through) | 30 min | Rewrote as generator |
| Cold-start retry catching wrong exception | 20 min | Deep botocore inspection |
| Session/memory wiring | 25 min | Discovering `chat_id` and `user_id` fields |
| Second Gateway tool-name collision | 15 min | Fast once error was read carefully |
| ETL mixed date formats | 90 min | Longest single debugging session — required per-value classification and out-of-range guard |
| TimesFM API v1 vs v2 differences | 45 min | Multiple wrong assumptions about class names |
| TimesFM CUDA vs CPU install | 20 min | Docker image ballooned to 5GB before fixing |
| Gateway http.passthrough constraints | 30 min | Required rethinking to Lambda bridge |
| MCP `inputSchema` doesn't accept `default` | 10 min | Fast fix once error surfaced |

**Total recoverable time if checklist used upfront: ~5-6 hours saved on a similar project.**

---

## The Meta-Lesson

Every one of these failures was a mismatch between an assumption and the actual API/system behaviour. The debugging pattern that worked every time:

1. **Read the exact error message.** Don't paraphrase it.
2. **Look at the wire.** boto3 service models, CloudWatch logs, `kubectl logs`, network captures.
3. **Test the smallest possible reproducer.** One `pd.to_datetime` call, one Lambda invoke, one `curl` port-forward.
4. **Assume the docs are approximate and the API is authoritative.** When in doubt, `dir(client)`, `inspect.signature(method)`, `service_model.operation_model(...)`.

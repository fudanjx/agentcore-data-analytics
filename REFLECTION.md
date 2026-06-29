# Build Reflection — AgentCore POC

This document captures what worked, what failed, root causes, and how to do it faster next time.

---

## What We Built

A production-grade data analytics agent accessible from DIFY and Open WebUI via OpenAI-compatible API, running 100% within AWS private networking. No internet traffic. Claude (Bedrock) + Claude Agent SDK + AgentCore Runtime + EKS proxy.

**Total wall-clock time:** ~1 full day of iteration.

---

## What Worked First Time

- **Claude Agent SDK MCP tool pattern** — `@tool` decorator + `create_sdk_mcp_server` worked exactly as documented
- **FastAPI + Mangum pattern** — dropped Mangum once we moved to AgentCore (no longer Lambda)
- **psycopg2 schema introspection** — `information_schema.columns` query clean and reliable
- **boto3 `bedrock-agentcore-control` API** — `create_agent_runtime` straightforward once we understood naming rules
- **VPC peering between default VPC and bot-nuhs-vpc** — already existed, worked immediately
- **EKS wildcard Fargate profile** — covered new `agentcore` namespace with zero config

---

## What Failed and Why

### 1. AgentCore container health check path
**Failed:** Container had `POST /invoke` → AgentCore called `GET /ping` and `POST /invocations`  
**Fix:** Add `/ping` GET endpoint; rename `/invoke` to `/invocations`  
**Lesson:** AgentCore uses the Lambda container interface convention: health = `/ping`, invocations = `/invocations`

### 2. AgentCore runtime name hyphens rejected
**Failed:** `agentcore-poc` → `ValidationException: Member must satisfy regular expression pattern: [a-zA-Z][a-zA-Z0-9_]{0,47}`  
**Fix:** `agentcore_poc`  
**Lesson:** AgentCore Runtime names allow only alphanumeric + underscore. No hyphens.

### 3. `response["body"]` vs `response["response"]`
**Failed:** Wrapper Lambda crashed with `KeyError: 'body'`  
**Root cause:** `invoke_agent_runtime` returns the streaming body under `response["response"]`, not `response["body"]` (which is what Lambda/API GW uses)  
**Fix:** `raw = response["response"].read()`  
**Lesson:** Always print `list(response.keys())` when a new boto3 client returns unexpected structure.

### 4. `ANTHROPIC_BASE_URL` doesn't work for the Claude CLI subprocess
**Failed:** Container returned `Not logged in · Please run /login`  
**Root cause:** `ANTHROPIC_BASE_URL` works for the Python Anthropic SDK client, not for the `claude` CLI subprocess spawned by `claude-agent-sdk`. The subprocess has its own auth mechanism.  
**Fix:** Set `CLAUDE_CODE_USE_BEDROCK=1` and pass via `ClaudeAgentOptions(env={...})`  
**Lesson:** `claude-agent-sdk` spawns a subprocess — env vars for the subprocess must be explicitly passed via `ClaudeAgentOptions(env=...)`. The container's environment is not automatically inherited.

### 5. On-demand Bedrock model ID rejected
**Failed:** `400 Invocation of model ID anthropic.claude-sonnet-4-6 with on-demand throughput isn't supported`  
**Fix:** Use an application inference profile ARN as the model ID  
**Lesson:** In regions/accounts where Claude models require inference profiles, you must use the profile ARN (e.g. `arn:aws:bedrock:us-east-1:<account>:application-inference-profile/<id>`) or a cross-region profile prefix (`global.anthropic.*`, `apac.anthropic.*`). The bare model ID only works with on-demand throughput enabled.

### 6. Inference profile region mismatch
**Failed:** Inference profile `ji5jakx5lho3` is in `us-east-1` but container env had `AWS_DEFAULT_REGION=ap-southeast-1`  
**Fix:** Override region in subprocess env: `env={"AWS_REGION": "us-east-1", "AWS_DEFAULT_REGION": "us-east-1"}`  
**Lesson:** The `claude` subprocess picks up the region from its own env. When the inference profile is in a different region from the container, override it explicitly in `ClaudeAgentOptions(env=...)`.

### 7. IAM policy scope too narrow for InvokeAgentRuntime
**Failed:** `AccessDeniedException: no identity-based policy allows bedrock-agentcore:InvokeAgentRuntime on resource: .../runtime-endpoint/DEFAULT`  
**Root cause:** We scoped the policy to the runtime ARN (`arn:.../runtime/agentcore_poc-iumXW8638m`) but AgentCore's IAM check uses the endpoint ARN (`arn:.../runtime/.../runtime-endpoint/DEFAULT`)  
**Fix:** `Resource: "*"` in the policy  
**Lesson:** When `invoke_agent_runtime` is called with a runtime ARN, AWS internally resolves it to an endpoint. The IAM check happens against the endpoint ARN. Scope broadly with `*` unless you know the exact endpoint ARN pattern.

### 8. Lambda Function URL blocked by org SCP
**Failed:** `403 Forbidden` on Lambda Function URL with `AuthType: NONE`  
**Root cause:** Org SCP (`Service Control Policy`) blocks unauthenticated Lambda URL invocations  
**Fix:** Switch to `AuthType: AWS_IAM`; use SigV4 signing or the EKS proxy  
**Lesson:** Check org SCPs early. `AuthType: NONE` works in personal accounts but often blocked in org accounts. Plan for SigV4 or a proxy from the start.

### 9. EKS Fargate pod has no AWS credentials from execution role
**Failed:** `Unable to locate credentials` in EKS pod  
**Root cause:** `AmazonEKSFargatePodExecutionRole` is used by ECS/Fargate to pull images and write logs — it does NOT automatically provide credentials to the running container  
**Fix:** IRSA (IAM Roles for Service Accounts): create IAM role with OIDC trust → annotate Kubernetes ServiceAccount → use ServiceAccount in pod  
**Lesson:** Fargate pod credentials = IRSA. The execution role and the pod's credentials are completely separate. Always set up IRSA for pods that need AWS API access.

### 10. IRSA resource scope too narrow
**Failed:** Even with IRSA, `AccessDeniedException` persisted  
**Root cause:** Same as #7 — policy was scoped to runtime ARN, but IAM check uses endpoint ARN  
**Fix:** `Resource: "*"` on the IRSA role policy too  

### 11. Internal NLB stuck in pending
**Failed:** `LoadBalancer` service stayed `<pending>` indefinitely  
**Root cause:** Private subnets missing `kubernetes.io/role/internal-elb=1` tag required by AWS Load Balancer Controller  
**Fix:** Tag both subnets:
```bash
aws ec2 create-tags --resources subnet-... --tags Key=kubernetes.io/role/internal-elb,Value=1
```
**Lesson:** Always pre-tag subnets when setting up EKS. Add `kubernetes.io/role/internal-elb=1` to private subnets and `kubernetes.io/role/elb=1` to public subnets at VPC setup time.

### 12. EKS proxy image architecture mismatch
**Failed:** `ImagePullBackOff: no match for platform in manifest`  
**Root cause:** Mac M-series builds multi-platform manifests by default; Fargate can't parse them  
**Fix:** `docker build --platform linux/amd64` (Fargate here is amd64, not arm64)  
**Lesson:** Always explicitly set `--platform`. Never rely on the default. Verify node arch with `kubectl get nodes -o jsonpath='{.items[*].status.nodeInfo.architecture}'` before building.

### 13. RDS not reachable from dev Mac
**Failed:** `connection ... timeout expired` from local `pg_restore`  
**Root cause:** RDS is in private subnets with NAT Gateway — `PubliclyAccessible: true` alone doesn't work without an IGW route  
**Fix:** Run data ingestion via ECS Fargate task inside the VPC  
**Lesson:** For data operations targeting private RDS: use ECS Fargate task (or Lambda) inside the VPC. Never try to make the RDS temporarily public — the route table won't support it if subnets only have NAT.

### 14. ECS task couldn't run `pg_restore` — no binary
**Failed:** `FileNotFoundError: pg_restore`  
**Root cause:** `agentcore-poc` Alpine container has Python but not postgresql-client  
**Fix:** `apk add --no-cache postgresql-client` at task start; run as `root` (container default user is non-root)  
**Lesson:** When running ad-hoc tasks with an existing image, install runtime tools via the container's package manager. Remember to override `user: root` in the ECS task definition for tool installs.

### 15. Lambda timeout at 120s
**Failed:** Long agent queries timed out  
**Fix:** Increase Lambda timeout to 300s; add Lambda Function URL (no API GW 29s limit)  
**Lesson:** AgentCore agents can take 30-90s. API Gateway has a hard 29s max — always add a Lambda Function URL as alternative for non-trivial agents.

---

## Architecture Decisions We'd Make Differently

### ❌ Starting with API Gateway as the main entry point
API Gateway's 29s timeout is incompatible with LLM agents. Should have started with a Lambda Function URL or the direct boto3 pattern.

### ✅ VPC-internal EKS proxy (right call)
Rather than fighting SigV4 auth in every frontend, a dumb proxy pod solves it cleanly for all internal clients at once. Works for DIFY, Open WebUI, and any future internal service.

### ✅ IRSA over instance roles
More secure, easier to reason about than trying to share instance-level credentials.

---

## Speed-Up Checklist for Next Time

Copy this checklist at project start:

```markdown
## AWS AgentCore New Project Checklist

### Naming
- [ ] AgentCore Runtime name: alphanumeric + underscore only (no hyphens)
- [ ] ECR repo names: can use hyphens

### Container
- [ ] Build `linux/arm64` for AgentCore Runtime (confirmed requirement)
- [ ] Add `GET /ping` health check endpoint
- [ ] Entry point: `POST /invocations` (not `/invoke`)

### Bedrock / Claude Agent SDK
- [ ] Verify model requires inference profile (check boto3 list-inference-profiles)
- [ ] Pass `CLAUDE_CODE_USE_BEDROCK=1` via `ClaudeAgentOptions(env=...)`
- [ ] Pass AWS_REGION override in subprocess env if inference profile region ≠ container region
- [ ] Use inference profile ARN as model string

### IAM
- [ ] Runtime role needs: bedrock:InvokeModel/InvokeModelWithResponseStream, secretsmanager:GetSecretValue, ecr:*, logs:*, ec2:CreateNetworkInterface/Describe*/Delete* (for VPC mode)
- [ ] InvokeAgentRuntime policy: use `Resource: "*"` (endpoint ARN != runtime ARN)
- [ ] Lambda Function URL: expect `AuthType: AWS_IAM` in org accounts (SCP)

### VPC / Networking
- [ ] All 5 VPC Interface Endpoints: bedrock-runtime, bedrock-agentcore, secretsmanager, ecr.api, ecr.dkr
- [ ] AgentCore runtime role needs EC2 VPC permissions for VPC mode
- [ ] Tag private subnets: `kubernetes.io/role/internal-elb=1` before creating internal LBs

### EKS
- [ ] Check Fargate node arch first: `kubectl get nodes -o jsonpath='{.items[*].status.nodeInfo.architecture}'`
- [ ] Build proxy image for correct arch (often amd64 even if Fargate)
- [ ] Always use IRSA for pod AWS credentials — execution role ≠ container credentials
- [ ] boto3 `invoke_agent_runtime` response body: `response["response"].read()` (not `response["body"]`)

### Data Ingestion
- [ ] Private RDS = ECS task for ingestion (not local pg_restore)
- [ ] ECS task: `user: root` for apk installs; agentcore-restore-task-role with S3 + secretsmanager

### Lambda
- [ ] Set timeout ≥ 300s for LLM-backed handlers
- [ ] Add Lambda Function URL alongside API Gateway for long-running queries
```

---

## Time Spent Per Problem Area

| Area | Approx time | Notes |
|---|---|---|
| Container interface discovery (`/ping`, `/invocations`) | 45 min | Trial and error via CloudWatch logs |
| Bedrock auth (`CLAUDE_CODE_USE_BEDROCK`) | 30 min | Documented but not obvious it applies to subprocess |
| Inference profile requirement | 20 min | Clear error message, fast fix |
| `response["response"]` key | 15 min | One CloudWatch log check |
| IAM `Resource: "*"` for InvokeAgentRuntime | 40 min | Misleading — simulate said allowed, live said denied |
| Lambda Function URL SCP block | 25 min | Needed to switch to AWS_IAM auth |
| EKS IRSA setup | 30 min | Standard pattern but multiple steps |
| Internal NLB subnet tags | 20 min | Error message was clear |
| EKS image architecture mismatch | 15 min | Clear error, quick fix |
| RDS data ingestion (ECS task approach) | 60 min | Multiple iterations (pg_restore binary, root user, IAM role trust) |

**Total recoverable time if checklist used upfront: ~3 hours saved**

# AgentCore POC — Product Requirements Document

## Goal

Expose an OpenAI-compatible `/v1/chat/completions` API backed by a data analytics agent hosted on **AWS AgentCore Runtime**. The agent auto-discovers the connected PostgreSQL database schema on every request, uses Claude (via Amazon Bedrock) to generate and execute SQL queries, and returns natural language answers.

Frontend clients (Open WebUI, DIFY, or any OpenAI SDK) connect via a VPC-internal proxy deployed on EKS.

---

## Architecture

```
External Client (Open WebUI / DIFY / SDK)
        │
        │  HTTP POST /v1/chat/completions  (no auth — VPC-internal only)
        ▼
EKS Fargate Pod: agentcore-proxy  (namespace: agentcore)
        │  boto3.invoke_agent_runtime()   ← IAM via IRSA
        │  via bedrock-agentcore VPC endpoint
        ▼
AWS AgentCore Runtime  (ap-southeast-1, bot-nuhs-vpc)
        │  POST /invocations  (managed by AgentCore)
        ▼
Agent Container (ECR: agentcore-poc, arm64)
        │  claude-agent-sdk query() with execute_sql MCP tool
        │  CLAUDE_CODE_USE_BEDROCK=1
        │  model: application inference profile (us-east-1)
        ▼
Amazon Bedrock  (us-east-1, cross-region inference profile)
        │  global.anthropic.claude-sonnet-4-6
        ▼  (via application inference profile ji5jakx5lho3)
RDS PostgreSQL  (ap-southeast-1, bot-nuhs-vpc)
        creds via Secrets Manager (ap-southeast-1)
```

### External Access (from outside EKS)

```
Python script / CLI
        │  boto3.invoke_agent_runtime()  ← direct, IAM-signed
        ▼
AgentCore Runtime  (bypasses proxy entirely)

Lambda Function URL (AWS_IAM auth)
        │  SigV4-signed HTTP  ← for OpenAI SDK compatibility
        ▼
OpenAI Wrapper Lambda (us-east-1)
        │  invoke_agent_runtime()
        ▼
AgentCore Runtime
```

---

## Key Design Decisions

| Decision | Choice | Reason |
|---|---|---|
| Agent harness | `claude-agent-sdk` subprocess | Official SDK; MCP tool support |
| LLM auth | `CLAUDE_CODE_USE_BEDROCK=1` + inference profile ARN | IAM only, no API key |
| Bedrock model | Application inference profile `ji5jakx5lho3` (us-east-1) | On-demand throughput requires an inference profile |
| AgentCore region | `ap-southeast-1` | Same region as RDS |
| Bedrock region | `us-east-1` | Inference profile lives here; subprocess env overrides region |
| AgentCore network | VPC mode, private subnets | All traffic stays within AWS backbone |
| VPC endpoints | bedrock-runtime, bedrock-agentcore, secretsmanager, ecr.api, ecr.dkr, s3 | Internet-free operation |
| Container platform | `linux/arm64` | AgentCore Runtime supported platform |
| Frontend connectivity | EKS Fargate proxy + IRSA | Avoids SigV4 complexity in frontend; org SCP blocks anonymous Lambda URLs |
| EKS credentials | IRSA (IAM Roles for Service Accounts) | Standard EKS pattern; Fargate pod execution role does not provide container credentials |
| Internal NLB | `kubernetes.io/role/internal-elb` subnet tag required | AWS LB controller needs this tag on subnets |

---

## AgentCore Container Interface

AgentCore calls the container on:
- `GET /ping` — health check (must return 200)
- `POST /invocations` — agent invocation

Request body sent by AgentCore:
```json
{"messages": [{"role": "user", "content": "..."}]}
```

Container response:
```json
{"result": "<final answer text>"}
```

> **Note:** Early documentation suggested `POST /invoke` but AgentCore actually uses `POST /invocations`. The `/ping` health check is required or the container is never marked READY.

---

## Bedrock / Claude SDK Integration

The `claude-agent-sdk` spawns a `claude` CLI subprocess. To use Bedrock instead of the Anthropic API:

```python
ClaudeAgentOptions(
    model="arn:aws:bedrock:us-east-1:<account>:application-inference-profile/<profile-id>",
    env={
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "AWS_REGION": "us-east-1",
        "AWS_DEFAULT_REGION": "us-east-1",
    },
)
```

- `CLAUDE_CODE_USE_BEDROCK=1` switches the subprocess from Anthropic API to Bedrock
- The region override is required because the inference profile is in `us-east-1`, even though the container runs in `ap-southeast-1`
- On-demand model IDs (e.g. `anthropic.claude-sonnet-4-6`) are **not supported** — must use an inference profile ARN or cross-region prefix (`global.anthropic.*`)

---

## AWS Resources

### AgentCore Runtime
- **Name:** `agentcore_poc` (underscores required — hyphens rejected by API)
- **Runtime ID:** `agentcore_poc-iumXW8638m`
- **Network:** VPC mode, `vpc-0c6ce733b2e6ed419` (bot-nuhs-vpc)
- **Subnets:** `subnet-061205c705e0f41d4`, `subnet-0466b6e1fbb8a49f3` (private)
- **Security group:** `sg-07258677b7e691e48` (agentcore-poc-runtime-sg)
- **IAM role:** `agentcore-poc-runtime-role`
- **Image:** `964340114883.dkr.ecr.ap-southeast-1.amazonaws.com/agentcore-poc:latest`

### EKS Proxy
- **Namespace:** `agentcore`
- **Deployment:** `agentcore-proxy`
- **ServiceAccount:** `agentcore-proxy` (annotated with IRSA role)
- **IRSA role:** `agentcore-proxy-irsa`
- **ClusterIP:** `http://agentcore-proxy.agentcore.svc.cluster.local`
- **Internal NLB:** `k8s-agentcor-agentcor-a9dbd8956e-c923dee5a7cceccb.elb.ap-southeast-1.amazonaws.com`
- **Image:** `964340114883.dkr.ecr.ap-southeast-1.amazonaws.com/agentcore-proxy:latest` (amd64)
- **Fargate profile:** covered by existing `wildcard-match` profile (`namespace: *`)

### OpenAI Wrapper Lambda (external access fallback)
- **Name:** `agentcore-poc-openai-wrapper` (us-east-1)
- **Function URL:** `https://hx66okhncgcepkwmpk2ljzu5qe0wbtzz.lambda-url.us-east-1.on.aws/`
- **Auth:** `AWS_IAM` (org SCP blocks `NONE`)
- **Timeout:** 300s

### RDS PostgreSQL
- **Identifier:** `jinxin-postgres`
- **Endpoint:** `jinxin-postgres.cf7in3efovlt.ap-southeast-1.rds.amazonaws.com`
- **DB:** `nuhsConversation`
- **Secret ARN:** `arn:aws:secretsmanager:ap-southeast-1:964340114883:secret:agentcore-rds-credentials-tlv56J`
- **Security group:** `sg-047ce45a9f53a6f7a` — allows port 5432 from `sg-07258677b7e691e48`
- **Network:** private subnets in bot-nuhs-vpc (not internet-accessible from dev Mac — requires tunnel or ECS task for data ingestion)

---

## VPC Endpoints (bot-nuhs-vpc, ap-southeast-1)

| Endpoint ID | Service |
|---|---|
| `vpce-0b582d02606dfbe00` | `bedrock-runtime` |
| `vpce-0d7da6165d12a2ae8` | `bedrock-agentcore` |
| `vpce-059f7b6613b722983` | `secretsmanager` |
| `vpce-02600a734df24aff5` | `ecr.api` |
| `vpce-084fe8036d1b6e33b` | `ecr.dkr` |
| `vpce-0cb3dca98becb59a1` | S3 Gateway (existing) |

All use security group `sg-0be4a7ae0ed2caf17` (vpc-endpoints-sg), allow 443 inbound from `10.0.0.0/16`.

---

## Environment Variables

| Variable | Set in | Value / Description |
|---|---|---|
| `AWS_DEFAULT_REGION` | AgentCore Runtime env | `ap-southeast-1` |
| `CLAUDE_CODE_USE_BEDROCK` | AgentCore Runtime env | `1` |
| `RDS_SECRET_ARN` | AgentCore Runtime env | Secrets Manager ARN |
| `RDS_DB_NAME` | AgentCore Runtime env | `nuhsConversation` |
| `AWS_REGION` / `AWS_DEFAULT_REGION` | `agent.py` subprocess env | `us-east-1` (inference profile region) |

---

## Frontend Connection Config

### DIFY (EKS, same cluster)
- **Base URL:** `http://agentcore-proxy.agentcore.svc.cluster.local`
- **API Key:** any value
- **Model:** `agentcore`

### Open WebUI (EC2, default VPC via VPC peering)
- **Base URL:** `http://k8s-agentcor-agentcor-a9dbd8956e-c923dee5a7cceccb.elb.ap-southeast-1.amazonaws.com`
- **API Key:** any value

### Python SDK / external
```python
import boto3, json
client = boto3.client("bedrock-agentcore", region_name="ap-southeast-1")
resp = client.invoke_agent_runtime(
    agentRuntimeArn="arn:aws:bedrock-agentcore:ap-southeast-1:964340114883:runtime/agentcore_poc-iumXW8638m",
    contentType="application/json",
    accept="application/json",
    payload=json.dumps({"messages": [{"role": "user", "content": "..."}]}).encode(),
)
print(json.loads(resp["response"].read())["result"])
```

---

## Data Ingestion

RDS is in private subnets — not reachable from a developer Mac. To load data:

```bash
# Upload dump to S3, then run one-shot ECS Fargate task
aws s3 cp data/dump.dump s3://agentcore-tmp-<account>/dump.dump
# ECS task uses agentcore-poc image + root user + postgresql-client via apk
# See: infra restore task definitions (agentcore-pg-restore)
```

Sample data loaded: `employees` schema in `nuhsConversation` DB (tables: department, department_employee, department_manager, employee, salary, title).

> **Note:** The schema query in `db.py` only checks `information_schema.columns WHERE table_schema = 'public'`. If data is loaded into a non-public schema (e.g. `employees`), set `search_path` or update the schema query.

---

## Open Items

- [ ] Update `db.py` schema query to include non-public schemas, or set `search_path=employees,public` in connection
- [ ] Add `GET /v1/models` to `app/main.py` for AgentCore playground compatibility
- [ ] Add streaming support to proxy for long-running queries
- [ ] Set up CloudWatch alarms on AgentCore Runtime error rate

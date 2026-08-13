# Deploy the Strands Agent to Amazon Bedrock AgentCore Runtime

This guide explains how to deploy the ZIP bundle in this directory as your own Amazon Bedrock AgentCore Runtime. It covers S3 source deployment, environment variables, optional Gateway and Code Interpreter integrations, Agent Skills, IAM roles, and verification.

The examples use these placeholders:

| Placeholder | Example |
| --- | --- |
| `ACCOUNT_ID` | `123456789012` |
| `REGION` | `ap-southeast-1` |
| `MODEL_REGION` | `us-east-1` |
| `RUNTIME_NAME` | `my_strands_agent` |
| `CODE_BUCKET` | `my-agentcore-code-123456789012` |
| `CONFIG_BUCKET` | `my-agent-config-123456789012` |

Replace every placeholder and sample ARN before using a command or policy.

## What you will create

You will create or select:

1. An S3 bucket and ZIP object containing the Runtime source bundle.
2. A Bedrock model or application inference profile.
3. An AgentCore Runtime execution role.
4. Optional AgentCore Gateways that expose MCP tools.
5. An optional custom AgentCore Code Interpreter and its separate execution role.
6. An optional AgentCore Memory resource.
7. An S3 prefix containing Agent Skills.
8. An AgentCore Runtime and endpoint.

The Runtime creates one Strands `Agent` for each invocation. Dify can provide the application system prompt in an OpenAI-style system message. Gateway and Code Interpreter tools are added only when their corresponding environment configuration is present.

## Prerequisites

- Access to the AWS account and selected Region.
- Permission to create or update AgentCore Runtime resources and pass the Runtime execution role.
- Permission to upload the deployment ZIP and skills to S3.
- Access to the configured Bedrock model or inference profile.
- Docker Desktop for building the Linux ARM64 dependency bundle on Windows.
- AWS CLI credentials if you use the command examples.

For production, use least-privilege policies rather than broad managed policies. The identity performing the deployment is different from the Runtime execution role. The deployment identity needs AgentCore control-plane permissions and `iam:PassRole`; the Runtime execution role is assumed by AgentCore while the agent runs.

## 1. Build the deployment ZIP

The supplied build script installs the pinned dependencies for Linux ARM64/Python 3.13, verifies imports, and creates the required ZIP structure.

From PowerShell:

```powershell
Set-Location .\Strands-runtime

powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\build_agentcore_bundle.ps1 `
  -OutputPath .\dist\strands_agent_v0.0.5.zip `
  -Force
```

The ZIP must contain:

```text
strands_agent/
  main.py
  agent.py
  code_interpreter.py
  gateway_config.py
  gateway_proxy.py
  memory.py
  skills_sync.py
  system_prompt.py
  requirements.txt
  ...vendored dependencies...
```

Important settings for this bundle:

- Runtime: `PYTHON_3_13`
- Entry point: `strands_agent/main.py`
- Architecture: Linux ARM64
- Do not configure an `opentelemetry-instrument` entry-point prefix unless that executable has been added to the bundle.

AWS currently limits direct-code deployment packages to 250 MB compressed and 750 MB uncompressed. The build script creates its staging tree outside the packaged `strands_agent/` directory.

## 2. Upload the ZIP to S3

Create or select a versioned S3 bucket in the same AWS account. Upload each release under a unique key so that rollback remains possible.

```powershell
$env:AWS_DEFAULT_REGION = "ap-southeast-1"
$codeBucket = "my-agentcore-code-123456789012"
$bundle = ".\dist\strands_agent_v0.0.5.zip"
$objectKey = "my-strands-agent/releases/v0.0.5/strands_agent.zip"

aws s3 cp $bundle "s3://$codeBucket/$objectKey"
aws s3api head-object --bucket $codeBucket --key $objectKey
```

Record the complete location:

```text
s3://my-agentcore-code-123456789012/my-strands-agent/releases/v0.0.5/strands_agent.zip
```

If the bucket uses a customer-managed KMS key, add `kms:Decrypt` for that key to the role that AgentCore uses to read the archive.

## 3. Decide which optional capabilities to enable

| Capability | Enable it with | Disable it with |
| --- | --- | --- |
| Application base prompt | `BASE_SYSTEM_PROMPT=s3://bucket/key.txt` | Omit it or use an empty value |
| AgentCore Gateway MCP tools | Valid `AGENTCORE_GATEWAYS_JSON` | Omit it, use an empty value, or use `{}` |
| Code Interpreter | `CODE_INTERPRETER_ID=<custom-interpreter-id>` | Omit it or use an empty value |
| AgentCore Memory | `MEMORY_ID=<memory-id>` | Set `MEMORY_ID` to an empty value |
| Agent Skills | Set `SKILLS_BUCKET`; `SKILLS_PREFIX` is optional | Omit `SKILLS_BUCKET` or use an empty value |

Do not set `MODEL_ID` to an empty value. Set a valid model identifier or omit it only if you intentionally want the project-specific fallback. For a portable deployment, always configure your own `MODEL_ID` or `MODEL_ARN`. Configure your own skills bucket and prefix only when the deployment needs skills.

### Dify-oriented configuration

For maximum prompt flexibility, leave `BASE_SYSTEM_PROMPT` empty and place the application prompt in Dify's system message. The Runtime appends caller-provided system messages to its internal skill and memory safety guidance.

The Dify system prompt remains part of the Bedrock cacheable prompt prefix. Cache reuse requires identical preceding content, the model's minimum cacheable token count, and another request within the configured TTL.

## 4. Configure every Runtime environment variable

Environment variable values in the AgentCore console are strings. The following table lists every application setting read by this Runtime.

### Region, model, caching, and usage

| Variable | Runtime default | Recommended configuration |
| --- | --- | --- |
| `AWS_DEFAULT_REGION` | `ap-southeast-1` in fallback paths | Set to the Runtime/AgentCore resource Region |
| `AWS_REGION` | Usually supplied by AWS | Normally leave Runtime-managed; it is used as the first S3 prompt-client Region fallback |
| `MODEL_ID` | Project-specific application inference profile ARN | Set your Bedrock model ID or application inference profile ARN |
| `MODEL_ARN` | Used only when `MODEL_ID` is absent | Alternative to `MODEL_ID`; do not set both |
| `MODEL_REGION` | Parsed from an ARN, otherwise `AWS_DEFAULT_REGION` | Set explicitly when the model is in a different Region |
| `PROMPT_CACHE_TTL` | `5m` | `5m` or `1h`; the model must support the selected TTL |
| `ENABLE_MODEL_USAGE_LOGS` | `true` | Use `true` to emit one content-free `MODEL_USAGE` record per invocation |
| `MODEL_PRICING_LABEL` | Project pricing label | Set an auditable label for your chosen model and pricing basis |
| `MODEL_INPUT_PRICE_PER_MTOK_USD` | `3.00` | Current uncached-input rate per million tokens |
| `MODEL_OUTPUT_PRICE_PER_MTOK_USD` | `15.00` | Current output rate per million tokens |
| `MODEL_CACHE_READ_PRICE_PER_MTOK_USD` | `0.30` | Current cache-read rate per million tokens |
| `MODEL_CACHE_WRITE_5M_PRICE_PER_MTOK_USD` | `3.75` | Current five-minute cache-write rate per million tokens |
| `MODEL_CACHE_WRITE_1H_PRICE_PER_MTOK_USD` | `6.00` | Current one-hour cache-write rate per million tokens |

Pricing values affect estimated logs only; they do not affect AWS billing.

### Base prompt and Gateway tools

| Variable | Runtime default | Recommended configuration |
| --- | --- | --- |
| `BASE_SYSTEM_PROMPT` | Empty | Empty for Dify-owned prompts, or an `s3://bucket/key.txt` URI containing a non-empty UTF-8 prompt |
| `BASE_SYSTEM_PROMPT_MAX_BYTES` | `200000` | Positive maximum prompt object size |
| `AGENTCORE_GATEWAYS_JSON` | Empty | `{}` for no gateways, or the validated mapping shown below |
| `ENABLE_GATEWAYS` | `true` | Keep `true`; set `false` to override and suppress configured gateways |

Gateway JSON example:

```json
{
  "analytics": {
    "label": "Analytics DB",
    "url": "https://analytics-gateway-id.gateway.bedrock-agentcore.ap-southeast-1.amazonaws.com",
    "arn": "arn:aws:bedrock-agentcore:ap-southeast-1:123456789012:gateway/analytics-gateway-id"
  }
}
```

Enter the value as one line in the console:

```text
{"analytics":{"label":"Analytics DB","url":"https://analytics-gateway-id.gateway.bedrock-agentcore.ap-southeast-1.amazonaws.com","arn":"arn:aws:bedrock-agentcore:ap-southeast-1:123456789012:gateway/analytics-gateway-id"}}
```

Requirements:

- Each top-level key is a short lowercase tool prefix such as `analytics`.
- The URL must be HTTPS with no extra path and must match the Gateway ID and Region in the ARN.
- The Runtime role must have `bedrock-agentcore:InvokeGateway` on every listed Gateway ARN.
- The Gateway must use IAM inbound authorization for this Runtime's SigV4 requests.

### Code Interpreter

| Variable | Runtime default | Recommended configuration |
| --- | --- | --- |
| `CODE_INTERPRETER_ID` | Empty | Empty to disable, or your custom Code Interpreter ID |
| `CODE_INTERPRETER_REGION` | `AWS_DEFAULT_REGION`, then `ap-southeast-1` | Region containing the Code Interpreter |
| `ENABLE_CODE_INTERPRETER` | `true` | Keep `true`; set `false` to override and suppress a configured interpreter |
| `CODE_INTERPRETER_SESSION_TIMEOUT_SECONDS` | `1800` | `60` to `28800`; values are clamped to this range |
| `CODE_INTERPRETER_MAX_RESULT_CHARS` | `200000` | Maximum tool-result characters retained in model context, minimum `1000` |

Use a custom Code Interpreter when skill resources or user files must be copied from S3. Its execution role is separate from the Runtime execution role; see the IAM examples below.

### AgentCore Memory

| Variable | Runtime default | Recommended configuration |
| --- | --- | --- |
| `MEMORY_ID` | Project-specific Memory ID | Set your Memory ID, or explicitly set an empty value to disable memory |
| `MEMORY_REGION` | `ap-southeast-1` | Region containing the Memory resource |
| `MEMORY_BATCH_SIZE` | `10` | Messages per persistence batch, clamped to `1` through `100` |
| `MEMORY_TOP_K` | `5` | Records retrieved per active strategy, clamped to `1` through `1000` |
| `MEMORY_RELEVANCE_SCORE` | `0.2` | Minimum relevance score, clamped to `0` through `1` |

Memory is used only when the invocation includes both an actor ID and session ID. When Memory is enabled, it becomes the source of truth for prior conversation turns.

### Agent Skills

| Variable | Runtime default | Recommended configuration |
| --- | --- | --- |
| `SKILLS_BUCKET` | Empty | Set a bucket owned by your deployment when enabling skills |
| `SKILLS_PREFIX` | Empty | Optional prefix containing one directory per skill; empty means the bucket root |
| `SKILLS_LOCAL_DIR` | `/tmp/strands-agent-skills` | Keep this writable `/tmp` path |
| `SKILLS_MAX_OBJECT_BYTES` | `50000000` | Maximum downloaded size of one skill object, minimum `1000` |
| `SKILLS_MAX_SYNC_BYTES` | `250000000` | Maximum combined startup download; never lower than the per-object limit |
| `SKILLS_MAX_RESOURCE_CHARS` | `100000` | Maximum UTF-8 characters returned by `read_skill_resource`, minimum `1000` |

Skills are enabled when `SKILLS_BUCKET` is non-empty. `SKILLS_PREFIX` is optional; an empty or unset prefix means skill directories are stored at the bucket root. If the bucket is empty or unset, the Runtime performs no S3 skill sync and omits the skill prompt guidance, `AgentSkills` plugin, `read_skill_resource`, and `stage_skill_resource` tools.

### Complete Dify example

This minimal example enables usage logging, lets Dify own the system prompt, and disables Gateway, Code Interpreter, Memory, and skills:

```text
AWS_DEFAULT_REGION=ap-southeast-1
MODEL_ID=arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/your-profile-id
MODEL_REGION=us-east-1
PROMPT_CACHE_TTL=5m
ENABLE_MODEL_USAGE_LOGS=true
MODEL_PRICING_LABEL=claude-sonnet-standard-2026-08
MODEL_INPUT_PRICE_PER_MTOK_USD=3.00
MODEL_OUTPUT_PRICE_PER_MTOK_USD=15.00
MODEL_CACHE_READ_PRICE_PER_MTOK_USD=0.30
MODEL_CACHE_WRITE_5M_PRICE_PER_MTOK_USD=3.75
MODEL_CACHE_WRITE_1H_PRICE_PER_MTOK_USD=6.00
BASE_SYSTEM_PROMPT=
BASE_SYSTEM_PROMPT_MAX_BYTES=200000
AGENTCORE_GATEWAYS_JSON={}
ENABLE_GATEWAYS=true
CODE_INTERPRETER_ID=
CODE_INTERPRETER_REGION=ap-southeast-1
ENABLE_CODE_INTERPRETER=true
CODE_INTERPRETER_SESSION_TIMEOUT_SECONDS=1800
CODE_INTERPRETER_MAX_RESULT_CHARS=200000
MEMORY_ID=
MEMORY_REGION=ap-southeast-1
MEMORY_BATCH_SIZE=10
MEMORY_TOP_K=5
MEMORY_RELEVANCE_SCORE=0.2
SKILLS_BUCKET=
SKILLS_PREFIX=
SKILLS_LOCAL_DIR=/tmp/strands-agent-skills
SKILLS_MAX_OBJECT_BYTES=50000000
SKILLS_MAX_SYNC_BYTES=250000000
SKILLS_MAX_RESOURCE_CHARS=100000
```

If the console does not accept an empty value, omit `BASE_SYSTEM_PROMPT`, `CODE_INTERPRETER_ID`, `SKILLS_BUCKET`, and `SKILLS_PREFIX`. Keep `AGENTCORE_GATEWAYS_JSON={}` because it is unambiguous. To disable Memory without falling back to the packaged project ID, explicitly save `MEMORY_ID` as an empty value; if the console cannot retain empty values, use a deployment API that can pass the empty string.

### Full-feature overrides

Starting from the complete example, replace these values to enable all optional integrations:

```text
AGENTCORE_GATEWAYS_JSON={"analytics":{"label":"Analytics DB","url":"https://analytics-gateway-id.gateway.bedrock-agentcore.ap-southeast-1.amazonaws.com","arn":"arn:aws:bedrock-agentcore:ap-southeast-1:123456789012:gateway/analytics-gateway-id"}}
CODE_INTERPRETER_ID=my-custom-code-interpreter-id
CODE_INTERPRETER_REGION=ap-southeast-1
MEMORY_ID=my-agent-memory-id
MEMORY_REGION=ap-southeast-1
SKILLS_BUCKET=my-agent-config-123456789012
SKILLS_PREFIX=skills/
```

You may still leave `BASE_SYSTEM_PROMPT` empty when Dify supplies the system prompt.

## 5. Add Agent Skills to S3

Each skill is a directory directly below `SKILLS_PREFIX` and must contain `SKILL.md`:

```text
skills/
  hospital-data-analyst/
    SKILL.md
    references/
      schema.md
    scripts/
      validate.py
    assets/
      report-template.xlsx
  nuhs-ngemr-pcr-clindoc/
    SKILL.md
    agents/
      openai.yaml
```

Minimum `SKILL.md`:

```markdown
---
name: hospital-data-analyst
description: Analyze hospital data requests. Use when a request requires the hospital analytics schema, governed query workflow, or domain-specific validation.
---

# Hospital Data Analyst

Follow the governed schema and validation workflow before querying data.
```

Rules:

- Use lowercase letters, digits, and hyphens for the skill directory and frontmatter `name`.
- Make every skill name unique.
- Put trigger conditions in the frontmatter `description`.
- Store detailed text references under `references/` and link them from `SKILL.md`.
- Store deterministic helpers under `scripts/` and output templates under `assets/`.
- Do not store secrets in skills.
- Grant the Runtime role read access only to the configured skills prefix.

Upload the local skills tree:

```powershell
$configBucket = "my-agent-config-123456789012"
aws s3 sync ..\skills "s3://$configBucket/skills/"
```

Inspect the result:

```powershell
aws s3 ls "s3://$configBucket/skills/" --recursive
```

Skills are synchronized once when a Runtime container starts. After changing S3 content, deploy a new Runtime version or restart sessions so new containers load the updated files. Do not expect an already-warm container to refresh automatically.

When Code Interpreter is enabled, `stage_skill_resource` can copy a validated skill resource from S3 into the request's interpreter session. The custom Code Interpreter role therefore also needs `s3:GetObject` for the skills prefix.

## 6. Create the AgentCore Runtime execution role

### Trust policy

Create an IAM role trusted by AgentCore. Replace `REGION` and `ACCOUNT_ID`:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AllowAgentCoreRuntimeAssumeRole",
      "Effect": "Allow",
      "Principal": {
        "Service": "bedrock-agentcore.amazonaws.com"
      },
      "Action": "sts:AssumeRole",
      "Condition": {
        "StringEquals": {
          "aws:SourceAccount": "ACCOUNT_ID"
        },
        "ArnLike": {
          "aws:SourceArn": "arn:aws:bedrock-agentcore:REGION:ACCOUNT_ID:*"
        }
      }
    }
  ]
}
```

After the Runtime ARN is known, narrow `aws:SourceArn` when your deployment process permits it.

### Example Runtime permissions policy

This is a feature-complete example. Replace every placeholder and remove statements for disabled capabilities.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "InvokeConfiguredBedrockModel",
      "Effect": "Allow",
      "Action": [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream"
      ],
      "Resource": [
        "arn:aws:bedrock:MODEL_REGION:ACCOUNT_ID:application-inference-profile/PROFILE_ID",
        "arn:aws:bedrock:*::foundation-model/*"
      ]
    },
    {
      "Sid": "ReadDirectDeploymentBundle",
      "Effect": "Allow",
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::CODE_BUCKET/my-strands-agent/releases/*"
    },
    {
      "Sid": "ListSkillsPrefix",
      "Effect": "Allow",
      "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::CONFIG_BUCKET",
      "Condition": {
        "StringLike": {
          "s3:prefix": [
            "skills",
            "skills/*"
          ]
        }
      }
    },
    {
      "Sid": "ReadSkillsAndOptionalPrompt",
      "Effect": "Allow",
      "Action": "s3:GetObject",
      "Resource": [
        "arn:aws:s3:::CONFIG_BUCKET/skills/*",
        "arn:aws:s3:::CONFIG_BUCKET/prompts/*"
      ]
    },
    {
      "Sid": "InvokeConfiguredGateways",
      "Effect": "Allow",
      "Action": "bedrock-agentcore:InvokeGateway",
      "Resource": [
        "arn:aws:bedrock-agentcore:REGION:ACCOUNT_ID:gateway/GATEWAY_ID"
      ]
    },
    {
      "Sid": "UseConfiguredCodeInterpreter",
      "Effect": "Allow",
      "Action": [
        "bedrock-agentcore:StartCodeInterpreterSession",
        "bedrock-agentcore:InvokeCodeInterpreter",
        "bedrock-agentcore:StopCodeInterpreterSession"
      ],
      "Resource": [
        "arn:aws:bedrock-agentcore:REGION:ACCOUNT_ID:code-interpreter-custom/CODE_INTERPRETER_ID",
        "arn:aws:bedrock-agentcore:REGION:ACCOUNT_ID:code-interpreter-custom/CODE_INTERPRETER_ID/*"
      ]
    },
    {
      "Sid": "UseConfiguredMemory",
      "Effect": "Allow",
      "Action": [
        "bedrock-agentcore:CreateEvent",
        "bedrock-agentcore:GetMemory",
        "bedrock-agentcore:ListEvents",
        "bedrock-agentcore:GetMemoryRecord",
        "bedrock-agentcore:ListMemoryRecords",
        "bedrock-agentcore:RetrieveMemoryRecords"
      ],
      "Resource": "arn:aws:bedrock-agentcore:REGION:ACCOUNT_ID:memory/MEMORY_ID"
    },
    {
      "Sid": "RuntimeLogGroups",
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup",
        "logs:DescribeLogGroups",
        "logs:DescribeLogStreams"
      ],
      "Resource": "arn:aws:logs:REGION:ACCOUNT_ID:log-group:/aws/bedrock-agentcore/runtimes/*"
    },
    {
      "Sid": "RuntimeLogStreams",
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogStream",
        "logs:PutLogEvents"
      ],
      "Resource": "arn:aws:logs:REGION:ACCOUNT_ID:log-group:/aws/bedrock-agentcore/runtimes/*:log-stream:*"
    },
    {
      "Sid": "RuntimeTracing",
      "Effect": "Allow",
      "Action": [
        "xray:PutTraceSegments",
        "xray:PutTelemetryRecords",
        "xray:GetSamplingRules",
        "xray:GetSamplingTargets"
      ],
      "Resource": "*"
    },
    {
      "Sid": "RuntimeMetrics",
      "Effect": "Allow",
      "Action": "cloudwatch:PutMetricData",
      "Resource": "*",
      "Condition": {
        "StringEquals": {
          "cloudwatch:namespace": "bedrock-agentcore"
        }
      }
    }
  ]
}
```

Policy notes:

- Replace `PROFILE_ID` and restrict the foundation-model ARNs to the Regions/models used by your inference profile when possible.
- Remove `InvokeConfiguredGateways`, `UseConfiguredCodeInterpreter`, or `UseConfiguredMemory` when that capability is disabled.
- Remove the prompt resource if `BASE_SYSTEM_PROMPT` is empty.
- Add `kms:Decrypt` on the appropriate KMS key when the ZIP, prompt, or skills use SSE-KMS.
- VPC mode can require additional network-interface permissions or a service-linked network role, depending on how the Runtime is created.

## 7. Create the custom Code Interpreter execution role

Skip this section when `CODE_INTERPRETER_ID` is empty.

The Runtime role starts and invokes Code Interpreter sessions. The custom Code Interpreter's own execution role supplies credentials to commands running inside its microVM. Keep this role tightly scoped because generated code can access its credentials.

### Code Interpreter trust policy

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Service": "bedrock-agentcore.amazonaws.com"
      },
      "Action": "sts:AssumeRole",
      "Condition": {
        "StringEquals": {
          "aws:SourceAccount": "ACCOUNT_ID"
        }
      }
    }
  ]
}
```

### Code Interpreter S3 policy

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ReadSkillResources",
      "Effect": "Allow",
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::CONFIG_BUCKET/skills/*"
    },
    {
      "Sid": "ReadUserInputs",
      "Effect": "Allow",
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::USER_FILES_BUCKET/input/*"
    },
    {
      "Sid": "WriteGeneratedOutputs",
      "Effect": "Allow",
      "Action": "s3:PutObject",
      "Resource": "arn:aws:s3:::USER_FILES_BUCKET/output/*"
    }
  ]
}
```

Add `s3:ListBucket` only if interpreter commands need to enumerate a prefix. Add KMS permissions only for the exact keys protecting those objects. Do not give Code Interpreter access to the deployment ZIP, unrelated buckets, IAM, Secrets Manager, or broad account resources.

Create or select a custom Code Interpreter using this role, then place its identifier in `CODE_INTERPRETER_ID`.

## 8. Create the Runtime in the AgentCore console

AWS console labels can evolve, but the current direct-code flow is:

1. Open Amazon Bedrock AgentCore in `REGION`.
2. From the Agents home page, choose **Host Agent**.
3. Choose **S3 Source - Upload from S3 bucket**.
4. Select the deployment ZIP object from `CODE_BUCKET`.
5. Enter an agent name such as `my_strands_agent`.
6. Select **Python 3.13**.
7. Set the entry point to `strands_agent/main.py`.
8. Select the Runtime execution role created above.
9. Choose the network mode:
   - Use `PUBLIC` for the simplest setup when the Runtime may reach AWS public endpoints.
   - Use `VPC` only after configuring subnets, security groups, DNS, NAT or required VPC endpoints.
10. Add the environment variables for your selected profile.
11. Review lifecycle timeouts and create the agent.
12. Wait for the Runtime version to become ready.
13. Choose **Create Endpoint**, then select that endpoint for testing.

For a VPC-only Runtime using Gateway, create the separate `com.amazonaws.REGION.bedrock-agentcore.gateway` interface endpoint. The ordinary `bedrock-agentcore` endpoint does not cover Gateway hostnames. Also provide connectivity to Bedrock Runtime, S3, CloudWatch, and any other configured service.

## 9. Test the deployed Runtime

### Console test

Use the endpoint Playground/Sandbox with a simple blocking payload:

```json
{
  "prompt": "What capabilities are available?",
  "user_id": "test-user",
  "stream": false
}
```

Test Dify-style system guidance:

```json
{
  "model": "my-strands-agent",
  "messages": [
    {
      "role": "system",
      "content": "You are a concise healthcare data assistant. Use only configured tools and activated skills."
    },
    {
      "role": "user",
      "content": "Explain which skills you can use."
    }
  ],
  "actor_id": "test-user",
  "session_id": "test-conversation-000000000000000001",
  "stream": false
}
```

### Boto3 test

The caller needs `bedrock-agentcore:InvokeAgentRuntime` on the Runtime/endpoint. AgentCore requires `runtimeSessionId` to contain at least 33 characters.

```python
import json
import uuid

import boto3


REGION = "ap-southeast-1"
RUNTIME_ARN = "arn:aws:bedrock-agentcore:REGION:ACCOUNT_ID:runtime/RUNTIME_ID"

client = boto3.client("bedrock-agentcore", region_name=REGION)
payload = {
    "prompt": "What capabilities are available?",
    "user_id": "test-user",
    "stream": False,
}

response = client.invoke_agent_runtime(
    agentRuntimeArn=RUNTIME_ARN,
    runtimeSessionId=str(uuid.uuid4()),
    qualifier="DEFAULT",
    payload=json.dumps(payload).encode("utf-8"),
)

body = response["response"].read()
print(body.decode("utf-8"))
```

Use a different session ID for each conversation. Reuse the same session ID for follow-up turns in that conversation. When Memory is enabled, also keep `actor_id` stable for the user.

## 10. Verify each optional integration

### No Gateway or Code Interpreter

With `AGENTCORE_GATEWAYS_JSON={}` and an empty `CODE_INTERPRETER_ID`, the Runtime starts normally. Extending the tool list with an empty Gateway client list is a no-op.

### Gateway

Ask a question that requires one configured MCP tool. Check Runtime logs for MCP connection or authorization errors. A `403` normally means the Runtime role lacks `bedrock-agentcore:InvokeGateway`, the ARN is wrong, or Gateway inbound authorization does not accept IAM callers.

### Code Interpreter

Ask for a small calculation, then test staging a known skill resource. `AccessDenied` from the Code Interpreter's `aws s3 cp` command points to the custom Code Interpreter role, not the Runtime role.

### Memory

Send two turns with the same actor and session. Ask the second turn to refer to the first. Check permissions for `CreateEvent`, `ListEvents`, `RetrieveMemoryRecords`, and `GetMemory` if restoration or persistence fails.

### Skills

Ask a question matching a skill's frontmatter description. Confirm the model activates the skill before using its related tools. If no skills appear:

1. Verify `SKILLS_BUCKET` and `SKILLS_PREFIX`.
2. Verify the Runtime role can list the bucket and read the prefix.
3. Confirm every directory contains a valid `SKILL.md`.
4. Start a new Runtime version/session after uploading changes.

When skills are intentionally disabled, confirm that `SKILLS_BUCKET` is empty or omitted. The Runtime should start normally and expose no skills-related tools.

## 11. Monitor logs and cache usage

The Runtime logs one `MODEL_USAGE` JSON record per model invocation when `ENABLE_MODEL_USAGE_LOGS=true`. It contains no prompt or response text.

CloudWatch Logs Insights example:

```text
fields @timestamp, @message
| filter @message like /MODEL_USAGE/
| parse @message /"session_id":"(?<session_id>[^"]+)"/
| parse @message /"cache_read_input_tokens":(?<cache_read>\d+)/
| parse @message /"cache_write_input_tokens":(?<cache_write>\d+)/
| parse @message /"estimated_cost_usd":(?<estimated_cost>[0-9.]+)/
| sort @timestamp desc
| display @timestamp, session_id, cache_read, cache_write, estimated_cost
```

Interpretation:

- `cache_write_input_tokens > 0`: Bedrock created or refreshed a prompt cache entry.
- `cache_read_input_tokens > 0`: the request reused cached input.
- Both zero: the prompt may be too short, changed before the cache point, expired, or the model/profile may not support the selected cache behavior.

## 12. Update the Runtime

For a code update:

1. Build a ZIP with a new versioned filename.
2. Upload it under a new S3 key.
3. In the agent details page, choose **Update hosting**.
4. Select the new object and review environment variables and role settings.
5. Create the new Runtime version.
6. Point the endpoint to the new ready version.
7. Run smoke tests before retiring the old version.

For skills-only changes, upload the skills and start new Runtime sessions or deploy a new version so warm containers do not retain stale synchronized files.

## Troubleshooting

| Symptom | Likely cause | Resolution |
| --- | --- | --- |
| Runtime cannot import `main.py` | Wrong entry point | Use `strands_agent/main.py` for the supplied ZIP |
| Runtime version fails during startup | Wrong architecture or missing dependency | Rebuild with `build_agentcore_bundle.ps1`; do not ZIP local Windows packages |
| `MODEL_ID` validation or invocation error | Empty/incorrect model ID, wrong Region, or missing model permission | Set a valid ID/ARN, `MODEL_REGION`, and matching Bedrock IAM resources |
| Invalid Gateway configuration | Malformed JSON or URL/ARN mismatch | Use one-line JSON; ensure hostname, ID, and Region match |
| Gateway `403` | Missing `InvokeGateway` or inbound auth mismatch | Scope `bedrock-agentcore:InvokeGateway` to the configured ARN and use IAM inbound auth |
| Gateway DNS failure in VPC | Missing Gateway-specific endpoint | Add `com.amazonaws.REGION.bedrock-agentcore.gateway` or suitable egress |
| Code Interpreter not present | Empty ID or `ENABLE_CODE_INTERPRETER=false` | Configure the custom ID and enable flag |
| Code Interpreter S3 copy denied | Custom Code Interpreter role lacks S3/KMS access | Update that role, not only the Runtime role |
| Memory silently disabled | Empty Memory ID, actor ID, or session ID | Provide all three and verify IAM permissions |
| Skills missing | Wrong S3 prefix, invalid `SKILL.md`, stale warm container, or denied S3 access | Correct the tree and permissions, then start a new Runtime version/session |
| Base prompt absent | Empty `BASE_SYSTEM_PROMPT` | This is expected for Dify-owned prompts; send a system message |
| Base prompt load fails | Invalid S3 URI, empty object, encoding issue, size limit, or denied S3/KMS access | Upload non-empty UTF-8 text and correct permissions/limits |

## AWS references

- [Direct code deployment for Python](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-get-started-code-deploy-python.html)
- [AgentCore Runtime IAM permissions](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-permissions.html)
- [Invoke an AgentCore Runtime agent](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-invoke-agent.html)
- [Gateway IAM inbound authorization](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-inbound-auth.html)
- [Code Interpreter S3 integration](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/code-interpreter-s3-integration.html)
- [AgentCore credentials management](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/security-credentials-management.html)
- [AgentCore Runtime security best practices](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-security-best-practices.html)

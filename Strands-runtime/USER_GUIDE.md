# Deploy the Strands Agent to Amazon Bedrock AgentCore Runtime

This guide starts with a prebuilt Runtime ZIP that has already been uploaded to S3. Use the same ZIP to create separate Amazon Bedrock AgentCore Runtime configurations for different use cases. Each Runtime gets its own environment variables, IAM permissions, optional tools, Memory, and skills without rebuilding the ZIP.

The examples use these placeholders:

| Placeholder | Example |
| --- | --- |
| `ACCOUNT_ID` | `123456789012` |
| `REGION` | `ap-southeast-1` |
| `MODEL_REGION` | `us-east-1` |
| `RUNTIME_NAME` | `my_strands_agent` |
| `CODE_BUCKET` | `my-agentcore-code-123456789012` |
| `ZIP_S3_URI` | `s3://my-agentcore-code-123456789012/releases/strands_agent.zip` |
| `CONFIG_BUCKET` | `my-agent-config-123456789012` |

Replace every placeholder and sample ARN before using a command or policy.

## What you will create

You will select or create:

1. The existing S3 ZIP object supplied by the bundle owner.
2. A Bedrock model or application inference profile.
3. An AgentCore Runtime execution role.
4. Optional AgentCore Gateways that expose MCP tools.
5. An optional custom AgentCore Code Interpreter and its separate execution role.
6. An optional AgentCore Memory resource.
7. An optional S3 bucket or prefix containing Agent Skills.
8. An AgentCore Runtime and endpoint.

The Runtime creates one Strands `Agent` for each invocation. Dify can provide the application system prompt in an OpenAI-style system message. Gateway and Code Interpreter tools are added only when their corresponding environment configuration is present.

## Prerequisites

- Access to the AWS account and selected Region.
- Permission to create or update AgentCore Runtime resources and pass the Runtime execution role.
- The complete S3 URI of the uploaded Runtime ZIP.
- Permission for AgentCore to read that ZIP object, including `kms:Decrypt` when it uses a customer-managed KMS key.
- Permission to upload skills only when this Runtime will use Agent Skills.
- Access to the configured Bedrock model or inference profile.
- AWS CLI credentials if you use the command examples.

For production, use least-privilege policies rather than broad managed policies. The identity performing the deployment is different from the Runtime execution role. The deployment identity needs AgentCore control-plane permissions and `iam:PassRole`; the Runtime execution role is assumed by AgentCore while the agent runs.

## 1. Start from the uploaded ZIP

Obtain the full S3 URI from the bundle owner. For example:

```text
s3://my-agentcore-code-123456789012/releases/v0.0.5/strands_agent.zip
```

Do not extract, modify, or repackage it. Record these hosting settings:

| Setting | Value |
| --- | --- |
| Source | S3 ZIP object |
| Runtime | Python 3.13 |
| Entry point | `strands_agent/main.py` |
| Architecture | Linux ARM64 |

The supplied ZIP already contains the application and its vendored dependencies in this structure:

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

Do not configure an `opentelemetry-instrument` entry-point prefix unless the bundle owner explicitly provides a ZIP containing that executable. If the ZIP bucket uses a customer-managed KMS key, the role AgentCore uses to read the archive also needs `kms:Decrypt` on that key.

## 2. Create one configuration per use case

Reuse the same `ZIP_S3_URI`, but create a separate Runtime configuration for each use case. Give each one its own Runtime name, agent identity, prompt strategy, optional integrations, and least-privilege Runtime role. Environment variables are read when a Runtime container starts; changing them requires a new Runtime version or restarted container and does not change an already-running container per request.

## 3. Decide which optional capabilities to enable

| Capability | Enable it with | Disable it with |
| --- | --- | --- |
| Application base prompt | `BASE_SYSTEM_PROMPT=s3://bucket/key.txt` | Omit it or use an empty value |
| AgentCore Gateway MCP tools | Valid `AGENTCORE_GATEWAYS_JSON` | Omit it, use an empty value, or use `{}` |
| Code Interpreter | `CODE_INTERPRETER_ID=<custom-interpreter-id>` | Omit it or use an empty value |
| AgentCore Memory | `MEMORY_ID=<memory-id>` | Omit `MEMORY_ID` or use an empty value |
| Agent Skills | Set `SKILLS_BUCKET`; `SKILLS_PREFIX` is optional | Omit `SKILLS_BUCKET` or use an empty value |

Configure a valid `MODEL_ID` or `MODEL_ARN`; there is no packaged model fallback. Configure your own skills bucket and prefix only when the deployment needs skills.

### Dify-oriented configuration

For maximum prompt flexibility, leave `BASE_SYSTEM_PROMPT` empty and place the application prompt in Dify's system message. The Runtime appends caller-provided system messages to its internal skill and memory safety guidance.

The Dify system prompt remains part of the Bedrock cacheable prompt prefix. Cache reuse requires identical preceding content, the model's minimum cacheable token count, and another request within the configured TTL.

## 4. Configure environment variables by importance

Environment variable values in the AgentCore console are strings. Do not copy every variable into every Runtime. Start with the compulsory setting, add the recommended identity settings, and configure optional capabilities only when the use case needs them.

### Compulsory

| Variable | Why it is required |
| --- | --- |
| `MODEL_ID` or `MODEL_ARN` | Exactly one must identify the Bedrock model or application inference profile. The Runtime rejects an invocation when both are empty or absent. |

The selected Runtime role must also have `bedrock:InvokeModel` and `bedrock:InvokeModelWithResponseStream` access to the configured model resources. These permissions are compulsory but are IAM settings, not environment variables.

### Recommended for each Runtime configuration

These are not required by the code, but set them so logs and agent metadata clearly identify the use case.

| Variable | Recommended value |
| --- | --- |
| `AWS_DEFAULT_REGION` | Region containing the Runtime and most AgentCore resources |
| `AGENT_NAME` | Short use-case name, such as `gmio-pcr` |
| `AGENT_DESCRIPTION` | Brief role description, such as `GMIO PCR intake agent` |
| `MODEL_REGION` | Set when the model is in a different Region; an inference-profile ARN otherwise supplies it automatically |

### Optional capabilities

Omit these variables, or leave their primary identifier empty, when the capability is not needed.

| Capability | Primary environment variable | Result when empty or omitted |
| --- | --- | --- |
| Runtime-owned base prompt | `BASE_SYSTEM_PROMPT` | No base prompt is loaded; Dify can supply the system message |
| Gateway MCP tools | `AGENTCORE_GATEWAYS_JSON` | No Gateway clients or Gateway tools are added |
| Code Interpreter | `CODE_INTERPRETER_ID` | No Code Interpreter session or tools are added |
| AgentCore Memory | `MEMORY_ID` | No Memory session, API calls, or Memory guidance are added |
| Agent Skills | `SKILLS_BUCKET` | No skill sync, guidance, plugin, or skill resource tools are added |
| Skills subfolder | `SKILLS_PREFIX` | When the bucket is configured, empty means skills are stored at its root |

Each enabled capability also requires the corresponding IAM permissions described later in this guide. Do not grant Gateway, Code Interpreter, Memory, prompt-bucket, or skills-bucket access to a Runtime that does not use that capability.

### Advanced settings — normally not important

Most users should omit these variables and keep the packaged defaults. Change them only for tuning, troubleshooting, nonstandard Regions, or accurate cost estimates.

| Variables | Why you might change them |
| --- | --- |
| `AWS_REGION` | Normally supplied by AgentCore; do not override it routinely |
| `PROMPT_CACHE_TTL` | Select `1h` instead of the default `5m` only when the model supports it and longer reuse is useful |
| `ENABLE_MODEL_USAGE_LOGS` | Disable the default usage log only when operational policy requires it |
| `MODEL_PRICING_LABEL` and all `MODEL_*_PRICE_PER_MTOK_USD` variables | Update estimated-cost logs when using another model or pricing basis; they do not change AWS billing |
| `BASE_SYSTEM_PROMPT_MAX_BYTES` | Raise or lower the prompt-object size limit |
| `ENABLE_GATEWAYS`, `ENABLE_CODE_INTERPRETER` | Emergency override switches; normally leave them at `true` because an empty primary identifier already disables the capability |
| `CODE_INTERPRETER_REGION`, `CODE_INTERPRETER_SESSION_TIMEOUT_SECONDS`, `CODE_INTERPRETER_MAX_RESULT_CHARS` | Nonstandard interpreter Region, session duration, or context limit |
| `ENABLE_TOOL_DETAILS` | Set to `true` only for projects whose frontend should receive tool and skill inputs/results; defaults to `false` |
| `TOOL_DETAIL_MAX_CHARS` | Maximum streamed frontend detail for each tool input or result; the default is `200000` characters |
| `MEMORY_REGION`, `MEMORY_BATCH_SIZE`, `MEMORY_TOP_K`, `MEMORY_RELEVANCE_SCORE` | Nonstandard Memory Region or retrieval/persistence tuning |
| `SKILLS_LOCAL_DIR`, `SKILLS_MAX_OBJECT_BYTES`, `SKILLS_MAX_SYNC_BYTES`, `SKILLS_MAX_RESOURCE_CHARS` | Local cache location and skill size limits |

The following reference tables document the exact defaults and accepted values.

### Region, model, caching, and usage

| Variable | Runtime default | Recommended configuration |
| --- | --- | --- |
| `AWS_DEFAULT_REGION` | `ap-southeast-1` in fallback paths | Set to the Runtime/AgentCore resource Region |
| `AWS_REGION` | Usually supplied by AWS | Normally leave Runtime-managed; it is used as the first S3 prompt-client Region fallback |
| `MODEL_ID` | Empty | Required unless `MODEL_ARN` is set; use a Bedrock model ID or application inference profile ARN |
| `MODEL_ARN` | Empty | Alternative to `MODEL_ID`; do not set both |
| `MODEL_REGION` | Parsed from an ARN, otherwise `AWS_DEFAULT_REGION` | Set explicitly when the model is in a different Region |
| `AGENT_NAME` | `data-analyst` | Set the Strands agent's name, for example `gmio-pcr` |
| `AGENT_DESCRIPTION` | `Data analyst with connected databases and managed code execution` | Briefly describe the agent's role and available capabilities |
| `PROMPT_CACHE_TTL` | `5m` | `5m` or `1h`; the model must support the selected TTL |
| `MODEL_CONNECT_TIMEOUT_SECONDS` | `10` | Bedrock model connection timeout in seconds, constrained to 1-60 |
| `MODEL_READ_TIMEOUT_SECONDS` | `900` | Bedrock model response read timeout in seconds, constrained to 60-900 |
| `MODEL_RETRY_MAX_ATTEMPTS` | `2` | Bedrock model retry attempts, constrained to 0-5 |
| `RUNTIME_STREAM_HEARTBEAT_SECONDS` | `15` | Heartbeat interval while waiting for a model or tool, constrained to 5-300 seconds |
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
| `CODE_INTERPRETER_RESULT_MODE` | `semantic` | Compact validated semantic results by default; set `legacy` only to restore raw event JSON temporarily |
| `CODE_INTERPRETER_SEMANTIC_MAX_CHARS` | `10000` | Semantic result limit, clamped to `2000` through `20000` characters |
| `CODE_INTERPRETER_MAX_RESULT_CHARS` | `200000` | Legacy raw-result limit, minimum `1000`; used only when result mode is `legacy` |

Use a custom Code Interpreter when skill resources or user files must be copied from S3. Its execution role is separate from the Runtime execution role; see the IAM examples below.

When a request includes an uploaded file in a `<document_input>` tag, prefer Code Interpreter to download and process the file instead of relying only on its filename or S3 URL. Ensure that the custom Code Interpreter execution role has `s3:GetObject` permission for the uploaded file's S3 location.

The Runtime adds a stable Code Interpreter result contract to the model prompt
when semantic mode is active. Code and shell tasks must print one final
`AGENTCORE_RESULT_JSON=<single-line JSON object>` marker containing boolean
`ok`, a concise `summary`, and only the small aggregates needed for the next
reasoning step. The object may include `row_count`, up to 20 `columns`, up to
20 scalar `metrics`, up to 30 `sample_rows` with 20 fields each, `artifacts`
with `s3_uri`, `filename`, and `content_type`, plus bounded warnings or errors.
Do not print full dataframes, raw SQL results, recursive listings, generated
file contents, or long logs. Keep bulk results in the sandbox or S3 and return
artifact metadata instead.

If the marker is absent or malformed, the Runtime supplies a bounded automatic
fallback based on stdout and error state. If an emergency compatibility rollback
is required, set `CODE_INTERPRETER_RESULT_MODE=legacy`; no rebuild is needed.

### AgentCore Memory

| Variable | Runtime default | Recommended configuration |
| --- | --- | --- |
| `MEMORY_ID` | Empty | Set your Memory ID to enable Memory; empty or unset disables it |
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

### Minimal Dify Runtime example

This is all most Dify-owned, tool-free Runtime configurations need. Omitted optional identifiers disable Gateway, Code Interpreter, Memory, skills, and the S3 base prompt. Dify supplies the application system message at invocation time.

```text
AWS_DEFAULT_REGION=ap-southeast-1
MODEL_ID=arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/your-profile-id
MODEL_REGION=us-east-1
AGENT_NAME=gmio-pcr
AGENT_DESCRIPTION=GMIO PCR intake agent
```

There is no need to create empty environment-variable entries. Omission disables each optional capability.

### Full-feature overrides

Starting from the minimal example, add only the integrations needed by this Runtime:

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
  domain-specialist/
    SKILL.md
    references/
      schema.md
    scripts/
      validate.py
    assets/
      report-template.xlsx
```

Minimum `SKILL.md`:

```markdown
---
name: domain-specialist
description: Apply the governed domain workflow. Use when a request requires domain-specific rules, references, or validation.
---

# Domain Specialist

Follow the governed references and validation workflow before completing the task.
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
4. Select the existing deployment ZIP object from `ZIP_S3_URI`.
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

1. Obtain the new versioned `ZIP_S3_URI` from the bundle owner.
2. In the agent details page, choose **Update hosting**.
3. Select the new S3 object and review environment variables and role settings.
4. Create the new Runtime version.
5. Point the endpoint to the new ready version.
6. Run smoke tests before retiring the old version.

For skills-only changes, upload the skills and start new Runtime sessions or deploy a new version so warm containers do not retain stale synchronized files.

## Troubleshooting

| Symptom | Likely cause | Resolution |
| --- | --- | --- |
| Runtime cannot import `main.py` | Wrong entry point | Use `strands_agent/main.py` for the supplied ZIP |
| Runtime version fails during startup | Wrong ZIP, architecture, or missing dependency | Confirm `ZIP_S3_URI`, Python 3.13, and `strands_agent/main.py`; ask the bundle owner for a corrected release rather than repackaging it |
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

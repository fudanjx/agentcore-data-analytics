# AgentCore Strands Data Analyst

This directory is an upload-ready Amazon Bedrock AgentCore **S3 source** bundle. It keeps the generated `main.py`/`BedrockAgentCoreApp` contract and translates the capabilities from `agentcore-data-analytics/app` into a Strands-native agent.

For a complete self-service deployment walkthrough, including every environment variable, Agent Skills, IAM policies, and console testing, see [USER_GUIDE.md](USER_GUIDE.md).

## Included capabilities

- A new Strands `Agent` per invocation, preventing conversation state from leaking between users.
- The reference Bedrock application inference profile, configurable through `MODEL_ID` or `MODEL_ARN`.
- Optional AgentCore Gateway MCP connections through directly signed SigV4 HTTP transports.
- Optional request-scoped managed AgentCore Code Interpreter tools for code and shell execution.
- Native Strands `AgentCoreMemorySessionManager` integration for session restoration, semantic/preference/summary retrieval, and batched turn persistence.
- Native Strands Agent Skills synced from S3, advertised by name and description, and activated on demand.
- OpenAI-style `messages` and simple AgentCore `prompt`, `input`, or `inputText` payloads.
- OpenAI-compatible streaming by default for `messages` payloads, matching the Dify/OpenAI proxy; simple `prompt` payloads remain blocking unless `stream` is true.

## Entry point and request formats

The build script places the application beneath `strands_agent/` in the ZIP. Configure the S3 source entry point as:

```text
strands_agent/main.py
```

Simple request:

```json
{"prompt":"How many rows are in the EMD table?","user_id":"user-123"}
```

Chat request:

```json
{
  "model": "strands-data-analyst",
  "messages": [
    {"role": "system", "content": "Answer concisely."},
    {"role": "user", "content": "Summarize last month's admissions."}
  ],
  "actor_id": "user-123",
  "session_id": "conversation-000000000000000000001",
  "stream": false
}
```

The blocking response is:

```json
{
  "result": "...",
  "session_id": "conversation-000000000000000000001",
  "model": "strands-data-analyst"
}
```

With `"stream": true`, AgentCore returns OpenAI `chat.completion.chunk` SSE objects with `choices[0].delta.content`, structured `agent_step` sideband events, a final `model_usage` sideband event, and a final stop chunk. Every `agent_step` includes a stable tool-use ID plus sanitized type/name and lifecycle status. When `ENABLE_TOOL_DETAILS=true`, it also includes parsed input and bounded result content; native `skills` results therefore carry the activated skill's complete instructions unless the configured detail limit truncates them. Tool details are disabled by default so one universal bundle can serve projects with different disclosure requirements.

The Dify proxy exposes each step as an `agent_step` OpenAI response extension and embeds the same JSON as a base64-encoded `<!--agentcore-step:...-->` content marker, because Dify's model-provider layer otherwise retains only text. A frontend can remove the marker, decode it as UTF-8 JSON, and decide whether to hide, summarize, or expand the details. The visible Markdown status line is retained for clients that do not parse markers.

The proxy converts `model_usage` into an OpenAI usage chunk so Dify can persist aggregate prompt and completion tokens. Cache-read, cache-write, and estimated-cost details remain in the Runtime's `MODEL_USAGE` CloudWatch record because Dify's standard message schema only supports aggregate token counts. A `messages` payload defaults to this streaming contract even when `stream` is omitted because the existing Dify/OpenAI proxy expects the Runtime to stream; set `"stream": false` explicitly to request blocking JSON directly.

The Dify proxy independently limits accepted serialized step details with `RUNTIME_STEP_DETAIL_MAX_CHARS` (default `500000`, constrained to 1,000-1,000,000). Keep that value at least as large as twice `TOOL_DETAIL_MAX_CHARS` when both a maximum-size input and result must fit in one completed step.

## Configuration

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `MODEL_ID` / `MODEL_ARN` | Empty | Required Bedrock model ID or application inference profile ARN used by Strands |
| `MODEL_REGION` | Region parsed from a model ARN, otherwise AWS default | Bedrock Runtime client region |
| `AGENT_NAME` | `data-analyst` | Name passed to the Strands agent |
| `AGENT_DESCRIPTION` | `Data analyst with connected databases and managed code execution` | Description passed to the Strands agent |
| `PROMPT_CACHE_TTL` | `5m` | Prompt-cache TTL for system/message and tool cache points; accepted values are `5m` and `1h`, and the selected model must support the requested TTL |
| `MODEL_CONNECT_TIMEOUT_SECONDS` | `10` | Bedrock model connection timeout, constrained to 1-60 seconds |
| `MODEL_READ_TIMEOUT_SECONDS` | `900` | Bedrock model response read timeout, constrained to 60-900 seconds |
| `MODEL_RETRY_MAX_ATTEMPTS` | `2` | Maximum Bedrock model retry attempts, constrained to 0-5 |
| `RUNTIME_STREAM_HEARTBEAT_SECONDS` | `15` | Emit a heartbeat sideband event while waiting for a model or tool, constrained to 5-300 seconds |
| `ENABLE_MODEL_USAGE_LOGS` | `true` | Emit one `MODEL_USAGE` JSON log after every invocation, without prompt or response content |
| `MODEL_PRICING_LABEL` | `claude-sonnet-4.6-standard-2026-08` | Label included with estimated-cost logs so the configured rates can be audited |
| `MODEL_INPUT_PRICE_PER_MTOK_USD` | `3.00` | Estimated uncached-input price in USD per million tokens |
| `MODEL_OUTPUT_PRICE_PER_MTOK_USD` | `15.00` | Estimated output price in USD per million tokens |
| `MODEL_CACHE_READ_PRICE_PER_MTOK_USD` | `0.30` | Estimated cache-read price in USD per million tokens |
| `MODEL_CACHE_WRITE_5M_PRICE_PER_MTOK_USD` | `3.75` | Estimated five-minute cache-write price in USD per million tokens |
| `MODEL_CACHE_WRITE_1H_PRICE_PER_MTOK_USD` | `6.00` | Estimated one-hour cache-write price in USD per million tokens |
| `BASE_SYSTEM_PROMPT` | Empty | Optional `s3://bucket/key.txt` URI for the UTF-8 base system prompt; no base prompt is added when empty or unset |
| `BASE_SYSTEM_PROMPT_MAX_BYTES` | `200000` | Maximum permitted size of the S3 system-prompt object |
| `AGENTCORE_GATEWAYS_JSON` | Empty | Gateway label, HTTPS URL, ARN, and inferred region mapping; no Gateway tools are added when empty, unset, or `{}` |
| `ENABLE_GATEWAYS` | `true` | Enable Gateway MCP tools |
| `CODE_INTERPRETER_ID` | Empty | Managed Code Interpreter identifier; no interpreter tools are added when empty or unset |
| `CODE_INTERPRETER_REGION` | `AWS_DEFAULT_REGION` or `ap-southeast-1` | Interpreter region |
| `ENABLE_CODE_INTERPRETER` | `true` | Enable interpreter tools |
| `CODE_INTERPRETER_SESSION_TIMEOUT_SECONDS` | `1800` | Session timeout, constrained to 60-28,800 seconds |
| `CODE_INTERPRETER_MAX_RESULT_CHARS` | `200000` | Tool-result context limit |
| `ENABLE_TOOL_DETAILS` | `false` | Include bounded tool/skill inputs and results in streamed `agent_step` events |
| `TOOL_DETAIL_MAX_CHARS` | `200000` | Maximum serialized characters exposed for each streamed tool input or result, constrained to 1,000-1,000,000 |
| `MEMORY_ID` | Empty | AgentCore Memory resource; empty or unset disables Memory |
| `MEMORY_REGION` | `ap-southeast-1` | Memory region |
| `MEMORY_BATCH_SIZE` | `10` | Native session-manager message batch size, flushed at invocation cleanup |
| `MEMORY_TOP_K` | `5` | Maximum long-term records retrieved from each active strategy |
| `MEMORY_RELEVANCE_SCORE` | `0.2` | Minimum long-term-memory relevance score, constrained to 0-1 |
| `SKILLS_BUCKET` | Empty | S3 bucket holding complete Agent Skill packages; required to enable skills |
| `SKILLS_PREFIX` | Empty | Optional S3 skills prefix; empty means skills are stored at the bucket root |
| `SKILLS_LOCAL_DIR` | `/tmp/strands-agent-skills` | Writable runtime cache |
| `SKILLS_MAX_OBJECT_BYTES` | `50000000` | Maximum size of one downloaded skill object |
| `SKILLS_MAX_SYNC_BYTES` | `250000000` | Maximum combined size downloaded during one startup sync |
| `SKILLS_MAX_RESOURCE_CHARS` | `100000` | Maximum UTF-8 text returned by one `read_skill_resource` call |

`MODEL_ID` or `MODEL_ARN` must be configured. Set `BASE_SYSTEM_PROMPT`, `AGENTCORE_GATEWAYS_JSON`, `CODE_INTERPRETER_ID`, `MEMORY_ID`, or `SKILLS_BUCKET` only when that optional capability belongs in the Runtime. Empty values disable the base prompt or corresponding tools, allowing a caller such as Dify to provide the application system prompt. Skills are enabled when `SKILLS_BUCKET` is non-empty; an empty `SKILLS_PREFIX` reads skills from the bucket root. `ENABLE_GATEWAYS=false` and `ENABLE_CODE_INTERPRETER=false` can still override configured integrations for a minimal smoke test.

Each completed or failed model invocation emits one `MODEL_USAGE` record containing non-cached input, output, cache-read, cache-write, total-input token counts, cache-read ratio, duration, and estimated USD cost. Bedrock reports `inputTokens` as only the input that was neither read from nor written to cache, so total input is calculated as `inputTokens + cacheReadInputTokens + cacheWriteInputTokens`. The default rates match the Runtime's reference Claude Sonnet 4.6 profile as of August 2026; override them when the model, inference tier, routing type, negotiated pricing, or published AWS rates change. The estimate covers model-token charges only and is not a billing record.

Example CloudWatch Logs Insights query:

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

When memory is enabled, AgentCore Memory is the source of truth for prior conversation turns. The Runtime sends only the latest user message from an OpenAI-style `messages` payload to Strands, preventing the caller's flattened history from duplicating the session restored by `AgentCoreMemorySessionManager`. Raw tool requests and results are excluded from durable conversational memory; the user turn and final assistant response are retained. Legacy plain-text events written by the previous Runtime implementation remain readable.

## Skills

When `SKILLS_BUCKET` is configured, the startup lifespan syncs every S3 object beneath `SKILLS_PREFIX` into `SKILLS_LOCAL_DIR`, preserving the hierarchy and enforcing per-object and total size limits. An empty prefix means the bucket root. Each skill must use the Agent Skills directory format:

```text
skills/
  hospital-data-analyst-nuh/
    SKILL.md
    references/
      emd.md
      schema.json
    scripts/
      validate.py
    assets/
      report-template.xlsx
```

Each `SKILL.md` requires YAML frontmatter containing a unique `name` and a useful `description`; the name should match its directory. The request-scoped agent registers Strands' `AgentSkills` plugin against the local parent directory. Strands places only skill metadata in the system prompt and adds its native `skills` activation tool. When the model activates a relevant skill, that tool returns the complete `SKILL.md` instructions.

If `SKILLS_BUCKET` is empty or unset, the Runtime skips synchronization and does not add skill activation guidance, the `AgentSkills` plugin, `read_skill_resource`, or `stage_skill_resource`.

Gateway MCP clients and managed Code Interpreter remain operational tools. They are not registered as skills. Runtime guidance directs the model to activate a matching skill before using its related domain tools. When the activated instructions require a UTF-8 text resource, the bounded `read_skill_resource` tool reads it from the local skill cache without allowing access outside that skill's directory.

When Code Interpreter is enabled, the request-scoped `stage_skill_resource` tool validates a selected resource against the synchronized skill package, derives its URI beneath the configured S3 skills prefix, and copies it into the active interpreter session. The custom Code Interpreter execution role therefore needs `s3:GetObject` on `arn:aws:s3:::<SKILLS_BUCKET>/<SKILLS_PREFIX>*`, plus `kms:Decrypt` when the objects use a customer-managed KMS key. Scripts are downloaded and can be staged, but they are never executed automatically. Restart or redeploy the Runtime after changing S3 content because synchronization occurs once during container startup.

To customize the base prompt without rebuilding the ZIP, upload a UTF-8 text
file and configure, for example:

```text
BASE_SYSTEM_PROMPT=s3://my-runtime-config/prompts/data-analyst.txt
```

The object is downloaded once per warm Runtime container. If the variable is
empty or unset, the Runtime adds no base application prompt; caller-provided
system guidance (for example, from Dify) is still passed to Strands. If the
variable is set but cannot be loaded or is invalid, the invocation fails
explicitly rather than silently changing agent behavior.

## Required IAM permissions

The S3-source Runtime execution role must be granted access to:

- `bedrock:InvokeModel` and `bedrock:InvokeModelWithResponseStream` for the configured model or inference profile;
- `bedrock-agentcore:InvokeGateway` for every configured Gateway ARN;
- `bedrock-agentcore:StartCodeInterpreterSession`, `InvokeCodeInterpreter`, and `StopCodeInterpreterSession` for the configured interpreter;
- the required AgentCore Memory data-plane operations and `bedrock-agentcore-control:GetMemory` for the configured Memory;
- `s3:ListBucket` on the skills bucket and `s3:GetObject` on the skills prefix;
- `s3:GetObject` on the object configured by `BASE_SYSTEM_PROMPT`, when used. If that object uses a customer-managed KMS key, also grant `kms:Decrypt` on the key.

Only permissions for integrations explicitly configured through environment variables are required.

## Packaging

Build a fresh Linux ARM64/Python 3.13 dependency bundle from PowerShell. Docker Desktop must be running:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\build_agentcore_bundle.ps1 `
  -OutputPath .\dist\strands_agent_bundle.zip
```

Pass `-Force` to replace an existing output file. The script keeps the existing artifact until its replacement passes validation. It installs every exact requirement pin in a clean ARM64 container, verifies the package versions and runtime imports, and copies only the application files into the `strands_agent/` directory expected by AgentCore:

```text
strands_agent/main.py
strands_agent/agent.py
strands_agent/code_interpreter.py
strands_agent/gateway_config.py
strands_agent/gateway_proxy.py
strands_agent/memory.py
strands_agent/skills_sync.py
strands_agent/system_prompt.py
strands_agent/requirements.txt
strands_agent/<freshly installed dependencies>
```

For a versioned release artifact:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
    -File .\build_agentcore_bundle.ps1 `
    -OutputPath .\dist\strands_agent_v0.0.5.zip `
    -Force
```

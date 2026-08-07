# AgentCore Strands Data Analyst

This directory is an upload-ready Amazon Bedrock AgentCore **S3 source** bundle. It keeps the generated `main.py`/`BedrockAgentCoreApp` contract and translates the capabilities from `agentcore-data-analytics/app` into a Strands-native agent.

## Included capabilities

- A new Strands `Agent` per invocation, preventing conversation state from leaking between users.
- The reference Bedrock application inference profile, configurable through `MODEL_ID` or `MODEL_ARN`.
- Three AgentCore Gateway MCP connections (`NUH`, `AH`, and `TimesFM`) through directly signed SigV4 HTTP transports.
- Request-scoped managed AgentCore Code Interpreter tools for code and shell execution.
- AgentCore Memory retrieval for current-session history and semantic, preference, and summary records, plus completed-turn persistence.
- Markdown analysis skills loaded from S3 and added to the system context.
- OpenAI-style `messages` and simple AgentCore `prompt`, `input`, or `inputText` payloads.
- OpenAI-compatible streaming by default for `messages` payloads, matching the Dify/OpenAI proxy; simple `prompt` payloads remain blocking unless `stream` is true.

## Entry point and request formats

Configure the S3 source entry point as:

```text
main.py
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

With `"stream": true`, AgentCore returns OpenAI `chat.completion.chunk` SSE objects with `choices[0].delta.content`, plus sanitized `agent_step` sideband events and a final stop chunk. Tool inputs and results are not exposed in status events. A `messages` payload defaults to this streaming contract even when `stream` is omitted because the existing Dify/OpenAI proxy expects the Runtime to stream; set `"stream": false` explicitly to request blocking JSON directly.

## Configuration

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `MODEL_ID` / `MODEL_ARN` | Reference application inference profile ARN | Bedrock model used by Strands |
| `MODEL_REGION` | Region parsed from a model ARN, otherwise AWS default | Bedrock Runtime client region |
| `BASE_SYSTEM_PROMPT` | Empty | Optional `s3://bucket/key.txt` URI for the UTF-8 base system prompt; the packaged prompt is used when unset |
| `BASE_SYSTEM_PROMPT_MAX_BYTES` | `200000` | Maximum permitted size of the S3 system-prompt object |
| `AGENTCORE_GATEWAYS_JSON` | Reference NUH, AH, and TimesFM Gateways | Gateway label, HTTPS URL, ARN, and inferred region mapping |
| `ENABLE_GATEWAYS` | `true` | Enable Gateway MCP tools |
| `CODE_INTERPRETER_ID` | Reference runtime interpreter | Managed Code Interpreter identifier |
| `CODE_INTERPRETER_REGION` | `AWS_DEFAULT_REGION` or `ap-southeast-1` | Interpreter region |
| `ENABLE_CODE_INTERPRETER` | `true` | Enable interpreter tools |
| `CODE_INTERPRETER_SESSION_TIMEOUT_SECONDS` | `1800` | Session timeout, constrained to 60-28,800 seconds |
| `CODE_INTERPRETER_MAX_RESULT_CHARS` | `200000` | Tool-result context limit |
| `MEMORY_ID` | Reference runtime Memory ID | AgentCore Memory resource |
| `MEMORY_REGION` | `ap-southeast-1` | Memory region |
| `MEMORY_MAX_SHORT_TERM_EVENTS` | `30` | Recent events loaded per invocation |
| `MEMORY_MAX_SHORT_TERM_CONTEXT_CHARS` | `40000` | Recent-history prompt limit |
| `SKILLS_BUCKET` | `ah-data-analytics` | S3 bucket holding Markdown skills |
| `SKILLS_PREFIX` | `skills/` | S3 skills prefix |
| `SKILLS_LOCAL_DIR` | `/tmp/strands-agent-skills` | Writable runtime cache |
| `SKILLS_MAX_PROMPT_CHARS` | `50000` | Combined skill-context limit |

Set `CODE_INTERPRETER_ID` or `MEMORY_ID` to an empty string to disable that integration. `ENABLE_GATEWAYS=false` and `ENABLE_CODE_INTERPRETER=false` are useful for a minimal smoke test.

To customize the base prompt without rebuilding the ZIP, upload a UTF-8 text
file and configure, for example:

```text
BASE_SYSTEM_PROMPT=s3://my-runtime-config/prompts/data-analyst.txt
```

The object is downloaded once per warm Runtime container. If the variable is
unset, the packaged default is used. If it is set but cannot be loaded or is
invalid, the invocation fails explicitly rather than silently changing agent
behavior.

## Required IAM permissions

The S3-source Runtime execution role must be granted access to:

- `bedrock:InvokeModel` and `bedrock:InvokeModelWithResponseStream` for the configured model or inference profile;
- `bedrock-agentcore:InvokeGateway` for every configured Gateway ARN;
- `bedrock-agentcore:StartCodeInterpreterSession`, `InvokeCodeInterpreter`, and `StopCodeInterpreterSession` for the configured interpreter;
- the required AgentCore Memory data-plane operations and `bedrock-agentcore-control:GetMemory` for the configured Memory;
- `s3:ListBucket` on the skills bucket and `s3:GetObject` on the skills prefix;
- `s3:GetObject` on the object configured by `BASE_SYSTEM_PROMPT`, when used. If that object uses a customer-managed KMS key, also grant `kms:Decrypt` on the key.

The exact reference ARNs are intentionally retained as defaults, but a new Runtime role does not inherit permission to use them. Add these permissions in the AgentCore source configuration or replace the defaults with resources owned by that Runtime/account.

## Packaging

Build a fresh Linux ARM64/Python 3.13 dependency bundle from PowerShell. Docker Desktop must be running:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\build_agentcore_bundle.ps1 `
  -OutputPath .\dist\strands_agent_bundle.zip
```

Pass `-Force` to replace an existing output file. The script installs the pinned requirements in a clean ARM64 container, verifies the Strands and MCP imports, and copies every top-level file in this directory except the build script into the `strands_agent/` directory expected by AgentCore:

```text
strands_agent/main.py
strands_agent/agent.py
strands_agent/code_interpreter.py
strands_agent/gateway_config.py
strands_agent/gateway_proxy.py
strands_agent/memory.py
strands_agent/skills_sync.py
strands_agent/system_prompt.py
strands_agent/six.py
strands_agent/typing_extensions.py
strands_agent/requirements.txt
strands_agent/README.md
strands_agent/.gitignore
strands_agent/<freshly installed dependencies>
```

powershell.exe -NoProfile -ExecutionPolicy Bypass `
    -File .\build_agentcore_bundle.ps1 `
    -OutputPath .\dist\strands_agent_v0.0.3.zip

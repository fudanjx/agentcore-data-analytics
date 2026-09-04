# AgentCore Runtime Application

This folder contains the container application deployed as an Amazon Bedrock AgentCore Runtime. It runs a Claude Agent SDK data analyst, streams its response as OpenAI-compatible server-sent events (SSE), and connects the agent to:

- AgentCore Memory for conversation history and long-term user context;
- optional project skills synchronized from S3;
- optional remote AgentCore Gateway MCP servers; and
- AgentCore Code Interpreter for Python, shell commands, uploaded-file analysis, and artifact generation.

The wider platform, proxy, data services, and deployment resources are documented in the [root README](../README.md) and [deployment guide](../docs/DEPLOY.md).

## Architecture

```text
Client or EKS proxy
        |
        | POST /invocations
        | messages + runtime session/user identity
        v
app/main.py
        |
        | extracts actor_id and session_id
        | returns an SSE StreamingResponse
        v
app/agent.py
        |
        +-- retrieve context ----------------> app/memory.py
        |                                      AgentCore Memory
        |
        +-- load project instructions --------> /app/.claude/skills
        |                                      optional S3 startup sync
        |
        +-- call remote MCP tools ------------> localhost:9000
        |                                      app/gateway_proxy.py
        |                                      SigV4 -> AgentCore Gateways
        |
        +-- call local MCP tools -------------> app/code_interpreter.py
        |                                      AgentCore Code Interpreter session
        |
        +-- stream text/tool status ----------> app/main.py -> client
        |
        +-- save completed turn --------------> app/memory.py
```

## Files in this folder

| File | Responsibility |
| --- | --- |
| `main.py` | FastAPI lifecycle, health endpoints, request parsing, identity extraction, and SSE formatting |
| `agent.py` | Claude Agent SDK configuration and agent loop; combines prompts, memory, skills, MCP, and Code Interpreter |
| `memory.py` | Retrieves current-session events and long-term records, then saves completed turns |
| `skills_sync.py` | Downloads complete skill packages from an optional S3 location into Claude's project skills directory |
| `gateway_config.py` | Loads and validates Gateway slugs, labels, HTTPS URLs, ARNs, and regions |
| `gateway_proxy.py` | Local HTTP proxy that signs outbound AgentCore Gateway requests with AWS SigV4 |
| `code_interpreter.py` | Starts/stops managed interpreter sessions and exposes them to Claude as MCP tools |

## Request lifecycle

1. AgentCore starts the ARM64 container with `uvicorn app.main:app` on port `8080`.
2. The FastAPI lifespan hook syncs skills and starts the local Gateway proxy only when those integrations are configured.
3. AgentCore checks `GET /ping` and forwards invocation bodies to `POST /invocations`.
4. `main.py` accepts either an OpenAI-style `messages` array or a simple `prompt`, `input`, or `inputText` field.
5. It obtains the conversation identity from AgentCore headers, with request-body fallbacks, and pads session IDs shorter than 33 characters.
6. `agent.stream()` retrieves memory, builds the prompt, starts a Code Interpreter session, registers all MCP servers, and starts `ClaudeSDKClient`.
7. Text deltas are returned immediately as OpenAI-compatible SSE chunks. Skill and tool lifecycle events are emitted as sanitized `agent_step` sideband events; raw SQL, tool inputs, file paths, and tool results are not exposed in these events.
8. In a `finally` block, the runtime stops the Code Interpreter session.
9. When an actor ID is present, the completed user/assistant turn is saved to AgentCore Memory before the stream finishes.

## HTTP contract

### Health checks

```text
GET /ping
GET /health
```

Both return:

```json
{"status": "ok"}
```

### Invocation

```http
POST /invocations
Content-Type: application/json
```

OpenAI-style body:

```json
{
  "model": "poc",
  "messages": [
    {"role": "system", "content": "Answer concisely."},
    {"role": "user", "content": "Summarize last month's admissions."}
  ]
}
```

Simple body:

```json
{"prompt": "Summarize last month's admissions."}
```

The response media type is always `text/event-stream`. Normal text uses OpenAI `chat.completion.chunk` objects, followed by `data: [DONE]`. A tool or skill status uses this sideband shape:

```text
data: {"event":"agent_step","step":{"type":"tool","name":"NUH: execute sql","status":"started"}}
```

The `/stream-test` endpoint, or an invocation body of `{"test":"stream"}`, bypasses the model and emits timed chunks for diagnosing transport buffering.

## Memory

Memory is added around the agent call in `agent.stream()`; it is not managed automatically by the Claude Agent SDK.

### Identity mapping

`main.py` resolves identity in this order:

| Value | Preferred source | Fallback |
| --- | --- | --- |
| `session_id` | `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` | body `chat_id`, then a generated UUID |
| `actor_id` | `X-Amzn-Bedrock-AgentCore-Runtime-User-Id` | body `model_item.info.user_id` |

Memory is skipped when `actor_id` is missing. A stable actor ID is therefore required for cross-session recall, and a stable session ID is required for within-session continuity.

### Short-term memory

`retrieve_short_term_context()` calls `list_events()` for the current actor and session. It:

- loads at most `MEMORY_MAX_SHORT_TERM_EVENTS` events (default `30`, maximum `100`);
- sorts service results into chronological order;
- keeps at most `MEMORY_MAX_SHORT_TERM_CONTEXT_CHARS` characters (default `40000`); and
- inserts the history into the user prompt as quoted conversation data, not system instructions.

### Long-term memory

The first memory-enabled request calls the AgentCore control plane to discover active `SEMANTIC`, `USER_PREFERENCE`, and `SUMMARIZATION` strategies. Their IDs are cached for the life of the container.

For every active strategy, `retrieve_long_term_context()` searches the actor namespace with the current prompt and requests the top five matching records. Deduplicated records are appended to the system prompt as contextual data.

### Saving memory

After a successful streamed response, `save_turn()` creates one event containing the current user message and assembled assistant response. Each side is limited to 8,000 characters. Retrieval and persistence failures are logged and treated as non-fatal, so a temporary Memory outage does not stop the answer.

### Adding or changing Memory

1. Create an AgentCore Memory resource with the desired long-term strategies.
2. Set `MEMORY_ID` and, if needed, `MEMORY_REGION` in the Runtime environment.
3. Grant the Runtime role the Memory actions and resource ARN listed in `infra/deploy.py`.
4. Ensure the caller supplies stable user and session identity.
5. Redeploy the Runtime. Strategy IDs do not need to be hard-coded; the app discovers them from the configured Memory resource.

## Skills

Claude Agent SDK project skills live at:

```text
/app/.claude/skills/<skill-name>/SKILL.md
```

When `SKILLS_BUCKET` is configured, the runtime sets `cwd="/app"`, `setting_sources=["project"]`, and `skills="all"`, so Claude discovers and selects synchronized skills during the agent loop. Without a bucket, it passes `skills=[]`, which explicitly prevents the Claude CLI from exposing any default skills.

At container startup, `skills_sync.sync_skills()` downloads every object beneath the configured S3 prefix into `/app/.claude/skills/`, preserving the package hierarchy. This includes `SKILL.md`, references, scripts, and assets. Per-object and total download limits guard the sync, and unsafe paths are rejected.

An empty or omitted `SKILLS_BUCKET` disables the integration. An empty `SKILLS_PREFIX` means skill directories are stored at the bucket root. S3 sync failures are logged and remain non-fatal.

### Adding a skill

Create a normal skill directory in the repository:

```text
.claude/skills/
  my-analysis-skill/
    SKILL.md
    references/
      definitions.md
```

Upload the complete package to the configured bucket and prefix:

```bash
aws s3 sync .claude/skills/ s3://YOUR_SKILLS_BUCKET/YOUR_SKILLS_PREFIX/
```

Restart or redeploy the Runtime after changing S3 content because sync runs only during container startup. The Runtime role requires `s3:ListBucket` on the bucket and `s3:GetObject` on the skills prefix.

## Remote MCP tools through AgentCore Gateway

The Claude SDK sends MCP HTTP traffic to local URLs such as:

```text
http://127.0.0.1:9000/nuh/mcp
```

`gateway_proxy.py` replaces the local prefix with the configured HTTPS Gateway URL, signs every request using the Runtime's AWS credentials and the `bedrock-agentcore` SigV4 service name, then forwards the Gateway response to the SDK.

This adapter is needed because Claude's HTTP MCP client does not sign AgentCore Gateway requests itself. Database credentials and SQL connections stay behind the Gateway targets; they are not stored in this Runtime.

Gateway configuration is supplied by `AGENTCORE_GATEWAYS_JSON`. Its shape is:

```json
{
  "analytics": {
    "label": "Analytics DB",
    "url": "https://gateway-id.gateway.bedrock-agentcore.ap-southeast-1.amazonaws.com",
    "arn": "arn:aws:bedrock-agentcore:ap-southeast-1:123456789012:gateway/gateway-id"
  }
}
```

Configuration is validated at import time. Each slug must be safe for a URL path, the endpoint must be HTTPS, and the hostname, Gateway ID, region, partition, and ARN must agree. Empty, unset, or `{}` configuration exposes no Gateway tools and does not start the local proxy.

### Adding an MCP Gateway

1. Deploy an AgentCore Gateway and at least one Gateway target that exposes MCP tools.
2. Add its slug, display label, base URL, and ARN to `AGENTCORE_GATEWAYS_JSON`.
3. Grant `bedrock-agentcore:InvokeGateway` on its ARN to the Runtime role.
4. Redeploy the Runtime.

`infra/deploy.py` uses the same validated configuration both to build the IAM resource list and to set the Runtime environment variable. Keeping the Gateway in that shared configuration therefore updates both sides together.

The agent creates MCP server entries dynamically from every configured slug. A new Gateway does not require another code change in `agent.py`.

## Code Interpreter

Code Interpreter is presented to Claude as a local SDK MCP server named `code_interpreter`. It provides two tools:

| Tool | AgentCore operation | Use |
| --- | --- | --- |
| `execute_code` | `executeCode` | Python, JavaScript, or TypeScript calculations and data analysis |
| `execute_command` | `executeCommand` | Shell commands, including approved file download/upload workflows |

For each invocation, the runtime:

1. starts a managed session using `CODE_INTERPRETER_ID`;
2. derives a readable session name from the Runtime session ID;
3. binds the two MCP tools to that exact managed session;
4. collects streamed interpreter results and returns them to Claude as tool content; and
5. stops the session even if the model or a tool fails.

Blocking boto3 calls run in worker threads so they do not block SSE delivery. Tool results are limited by `CODE_INTERPRETER_MAX_RESULT_CHARS` (default `200000`). Binary values are represented by their byte count rather than embedded in the model context.

### Adding or replacing Code Interpreter

1. Create a custom AgentCore Code Interpreter.
2. Set `CODE_INTERPRETER_ID` and, if needed, `CODE_INTERPRETER_REGION`.
3. Grant the Runtime role `StartCodeInterpreterSession`, `InvokeCodeInterpreter`, and `StopCodeInterpreterSession` on the interpreter ARN and its sessions.
4. If it must read uploads or publish generated files, grant those S3 permissions to the Code Interpreter's execution role as well.
5. Redeploy the Runtime.

To expose another interpreter operation, add a decorated tool in `build_mcp_server()`, map it to the corresponding `invoke_code_interpreter` operation name, and add its MCP name to `allowed_tools` in `_build_agent_options()`.

## Configuration

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `MODEL_ARN` | Project inference profile ARN | Bedrock model or inference profile used by Claude Agent SDK |
| `AGENTCORE_GATEWAYS_JSON` | Empty | Validated remote MCP Gateway mapping; empty, unset, or `{}` disables Gateway tools |
| `SKILLS_BUCKET` | Empty | S3 bucket containing Claude Agent Skill packages; empty disables skills |
| `SKILLS_PREFIX` | Empty | Optional S3 prefix; empty reads skill directories from the bucket root |
| `SKILLS_MAX_OBJECT_BYTES` | `50000000` | Maximum size of one downloaded skill object |
| `SKILLS_MAX_SYNC_BYTES` | `250000000` | Maximum combined bytes downloaded during startup |
| `MEMORY_ID` | Project Memory ID | AgentCore Memory resource |
| `MEMORY_REGION` | `ap-southeast-1` | Memory service region |
| `MEMORY_MAX_SHORT_TERM_EVENTS` | `30` | Maximum current-session events loaded per request |
| `MEMORY_MAX_SHORT_TERM_CONTEXT_CHARS` | `40000` | Maximum short-term history inserted into the prompt |
| `CODE_INTERPRETER_ID` | Project interpreter ID | Managed Code Interpreter resource |
| `CODE_INTERPRETER_REGION` | `AWS_DEFAULT_REGION` or `ap-southeast-1` | Interpreter service region |
| `CODE_INTERPRETER_SESSION_TIMEOUT_SECONDS` | `1800` | Managed session timeout, constrained to 60-28,800 seconds |
| `CODE_INTERPRETER_MAX_RESULT_CHARS` | `200000` | Maximum interpreter result text returned to the model |
| `CLAUDE_AGENT_MAX_BUFFER_BYTES` | `10485760` | Claude SDK receive buffer size |
| `AWS_DEFAULT_REGION` | Set by deployment | Default AWS region |
| `CLAUDE_CODE_USE_BEDROCK` | `1` | Makes the Claude SDK use Amazon Bedrock with IAM credentials |

The local skill directory remains `/app/.claude/skills` because it is coupled to the Claude project discovery path for `cwd="/app"`.

## IAM and networking

The Runtime role created by `infra/deploy.py` grants access to:

- the Bedrock model/inference profile;
- every configured AgentCore Gateway, when present;
- the configured Code Interpreter;
- the S3 skills bucket and prefix, when configured;
- the configured AgentCore Memory resource; and
- CloudWatch Logs, ECR, and VPC network-interface operations.

The Runtime is deployed in VPC mode. Its subnets, security group, DNS, routes, and VPC endpoints must allow access to Bedrock Runtime, AgentCore, AgentCore Gateway, S3, ECR, and logging services used by the container.

## Run and verify

Install dependencies and start the application from the repository root:

```bash
python -m pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

The process can start without S3 access because skill sync is best-effort, but real agent invocations require valid AWS credentials and access to the configured model, Gateways, Memory, and Code Interpreter.

Run the focused tests:

```bash
python -m pytest \
  tests/test_memory.py \
  tests/test_code_interpreter.py \
  tests/test_gateway_config.py \
  tests/test_runtime_status_stream.py
```

Build and deploy the AgentCore Runtime:

```bash
bash infra/build_and_push.sh
ECR_IMAGE_URI=<account>.dkr.ecr.ap-southeast-1.amazonaws.com/agentcore-poc:latest \
  python infra/deploy.py
```

The Docker image must be `linux/arm64`, listens on port `8080`, and runs as the unprivileged `appuser` account.

## Failure behavior

| Failure | Behavior |
| --- | --- |
| S3 skill sync fails | Warning is logged; bundled skills remain usable |
| Memory lookup or save fails | Warning is logged; the answer continues without that memory operation |
| Gateway signing fails | Local MCP proxy returns HTTP 500 |
| Gateway call fails | Local MCP proxy returns HTTP 502 |
| Code Interpreter tool fails | A tool error is returned to the agent |
| Agent stream fails | An SSE error message is emitted, followed by `[DONE]` |
| Code Interpreter stop fails | Warning is logged; it does not replace the agent response |

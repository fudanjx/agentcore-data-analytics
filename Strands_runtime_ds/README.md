# Strands Runtime DeepSeek

This project is a separate replica of the live `Strands_runtime` AgentCore
application. It preserves the same request contract, actor/session isolation,
AgentCore Memory, Gateway MCP tools, S3-hosted Agent Skills, managed Code
Interpreter, bounded interpreter results, tool-event streaming, heartbeat, and
usage telemetry. Only the model transport changes.

## Model transport

- Provider: DeepSeek official API
- Model: `deepseek-v4-flash`
- Interface: OpenAI-compatible Responses API
- Base URL: `https://api.deepseek.com`
- Thinking: enabled, with environment-controlled reasoning effort
- Server-side response state: disabled; AgentCore Memory remains the durable
  conversation store

The DeepSeek Responses API is stateless. Every model turn therefore contains
the system prompt and conversation/tool context required for that turn. The
adapter preserves DeepSeek reasoning input items between function calls so a
multi-tool agent loop can continue coherently. Reasoning text is not copied to
the Runtime's OpenAI-compatible visible text stream or CloudWatch logs.

## Required model environment variables

```text
DEEPSEEK_API_KEY=PENDING_REPLACE_ME
DEEPSEEK_BASE_URL=https://api.deepseek.com
MODEL_ID=deepseek-v4-flash
DEEPSEEK_REASONING_EFFORT=high
DEEPSEEK_MAX_OUTPUT_TOKENS=32768
MODEL_CONNECT_TIMEOUT_SECONDS=10
MODEL_READ_TIMEOUT_SECONDS=900
MODEL_RETRY_MAX_ATTEMPTS=2
MODEL_PRICING_LABEL=deepseek-v4-flash-official-2026-08
```

`DEEPSEEK_REASONING_EFFORT` accepts `low`, `high`, or `max`.
`DEEPSEEK_MAX_OUTPUT_TOKENS` accepts 1,024 through 384,000 and includes both
reasoning and visible output tokens for each model call.

`PENDING_REPLACE_ME` is intentionally rejected before Memory, Gateway, or Code
Interpreter resources are acquired. Replace it in the AgentCore Runtime console
before a real invocation. Never add a real API key to this repository, a ZIP
bundle, test output, or CloudWatch logs.

Do not configure `MODEL_ARN`, `MODEL_REGION`, `ENABLE_PROMPT_CACHE`, or
`PROMPT_CACHE_TTL`. This project does not call Bedrock Runtime. DeepSeek context
caching is automatic; the Responses API exposes no caching toggle or explicit
cache key.

## Inherited live configuration

The deployed runtime copies these settings from `Strands_runtime`:

- Runtime role: `agentcore-poc-runtime-role`
- VPC subnets and security group
- HTTP protocol, 3,600-second idle session timeout, and 28,800-second lifetime
- AgentCore Gateways configured in `AGENTCORE_GATEWAYS_JSON`
- Code Interpreter ID and result limits
- AgentCore Memory ID `harness_harness_e52fs_8d3d-vtE3DJC9ia`
- Skills bucket `agentcore-harness-dev` and prefix `skills/`
- Tool detail limit and streaming behavior

The private Runtime subnets need working NAT egress for
`https://api.deepseek.com`. AWS-native Gateway, Memory, Code Interpreter, S3,
and CloudWatch traffic continues to use the configured AWS network paths.

## External processing boundary

Prompts, the system prompt, selected memory context, and bounded tool results
used by the model leave AWS and are sent to DeepSeek. Database services, full
query result sets retained inside Code Interpreter, generated artifacts, and
AWS service resources remain in AWS unless their contents are explicitly added
to model context by the agent workflow.

## Usage telemetry

One content-free `MODEL_USAGE` record is emitted per invocation when
`ENABLE_MODEL_USAGE_LOGS=true`. The DeepSeek adapter maps:

- uncached input to `input_tokens`;
- `input_tokens_details.cached_tokens` to `cache_read_input_tokens`;
- output, including reasoning, to `output_tokens`;
- `output_tokens_details.reasoning_tokens` to `reasoning_tokens`;
- provider totals to `total_tokens_reported`.

DeepSeek does not report cache-write tokens, so `cache_write_input_tokens`
remains zero. Telemetry does not contain prompts, output text, reasoning text,
tool-result contents, or the API key.

## Test

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 -m py_compile *.py
```

## Build

Docker Desktop must be running. The build targets AgentCore's Linux ARM64 and
Python 3.13 source deployment format:

```bash
./build_agentcore_bundle.sh ./dist/strands_runtime_ds_v0.0.1.zip
```

The resulting entry point is `strands_agent/main.py`. The ZIP includes runtime
dependencies but excludes tests, local bytecode, credentials, and unrelated
project files.

## Deployment boundary

Deploy this bundle only to the separate `Strands_runtime_ds` Runtime. Do not
update `Strands_runtime-mk6uFHBu9d`, and do not add an OpenWebUI or Dify proxy
route as part of this project.

## Current deployed instance

```text
Runtime name: Strands_runtime_ds
Runtime ID: Strands_runtime_ds-1HiZXr53Kp
Runtime ARN: arn:aws:bedrock-agentcore:ap-southeast-1:964340114883:runtime/Strands_runtime_ds-1HiZXr53Kp
Runtime version: 1
Artifact: s3://bedrock-agentcore-runtime-964340114883-ap-southeast-1-7a3qgyspw/strands_runtime_ds_v0.0.1.zip
Artifact SHA-256: 588f77b4bd6828b41f230f956f483f4d2ffc9ff088fdf1b55a5bf6ae93799056
```

Version 1 is intentionally deployed with `DEEPSEEK_API_KEY=PENDING_REPLACE_ME`.
It reaches `READY`, but invocations are rejected locally until the environment
variable is replaced with a valid key and AgentCore finishes publishing the
resulting new Runtime version.

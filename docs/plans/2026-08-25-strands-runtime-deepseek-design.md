# Strands Runtime DeepSeek Design

## Goal

Create a separate Amazon Bedrock AgentCore Runtime named `Strands_runtime_ds`
that preserves the live `Strands_runtime` agent's tools, memory, request
contract, streaming behavior, and operational limits while replacing the
Bedrock model transport with DeepSeek's official Responses API and model
`deepseek-v4-flash`.

## Boundaries

- Work is isolated on branch `strands-runtime-ds` in a new
  `Strands_runtime_ds/` project.
- The existing `Strands_runtime-mk6uFHBu9d` runtime is not updated.
- No OpenWebUI or Dify proxy route is added or changed.
- The DeepSeek API key is supplied directly through the new Runtime's
  `DEEPSEEK_API_KEY` environment variable. The initial deployed value is the
  inert placeholder `PENDING_REPLACE_ME`.
- No data restriction is enforced in code or the system prompt. The deployment
  documentation records that model inputs and bounded tool results leave AWS
  for DeepSeek's API.

## Runtime architecture

The new project starts from the current `Strands-runtime` source and retains:

- the AgentCore Runtime HTTP entry point and OpenAI-compatible SSE contract;
- actor/session parsing and shared AgentCore Memory ID
  `harness_harness_e52fs_8d3d-vtE3DJC9ia`;
- the NUH, AH, TimesFM, and S3 Tables AgentCore Gateways;
- managed Code Interpreter and its bounded result contract;
- S3-hosted skills and activation behavior;
- tool lifecycle events, stream heartbeats, and cleanup behavior;
- the live Runtime IAM role, VPC subnets, security group, and lifecycle values.

The model layer uses Strands' `OpenAIResponsesModel` against
`https://api.deepseek.com` with server-side response state disabled. AgentCore
Memory remains the only durable conversation store. DeepSeek receives the
complete request context required for each stateless model turn.

## Configuration

Provider-specific Runtime environment variables are:

```text
DEEPSEEK_API_KEY=PENDING_REPLACE_ME
DEEPSEEK_BASE_URL=https://api.deepseek.com
MODEL_ID=deepseek-v4-flash
DEEPSEEK_REASONING_EFFORT=high
DEEPSEEK_MAX_OUTPUT_TOKENS=32768
```

`DEEPSEEK_REASONING_EFFORT` accepts `low`, `high`, or `max`.
`DEEPSEEK_MAX_OUTPUT_TOKENS` is bounded and applies per model response,
including reasoning tokens. Existing connection/read timeouts, retry count,
and Runtime heartbeat configuration remain environment-controlled.

Bedrock-only model variables are omitted: `MODEL_ARN`, `MODEL_REGION`,
`ENABLE_PROMPT_CACHE`, and `PROMPT_CACHE_TTL`. DeepSeek context caching is
automatic and cannot be enabled or disabled through the Responses API.

## Usage telemetry

The adapter normalizes DeepSeek usage into the existing `MODEL_USAGE` contract:

- `inputTokens`: input tokens not served from cache;
- `cacheReadInputTokens`: `input_tokens_details.cached_tokens`;
- `cacheWriteInputTokens`: zero because DeepSeek does not report cache writes;
- `outputTokens`: all output tokens;
- `reasoningTokens`: `output_tokens_details.reasoning_tokens`;
- `totalTokens`: the provider-reported total.

The CloudWatch record contains counts and identifiers only. It must not log the
API key, prompt, reasoning text, response text, or tool-result contents.

## Failure handling

The model is validated before Gateway clients, Memory, or Code Interpreter
sessions start. A missing, blank, or placeholder API key produces a clear local
configuration error and never calls DeepSeek. Provider timeouts, rate limits,
stream failures, and tool errors flow through the existing Runtime error/SSE
contract. All acquired resources are cleaned up on success, failure,
cancellation, or timeout.

## Delivery and verification

Build `strands_runtime_ds_v0.0.1.zip`, upload it to
`s3://bedrock-agentcore-runtime-964340114883-ap-southeast-1-7a3qgyspw/`, and
create `Strands_runtime_ds` using the cloned live infrastructure settings.
Verify unit and regression tests, bundle contents, Runtime `READY` state, and a
safe placeholder-key invocation. A real end-to-end model/tool test is deferred
until the user replaces the placeholder with a valid DeepSeek API key.

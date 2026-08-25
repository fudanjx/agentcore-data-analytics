# Strands Runtime GPT

This is a separate replica of the live `Strands_runtime` v0.0.8 application.
It retains the same AgentCore Memory, Gateway MCP tools, Agent Skills, managed
Code Interpreter, request contract, tool-event streaming, and bounded telemetry.

The only functional replacement is the model transport:

- Model: `openai.gpt-5.6-luna`
- Endpoint: Bedrock Mantle Responses API in `us-east-1`
- Project: `Strands_runtime_gpt` (`proj_ftf4uxf7ldupffxdodeo`)
- Memory: the existing configured AgentCore Memory resource, so user/session
  history is shared with the Claude runtime.
- Mantle server-side response state: disabled. AgentCore Memory remains the
  only conversation-memory system.

## Luna prompt caching

Luna does not support the Bedrock Converse cache configuration used by the
Claude runtime. `bedrock_mantle_openai.py` sends the stable developer prompt as
the first Responses input item and marks it with Luna's explicit cache point.
User messages, restored memory, tool inputs, and tool results are not marked as
cacheable. Mantle uses its fixed 30-minute explicit-cache TTL.

`MODEL_USAGE` normalizes Mantle's inclusive `input_tokens` into the Runtime's
existing non-cached input, cache-read, and cache-write fields before estimating
costs. This prevents cache tokens from being charged twice in CloudWatch logs.

## Required environment variables

```text
MODEL_PROVIDER=bedrock_mantle_openai
MODEL_ID=openai.gpt-5.6-luna
MODEL_REGION=us-east-1
OPENAI_PROJECT_ID=proj_ftf4uxf7ldupffxdodeo
ENABLE_PROMPT_CACHE=true
PROMPT_CACHE_TTL=30m
MODEL_PRICING_LABEL=openai-gpt-5.6-luna-standard-2026-08
MODEL_INPUT_PRICE_PER_MTOK_USD=0.44
MODEL_OUTPUT_PRICE_PER_MTOK_USD=1.98
MODEL_CACHE_READ_PRICE_PER_MTOK_USD=0.044
MODEL_CACHE_WRITE_PRICE_PER_MTOK_USD=0.55
```

All remaining environment variables are copied from the active
`Strands_runtime` configuration, including `MEMORY_ID`, the gateways, skills,
and Code Interpreter settings. Do not set the Singapore application inference
profile here: GPT-5.6 Luna requires the Mantle Responses API, not Converse.

## Bounded S3 Tables exports (GPT pilot)

The shared S3 Tables Gateway continues to return small SQL results directly,
but it now fails closed when a result exceeds 1,000 rows. It also exposes
`s3tables_execute_sql_export`, which runs the same read-only AH/NUH query and
returns only the Athena result CSV URI plus compact execution metadata.

This runtime alone has a stable instruction to use the export operation for
large or multi-month dashboard and mapping work. Code Interpreter downloads the
exact returned CSV, performs mapping/validation/aggregation locally, and sends
only its bounded `AGENTCORE_RESULT_JSON` back to the model. The Claude runtime
receives the safe Gateway limit but no automatic export-use instruction yet.

The Code Interpreter execution role must have only this additional permission:

```text
s3:GetObject on arn:aws:s3:::agentcore-tmp-964340114883/athena-results/*
```

It does not need `s3:ListBucket`, write access to that bucket, or Athena API
permissions.

## Build

With Docker Desktop running:

```bash
./build_agentcore_bundle.sh ./dist/strands_runtime_gpt_v0.0.3.zip
```

The script creates a Linux ARM64/Python 3.13 bundle with entry point
`strands_agent/main.py`.

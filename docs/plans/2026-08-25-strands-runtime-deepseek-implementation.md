# Strands Runtime DeepSeek Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build and deploy an isolated AgentCore replica that uses DeepSeek V4 Flash through the official Responses API while preserving the existing Strands runtime's tools, memory, streaming, and telemetry contracts.

**Architecture:** Copy the current `Strands-runtime` application into a new `Strands_runtime_ds` project, replace only the Bedrock model factory with a small DeepSeek Responses adapter, and normalize DeepSeek usage into the existing Runtime telemetry schema. Deploy a separate S3 source bundle and Runtime that clone the live IAM, VPC, environment, and lifecycle configuration without modifying the source Runtime or proxies.

**Tech Stack:** Python 3.13, Strands Agents 1.50.2, OpenAI Python SDK 2.x, Amazon Bedrock AgentCore Runtime, AgentCore Memory/Gateway/Code Interpreter, Docker ARM64 bundle build, boto3/AWS CLI.

---

### Task 1: Freeze the approved design and source baseline

**Files:**
- Create: `docs/plans/2026-08-25-strands-runtime-deepseek-design.md`
- Create: `docs/plans/2026-08-25-strands-runtime-deepseek-implementation.md`
- Inspect: `Strands-runtime/`

**Steps:**
1. Confirm branch `strands-runtime-ds` is based on synchronized `main` and the worktree has no unrelated changes.
2. Record the live source Runtime's complete configuration and latest artifact read-only.
3. Compare the local `Strands-runtime` source contract with the deployed artifact before copying it.
4. Verify the design excludes proxy changes and changes to `Strands_runtime-mk6uFHBu9d`.

### Task 2: Create failing DeepSeek configuration and usage tests

**Files:**
- Create: `Strands_runtime_ds/tests/test_deepseek_model.py`
- Create: `Strands_runtime_ds/tests/test_agent_deepseek_config.py`

**Steps:**
1. Add tests for missing and placeholder API keys.
2. Add tests for accepted/rejected reasoning effort values.
3. Add tests for bounded maximum output tokens.
4. Add tests that the Responses request contains the configured reasoning effort and output-token limit.
5. Add tests mapping inclusive input, cached input, output, and reasoning counts into the established Strands usage schema.
6. Run the focused tests and confirm they fail because the DeepSeek adapter does not exist.

### Task 3: Implement the DeepSeek model adapter

**Files:**
- Create: `Strands_runtime_ds/deepseek_openai.py`
- Modify: `Strands_runtime_ds/agent.py`
- Modify: `Strands_runtime_ds/requirements.txt`

**Steps:**
1. Implement a thin `OpenAIResponsesModel` subclass for DeepSeek request and usage normalization.
2. Add validated environment configuration for base URL, API key, reasoning effort, and maximum output tokens.
3. Build an `httpx` timeout and retry configuration from the inherited Runtime settings.
4. Validate the API key before acquiring any managed tool or Memory resource.
5. Replace `BedrockModel` construction with the stateless DeepSeek model factory.
6. Remove Bedrock-specific cache and Region logic from the new project only.
7. Run focused tests until they pass.

### Task 4: Preserve and verify inherited runtime behavior

**Files:**
- Copy/modify: `Strands_runtime_ds/main.py`
- Copy: `Strands_runtime_ds/code_interpreter.py`
- Copy: `Strands_runtime_ds/code_interpreter_result.py`
- Copy: `Strands_runtime_ds/gateway_config.py`
- Copy: `Strands_runtime_ds/gateway_proxy.py`
- Copy: `Strands_runtime_ds/memory.py`
- Copy: `Strands_runtime_ds/skills_sync.py`
- Copy: `Strands_runtime_ds/system_prompt.py`
- Copy: relevant `Strands-runtime/tests/`

**Steps:**
1. Retain request parsing, actor/session identity, shared Memory, tools, status events, heartbeat, and cleanup logic.
2. Run the inherited Code Interpreter contract tests.
3. Run request/streaming regression tests with provider calls mocked.
4. Run `py_compile`, shell syntax, and `git diff --check` validation.

### Task 5: Document and build the source bundle

**Files:**
- Create: `Strands_runtime_ds/README.md`
- Create: `Strands_runtime_ds/build_agentcore_bundle.sh`
- Create: `Strands_runtime_ds/dist/strands_runtime_ds_v0.0.1.zip`

**Steps:**
1. Document configuration, security boundary, automatic cache behavior, usage fields, build, deployment, and key-replacement procedure.
2. Build a Linux ARM64/Python 3.13 S3 source bundle using Docker.
3. Verify required source modules and dependencies are present at `strands_agent/` and no tests, bytecode, local credentials, or unrelated artifacts are packaged.
4. Import the packaged application and run tests inside a matching container.

### Task 6: Upload and create the separate AgentCore Runtime

**Files:**
- Upload: `s3://bedrock-agentcore-runtime-964340114883-ap-southeast-1-7a3qgyspw/strands_runtime_ds_v0.0.1.zip`
- Create: AgentCore Runtime `Strands_runtime_ds`

**Steps:**
1. Capture the existing Runtime configuration and latest source artifact again immediately before deployment.
2. Upload the verified bundle and record its S3 version/ETag.
3. Create a new Runtime with the cloned role, VPC configuration, HTTP protocol, one-hour idle timeout, and eight-hour maximum lifetime.
4. Copy non-Bedrock environment values; add DeepSeek variables and omit Bedrock model/cache variables.
5. Explicitly confirm `Strands_runtime-mk6uFHBu9d` and proxy resources were not updated.

### Task 7: Verify deployment safely

**Steps:**
1. Wait for `Strands_runtime_ds` to reach `READY` and record its ARN/version.
2. Invoke it with a unique 33+ character session ID while the placeholder is present.
3. Verify the response is the expected local configuration error and no outbound DeepSeek request is attempted.
4. Inspect CloudWatch for secret-free startup/error logs.
5. Re-read both Runtime configurations and compare shared tools, Memory, VPC, role, and lifecycle settings.
6. Report that live model/tool validation remains pending until the user supplies the key.

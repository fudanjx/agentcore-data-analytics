# Strands Bedrock Timeout and Context Limits Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Prevent long Strands analytics workflows from failing at the Bedrock model-client read timeout while bounding retained tool output.

**Architecture:** Configure the Strands `BedrockModel` with a 15-minute Botocore read timeout, a short connection timeout, and standard retries. Bound Code Interpreter results and frontend tool details through deployment-controlled environment variables so the model receives compact results on later turns.

**Tech Stack:** Python, Strands Agents, Botocore, AgentCore Runtime, pytest.

---

### Task 1: Make Bedrock model networking configurable

**Files:**
- Modify: `Strands-runtime/agent.py`
- Test: `tests/test_strands_optional_runtime_config.py`

**Step 1:** Add a failing test that asserts the defaults are 900-second read timeout, 10-second connection timeout, and two standard retries.

**Step 2:** Add bounded environment parsing and pass a Botocore `Config` to `BedrockModel`.

**Step 3:** Run the targeted test.

### Task 2: Bound retained tool output

**Files:**
- Modify: `Strands-runtime/agent.py`
- Modify: `Strands-runtime/code_interpreter.py`
- Modify: `infra/strands_runtime_deploy.py`
- Test: `tests/test_strands_code_interpreter.py`

**Step 1:** Add failing tests for deployed output limits.

**Step 2:** Configure `TOOL_DETAIL_MAX_CHARS=12000` and `CODE_INTERPRETER_MAX_RESULT_CHARS=30000` on the Strands runtime.

**Step 3:** Run targeted tests.

### Task 3: Package, deploy, and verify

**Files:**
- Build: `Strands-runtime/dist/strands_agent_v0.0.6.zip`

**Step 1:** Build the Linux AgentCore bundle and validate its contents.

**Step 2:** Update only `Strands_runtime-mk6uFHBu9d`; wait for `READY`.

**Step 3:** Verify its live environment, archive, and a small proxy request.

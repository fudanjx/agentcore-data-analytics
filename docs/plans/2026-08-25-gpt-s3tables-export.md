# GPT S3 Tables Export Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Prevent large S3 Tables query results from being silently truncated or placed in model context, initially for `Strands_runtime_gpt` only.

**Architecture:** The shared S3 Tables Gateway Lambda will fail closed when direct SQL results exceed its safe row limit and will expose a metadata-only export operation backed by Athena's existing result location. GPT receives a runtime-local instruction to use that export for large, multi-month analytics; it downloads the resulting CSV in Code Interpreter and returns only a compact result contract. Claude/Strands receives the safe Gateway behavior but no new automatic-use instruction.

**Tech Stack:** Python 3.12, AWS Lambda, Amazon Athena, Amazon S3, Amazon Bedrock AgentCore Gateway/Runtime, pytest.

---

### Task 1: Capture the deployed dual-source Lambda contract

**Files:**
- Modify: `mcp_lambda_s3tables/handler.py`
- Modify: `mcp_lambda_s3tables/deploy.py`

**Step 1:** Compare the checked-in AH-only Lambda with the live dual-source Lambda.

**Step 2:** Port the live AH/NUH source selection and environment handling without changing the existing query semantics.

**Step 3:** Update the checked-in Gateway tool schema to describe the two sources.

### Task 2: Fail closed and add an export operation

**Files:**
- Modify: `mcp_lambda_s3tables/handler.py`
- Create: `mcp_lambda_s3tables/tests/test_handler.py`

**Step 1:** Write tests showing that a direct result at the limit is rejected rather than returned partially.

**Step 2:** Add a dedicated `execute_sql_export` operation which returns Athena query metadata and its exact S3 CSV URI, never raw query rows.

**Step 3:** Test small direct results, oversized direct results, and export metadata for both source settings.

### Task 3: Make deployment safe for an existing shared target

**Files:**
- Modify: `mcp_lambda_s3tables/deploy.py`
- Create: `mcp_lambda_s3tables/deploy_gpt_export.py`

**Step 1:** Add the export tool to the Gateway target schema.

**Step 2:** Implement an explicit update path for the existing Lambda/target that preserves live dual-source environment variables and credentials.

**Step 3:** Grant the Code Interpreter role read-only access only to `agentcore-tmp-964340114883/athena-results/*`.

### Task 4: Add GPT-only workflow guidance

**Files:**
- Modify: `Strands_runtime_gpt/agent.py`
- Modify: `Strands_runtime_gpt/README.md`

**Step 1:** Add a stable GPT runtime instruction to export large/multi-month dashboard queries instead of placing rows in the model context.

**Step 2:** Direct Code Interpreter to download the exact returned URI, perform mappings/validation locally, and return compact `AGENTCORE_RESULT_JSON` only.

**Step 3:** Document the bounded-context behavior and minimum S3 permission.

### Task 5: Verify and deploy the GPT pilot

**Files:**
- Build: `Strands_runtime_gpt/dist/strands_runtime_gpt_v0.0.3.zip`

**Step 1:** Run the focused Lambda tests and Python syntax checks.

**Step 2:** Deploy the Lambda, existing Gateway target schema, and narrowly scoped Code Interpreter policy.

**Step 3:** Build/upload GPT runtime v0.0.3 and update `Strands_runtime_gpt-zMvd2XDyLM` only.

**Step 4:** Confirm the export operation returns metadata rather than rows and confirm the Claude runtime configuration/artifact remains unchanged.

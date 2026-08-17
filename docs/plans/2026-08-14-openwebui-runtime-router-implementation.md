# OpenWebUI Runtime Router Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Convert `agentcore-proxy` into a configuration-driven OpenWebUI-only router for three AgentCore runtimes while preserving secure file isolation and adding HTML downloads and direct tool lifecycle statuses.

**Architecture:** A validated JSON registry maps public slugs to Runtime or Harness ARNs and display names. Generic OpenAI-compatible handlers apply one trusted OpenWebUI identity/file policy before the configured AgentCore invocation; a generic artifact endpoint validates user-owned outputs for every configured slug. The v0.10.2 OpenWebUI filter selects the matching registration endpoint and turns private stream markers into native status events and authenticated file links.

**Tech Stack:** Python 3.12, FastAPI, boto3 AgentCore/S3 clients, OpenWebUI filter functions, Kubernetes ConfigMap/Deployment, pytest.

---

### Task 1: Capture config-driven routing behavior

**Files:**
- Modify: `tests/test_proxy_openwebui_context.py`
- Modify: `tests/test_runtime_status_stream.py`

**Steps:**

1. Add tests that load a three-entry runtime registry and assert canonical model discovery.
2. Add tests for root and `insights` compatibility aliases.
3. Assert all three canonical chat routes reject missing trusted user/chat headers.
4. Assert an unknown slug and removed Dify endpoints return `404`.
5. Run the focused tests and confirm the new expectations fail before implementation.

### Task 2: Implement the runtime registry and remove main-proxy Dify support

**Files:**
- Modify: `proxy/server.py`
- Modify: `proxy/k8s/deployment.yaml`
- Create: `proxy/k8s/runtime-routes-configmap.yaml`

**Steps:**

1. Parse and validate `AGENTCORE_RUNTIME_ROUTES_JSON` into immutable route records.
2. Replace the hard-coded runtime/harness maps with the runtime registry and explicit compatibility aliases.
3. Route each chat request through its configured `invoke_agent_runtime` or
   `invoke_harness` path with trusted OpenWebUI actor/session context.
4. Remove native Dify endpoints, translation helpers, Dify storage settings, and Dify-specific artifact handling from `proxy/server.py`.
5. Put the approved runtime mapping into a ConfigMap and inject it into the Deployment.
6. Remove obsolete Dify and Harness environment variables from the Deployment.
7. Run focused proxy tests.

### Task 3: Generalize secure artifacts and add HTML

**Files:**
- Modify: `proxy/server.py`
- Modify: `tests/test_proxy_openwebui_context.py`

**Steps:**

1. Add `.html` to the generated-output allowlist.
2. Replace the Office-only registration decorator with `POST /{slug}/v1/artifacts/register`.
3. Require a configured canonical or compatibility slug and trusted user/chat headers.
4. Reuse existing bucket, prefix, ownership-tag, size, and metadata validation for every runtime.
5. Confirm HTML succeeds only under the exact requesting user/chat prefix and cross-user registration fails.
6. Confirm artifact links retain `attachment=true`.

### Task 4: Stream tool lifecycle events independently

**Files:**
- Modify: `proxy/server.py`
- Modify: `tests/test_runtime_status_stream.py`

**Steps:**

1. Replace active-tool grouping with a stateless translation of each structured lifecycle event.
2. Preserve event order and emit a separate status marker for start, completion, or failure.
3. Remove `Preparing final answer` and other invented fallback progress.
4. Redact tool inputs and raw results from status descriptions.
5. Ensure response termination emits the UI-closing status marker.
6. Run streaming tests for multiple sequential and overlapping tools.

### Task 5: Generalize the OpenWebUI v0.10.2 integration

**Files:**
- Modify: `openwebui-insights/functions/agentcore_file_context.py`
- Modify: `openwebui-insights/compose-service.yml`
- Modify: `openwebui-insights/deploy_office_provider.py`
- Modify: `tests/test_openwebui_filter.py`
- Modify: `tests/test_openwebui_office_stream.py`

**Steps:**

1. Recognize canonical and compatibility model IDs while excluding unrelated providers.
2. Derive the canonical slug from prefixed OpenWebUI model IDs.
3. Resolve `/{slug}/v1/artifacts/register` without accepting an arbitrary URL from model output.
4. Apply status and artifact marker handling to all three canonical agents.
5. Configure three visible provider connections and keep `insights` only as a hidden compatibility path.
6. Run filter and stream tests.

### Task 6: Update documentation and verify regressions

**Files:**
- Modify: `README.md`
- Modify: `docs/DEPLOY.md`
- Modify: `outstanding_task.md`
- Modify: relevant proxy/OpenWebUI READMEs discovered during implementation

**Steps:**

1. Document the runtime registry, exact routes, identity contract, file flow, HTML attachment policy, and Dify removal boundary.
2. Document ConfigMap edit/apply/rollout steps for changing agents.
3. Remove stale statements that describe Dify as a capability of `agentcore-proxy`.
4. Run focused tests, then `pytest -q tests` in the project test environment.
5. Inspect `git diff --check`, `git diff --stat`, and the final diff while preserving unrelated user changes.
6. Report the implementation and verification results; do not build, deploy, commit, or push unless separately requested.

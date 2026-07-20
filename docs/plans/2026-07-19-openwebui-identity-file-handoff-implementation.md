# OpenWebUI Identity and S3 File Handoff Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Forward stable OpenWebUI user/chat identity and actor-owned S3 file manifests through the proxy to AgentCore without OpenWebUI RAG duplication.

**Architecture:** A model-scoped OpenWebUI filter builds a chat-wide `agentcore_files` manifest from OpenWebUI's own database. The proxy derives namespaced AgentCore identity from trusted headers, validates every S3 object using metadata-only AWS permissions, and injects a system-context manifest before invoking the harness.

**Tech Stack:** Python 3.12, FastAPI, boto3, OpenWebUI filter functions, SQLite/SQLAlchemy, Docker Compose, Kubernetes/EKS, Caddy, `unittest`.

---

### Task 1: OpenWebUI identity contract

**Files:**
- Create: `tests/test_proxy_openwebui_context.py`
- Modify: `proxy/server.py`

**Step 1: Write the failing HTTP tests**

Use FastAPI `TestClient` against `POST /harness/v1/chat/completions`.

Cover:

```python
def test_openwebui_headers_become_namespaced_actor_and_session():
    # X-OpenWebUI-User-Id=user-1
    # X-OpenWebUI-Chat-Id=chat-1...
    # captured completion call receives:
    # actor = openwebui:user-1
    # session = owui-user-1-chat-1...

def test_missing_openwebui_user_header_is_rejected():
    # expect 400 identity_context_required

def test_missing_openwebui_chat_header_is_rejected():
    # expect 400 identity_context_required
```

Patch `_build_completion` at the proxy boundary so the public route is tested
without calling AWS.

**Step 2: Run the tests and verify RED**

Run:

```bash
python3 -m unittest tests.test_proxy_openwebui_context -v
```

Expected: identity tests fail because the route currently reads body fields and
mints a random session.

**Step 3: Implement the minimum identity extraction**

Add a helper that reads the two OpenWebUI headers, validates they are non-empty,
and returns:

```python
actor_id = f"openwebui:{user_id}"
session_id = f"owui-{user_id}-{chat_id}"
```

Validate the session against AgentCore's 33–100 character and
`[A-Za-z0-9][A-Za-z0-9-_]*` constraints. Return
`400 identity_context_required` for missing values.

Apply this contract only to the `harness` slug in this phase. Leave Dify and
other compatibility routes unchanged.

**Step 4: Run the tests and verify GREEN**

Run the same `unittest` command. Expected: all identity tests pass.

### Task 2: Proxy file manifest validation and system context

**Files:**
- Modify: `tests/test_proxy_openwebui_context.py`
- Modify: `proxy/server.py`
- Modify: `proxy/k8s/deployment.yaml`

**Step 1: Add one failing file-validation test**

Send:

```json
{
  "model": "harness",
  "messages": [{"role": "user", "content": "Analyze the file"}],
  "agentcore_files": [{
    "file_id": "file-1",
    "s3_uri": "s3://agentcore-openwebui-test-964340114883/openwebui-test/file-1_costs.csv",
    "filename": "costs.csv",
    "mime_type": "text/csv",
    "size": 1945
  }]
}
```

Use a fake S3 client that returns object size and tags. Assert the captured
messages contain a system message with the validated URI and conditional Code
Interpreter instructions.

**Step 2: Run the single test and verify RED**

Expected: the manifest is ignored.

**Step 3: Implement the first valid-file path**

Add:

- `OPENWEBUI_UPLOADS_BUCKET`
- `OPENWEBUI_UPLOADS_PREFIX`
- `MAX_FILES_PER_CHAT = 10`
- `MAX_CHAT_UPLOAD_BYTES = 200 MiB`

Parse each `s3://` URI with `urllib.parse`. Use a prefix-restricted listing for
authoritative existence/size without granting object-content reads:

```python
s3.list_objects_v2(
    Bucket=bucket,
    Prefix=key,
    MaxKeys=1,
)
s3.get_object_tagging(Bucket=bucket, Key=key)
```

Verify bucket, prefix, object size, `OpenWebUI-User-Id`, and
`OpenWebUI-File-Id`.

**Step 4: Run the test and verify GREEN**

**Step 5: Add failure cases one at a time**

For each behavior, add one test, observe RED, implement the minimum change, and
observe GREEN:

- wrong owner tag → `403 file_not_accessible`
- wrong file ID tag → `403 file_not_accessible`
- wrong bucket/prefix → `403 file_not_accessible`
- unsupported extension → `400 invalid_file_manifest`
- malformed manifest → `400 invalid_file_manifest`
- over 50 MiB → `413 file_limit_exceeded`
- over 10 files → `400 too_many_files`
- over 200 MiB combined → `413 file_limit_exceeded`
- AWS metadata failure → `502 file_validation_failed`
- one bad file prevents the AgentCore call
- text-only requests remain unchanged

Do not log the complete URI or manifest.

**Step 6: Configure the deployment**

Add explicit bucket and prefix environment variables to
`proxy/k8s/deployment.yaml`.

**Step 7: Run the complete proxy test module**

Expected: all tests pass.

### Task 3: OpenWebUI chat-wide manifest filter

**Files:**
- Create: `openwebui-local/functions/agentcore_file_context.py`
- Create: `tests/test_openwebui_filter.py`

**Step 1: Write the failing filter test**

Load the filter module with stub OpenWebUI database modules. Call its public
`Filter.inlet()` method and verify:

- `harness` and `agentcore.harness` requests get `agentcore_files`
- a non-AgentCore model is unchanged
- files from all messages in the current chat are included
- duplicate file associations are deduplicated
- every `File.user_id` must equal the current user
- `files` and `metadata.files` are removed only for AgentCore requests

**Step 2: Run the filter test and verify RED**

Run:

```bash
python3 -m unittest tests.test_openwebui_filter -v
```

Expected: module does not exist.

**Step 3: Implement the minimum filter**

The async inlet signature is:

```python
async def inlet(
    self,
    body: dict,
    __user__: dict,
    __metadata__: dict,
    __chat_id__: str,
) -> dict:
```

For AgentCore only:

1. Require user and chat IDs.
2. Verify the chat belongs to the current user.
3. Query `ChatFile` rows for the complete chat.
4. Join/load each `File` record.
5. Reject missing or differently owned files.
6. Deduplicate by file ID.
7. Add metadata-only `agentcore_files`.
8. Remove OpenWebUI file/RAG fields.

**Step 4: Run the filter tests and verify GREEN**

### Task 4: Idempotent filter installer

**Files:**
- Create: `openwebui-local/install_filter.py`
- Create: `openwebui-local/install_filter.sh`
- Modify: `openwebui-local/docker-compose.yml`
- Modify: `openwebui-local/start.sh`

**Step 1: Mount the versioned filter and installer read-only**

Mount `openwebui-local/functions/` and `install_filter.py` under
`/opt/agentcore-openwebui/`.

**Step 2: Implement an idempotent installer**

Before first install, use SQLite's backup API to create one timestamped database
backup. Upsert function ID `agentcore_file_context` as:

```text
type=filter
is_active=true
is_global=true
```

Update the existing row on later starts rather than creating duplicates.

**Step 3: Integrate with `start.sh`**

After OpenWebUI becomes healthy:

1. Run `install_filter.sh`.
2. Restart the container to clear function-module cache.
3. Wait for healthy status again.

**Step 4: Verify idempotency**

Run `start.sh` twice and query the function table. Expected: exactly one active,
global `agentcore_file_context` row.

### Task 5: Metadata-only IAM

**Files:**
- Modify: `infra/user_uploads_bootstrap.py`

**Step 1: Extend the proxy policy generator**

Grant the proxy role only:

```text
s3:ListBucket (conditioned to openwebui-test/*)
s3:GetObjectTagging
```

on:

```text
arn:aws:s3:::agentcore-openwebui-test-964340114883/openwebui-test/*
```

Do not grant `s3:GetObject` on the OpenWebUI bucket.

**Step 2: Apply only the proxy-policy function**

Run:

```bash
python3 -c \
  'from infra.user_uploads_bootstrap import grant_proxy_upload; grant_proxy_upload()'
```

**Step 3: Verify deployed IAM**

Read the inline policy and confirm metadata permissions are present while
OpenWebUI object download permission is absent.

### Task 6: Documentation and privacy logging

**Files:**
- Modify: `openwebui-local/README.md`
- Modify: `docs/DEPLOY.md`
- Modify: `outstanding_task.md`
- Remote: `/etc/caddy/Caddyfile` on EC2

**Step 1: Update documentation**

Document:

- header-to-ActorID/session mapping
- filter installation and manifest shape
- S3 ownership validation
- file limits and errors
- trusted-frontend POC boundary
- signed JWT and object-scoped CI access as deferred production work
- Dify as deferred

**Step 2: Disable Caddy access logging**

Remove the site-level access `log` block, validate the Caddyfile, restart Caddy,
and confirm the service remains bound only to `100.79.116.60:18080`.

### Task 7: Build, deploy, and end-to-end verification

**Files:**
- Modify only if required by discovered defects in the preceding tasks.

**Step 1: Run local verification**

```bash
python3 -m unittest discover -s tests -v
bash -n openwebui-local/*.sh
git diff --check
```

**Step 2: Build and push the proxy**

Use `proxy/build_and_push.sh`, then roll out `deploy/agentcore-proxy`.

**Step 3: Verify deployment**

Confirm:

- pod ready
- `/health` and `/models` return 200
- missing headers fail closed
- real OpenWebUI request logs namespaced actor/session
- text-only streaming succeeds

**Step 4: Verify file analysis**

Use the existing chat containing `costs.csv`. Ask AgentCore to report a
deterministic calculation and confirm:

- the filter emits one chat-wide manifest
- proxy validates the owner and file ID tags
- AgentCore invokes Code Interpreter conditionally
- response matches an independently calculated result
- a later turn can reuse the file without reattachment

**Step 5: Verify isolation failures**

Use safe synthetic metadata/tests to prove wrong-owner and cross-chat manifests
are rejected before AgentCore invocation.

**Step 6: Final audit**

Confirm no Caddy identity-header access logs are generated and no unrelated
worktree changes were altered.

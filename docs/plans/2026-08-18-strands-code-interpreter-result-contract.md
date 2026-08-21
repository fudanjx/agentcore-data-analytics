# Strands Code Interpreter Result Contract Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace raw, character-truncated AgentCore Code Interpreter event JSON with a bounded semantic result contract, while retaining an emergency legacy mode, then package and deploy the change as `strands_agent_v0.0.7.zip` to the live `Strands_runtime-mk6uFHBu9d` runtime.

**Architecture:** Add a dependency-free result-normalization module between the AgentCore Code Interpreter event stream and the Strands tool result. In the default semantic mode, the model-generated program emits a final `AGENTCORE_RESULT_JSON=` marker; the normalizer validates and bounds that contract, with a bounded stdout/error fallback when the marker is absent. Existing tool inputs, Code Interpreter sessions, gateways, skills, memory, prompt caching, streaming, S3 permissions, and runtime network configuration remain unchanged.

**Tech Stack:** Python 3.13, Strands Agents, Amazon Bedrock AgentCore Runtime and Code Interpreter, boto3/botocore, `unittest`, Docker Desktop on Apple Silicon macOS, AWS CLI, S3-source AgentCore deployment.

---

## Scope and fixed decisions

- Implement only section 1, **Code Interpreter result contract**, from `Strands_agent_design.md`.
- Use hybrid normalization: declared semantic contract first, bounded automatic fallback second.
- Enable semantic mode by default.
- Preserve `CODE_INTERPRETER_RESULT_MODE=legacy` as an emergency rollback switch without rebuilding.
- Keep the existing `CODE_INTERPRETER_MAX_RESULT_CHARS` variable for legacy mode.
- Add `CODE_INTERPRETER_SEMANTIC_MAX_CHARS`, default `10000`, for the result returned to the model in semantic mode.
- Cap semantic samples at 30 rows and 20 columns.
- Preserve the current 900-second Bedrock read timeout, 10-second connection timeout, two retry attempts, 30,000-character legacy Code Interpreter setting, 12,000-character streamed tool-detail setting, and all live AgentCore integrations.
- Build a Linux ARM64/Python 3.13 ZIP on the local Apple Silicon Mac using Docker Desktop.
- Upload to `s3://bedrock-agentcore-runtime-964340114883-ap-southeast-1-7a3qgyspw/strands_agent_v0.0.7.zip`.
- Update live runtime `Strands_runtime-mk6uFHBu9d`, preserving its current IAM role, VPC, lifecycle, protocol, entry point, request-header configuration, authorizer configuration, and environment variables.
- If post-deployment smoke tests fail, restore `strands_agent_v0.0.6.zip` with the same preserved runtime configuration.

### Task 1: Add the pure semantic result normalizer using TDD

**Files:**
- Create: `Strands-runtime/code_interpreter_result.py`
- Create: `Strands-runtime/tests/test_code_interpreter_result.py`

**Step 1: Write failing tests for a valid declared contract**

Create tests that pass synthetic AgentCore result events containing text whose final line is:

```text
AGENTCORE_RESULT_JSON={"ok":true,"summary":"Aggregated monthly visits.","row_count":18420,"columns":["month","department","visit_count"],"metrics":{"departments":18},"sample_rows":[{"month":"2026-06","department":"Cardiology","visit_count":245}],"artifacts":[{"s3_uri":"s3://example/outputs/report.html","filename":"report.html","content_type":"text/html"}],"warnings":[]}
```

Assert that the returned JSON object:

- has `contract_version: 1` and `source: "declared"`;
- preserves the allowed semantic fields;
- excludes the raw stream envelope and marker text;
- serializes to at most the requested maximum size.

**Step 2: Run the focused test and verify it fails**

Run:

```bash
python3 -m unittest discover -s Strands-runtime/tests -p 'test_code_interpreter_result.py' -v
```

Expected: `FAIL` or import error because `code_interpreter_result.py` does not exist.

**Step 3: Write failing boundary and fallback tests**

Cover:

- marker split across multiple text blocks;
- multiple markers, where the last valid marker wins;
- invalid JSON marker;
- missing marker;
- `isError: true` and non-zero `structuredContent.exitCode`;
- very large stdout with useful content at the beginning and end;
- more than 30 sample rows;
- more than 20 columns;
- oversized strings, warnings, metrics, and artifacts;
- binary event fields;
- non-finite numbers;
- `max_chars` too small to hold the full normalized result.

Expected fallback shape:

```json
{
  "contract_version": 1,
  "source": "fallback",
  "ok": true,
  "summary": "Code Interpreter completed without a declared semantic result.",
  "stdout_preview": "bounded head and tail text",
  "warnings": ["The program did not emit AGENTCORE_RESULT_JSON."]
}
```

Error fallback must set `ok: false` and retain only bounded, actionable error text and exit-code metadata.

**Step 4: Implement the dependency-free normalizer**

Implement these public elements:

```python
RESULT_MARKER = "AGENTCORE_RESULT_JSON="
RESULT_CONTRACT_VERSION = 1

def render_semantic_events(events: Iterable[dict], max_chars: int = 10_000) -> str:
    """Consume AgentCore result events and return one bounded JSON contract."""

def render_legacy_events(events: Iterable[dict], max_chars: int) -> str:
    """Retain the current raw-event JSON behavior for emergency compatibility."""

def result_is_error(rendered: str) -> bool:
    """Recognize both semantic-object and legacy-list failures."""
```

Implementation rules:

- Process the event iterable once.
- In semantic mode, do not accumulate unbounded raw events.
- Retain a bounded text head and rolling tail so a final marker survives large stdout.
- Accept marker content only as a JSON object.
- Allow only `ok`, `summary`, `row_count`, `columns`, `metrics`, `sample_rows`, `artifacts`, `warnings`, and `error` from declared output.
- Require `ok` to be boolean and `summary` to be a non-empty string; otherwise use fallback.
- Keep metrics scalar-only and bounded.
- Keep sample-row values scalar-only, maximum 30 rows and 20 keys per row.
- Keep maximum 20 column names, 20 metrics, 20 warnings, and 20 artifacts.
- Keep artifact fields only for `s3_uri`, `filename`, and `content_type`; never fetch or validate S3 inside the normalizer.
- Replace non-finite floats with strings or `null` so output always uses valid JSON.
- Bound individual strings before final serialization.
- If the final object still exceeds `max_chars`, progressively reduce previews, rows, artifacts, metrics, columns, and warnings while always preserving `contract_version`, `source`, `ok`, and a bounded `summary` or `error`.

**Step 5: Run the normalizer tests**

Run:

```bash
python3 -m unittest discover -s Strands-runtime/tests -p 'test_code_interpreter_result.py' -v
```

Expected: all tests pass without installing boto3 or Strands.

### Task 2: Integrate semantic results into the Code Interpreter wrapper

**Files:**
- Modify: `Strands-runtime/code_interpreter.py`
- Modify: `Strands-runtime/tests/test_code_interpreter.py`

**Step 1: Write failing wrapper tests**

Stub `get_client()` with a fake client whose `invoke_code_interpreter()` returns synthetic streaming events. Verify:

- default mode calls `render_semantic_events()`;
- `CODE_INTERPRETER_RESULT_MODE=legacy` calls `render_legacy_events()`;
- an SDK exception becomes a semantic `ok: false` contract in semantic mode;
- legacy exception text remains backward-compatible;
- `_tool_result_is_error()` recognizes both formats;
- `stage_skill_resource` still reports staging success and failure correctly.

Because configuration is loaded at import time, tests must patch the module constants directly or reload the module with a controlled environment.

**Step 2: Run the focused wrapper tests and verify they fail**

Run:

```bash
python3 -m unittest discover -s Strands-runtime/tests -p 'test_code_interpreter.py' -v
```

Expected: failures because semantic integration does not yet exist.

**Step 3: Add bounded environment configuration**

Add:

```python
RESULT_MODE = os.environ.get("CODE_INTERPRETER_RESULT_MODE", "semantic").strip().lower()
if RESULT_MODE not in {"semantic", "legacy"}:
    raise ValueError("CODE_INTERPRETER_RESULT_MODE must be 'semantic' or 'legacy'")

SEMANTIC_MAX_RESULT_CHARS = min(
    20_000,
    max(2_000, int(os.environ.get("CODE_INTERPRETER_SEMANTIC_MAX_CHARS", "10000"))),
)
```

Keep the existing `MAX_RESULT_CHARS` behavior for legacy mode.

**Step 4: Replace raw collection with mode-aware streaming normalization**

Change `_invoke_and_collect()` so it passes `response["stream"]` directly to the selected renderer. Do not first build a list in semantic mode.

Change `_invoke_tool()` so an exception in semantic mode returns a JSON object equivalent to:

```json
{
  "contract_version": 1,
  "source": "runtime_error",
  "ok": false,
  "summary": "Code Interpreter invocation failed.",
  "error": "bounded exception text",
  "warnings": []
}
```

Do not include credentials, complete request arguments, code bodies, or stack traces in the returned model context; keep full stack traces in Runtime logs.

**Step 5: Update tool descriptions**

Tell `execute_code` and `execute_command` to:

- aggregate and calculate inside the sandbox;
- avoid printing full dataframes, raw SQL results, or recursive listings;
- print one final `AGENTCORE_RESULT_JSON=<single-line JSON object>` marker;
- return at most 30 representative rows and 20 columns;
- place full generated output in a file/S3 artifact and return artifact metadata;
- set `ok: false` with an actionable error for failures.

**Step 6: Run wrapper and normalizer tests**

Run:

```bash
python3 -m unittest discover -s Strands-runtime/tests -p 'test_*.py' -v
```

Expected: all tests pass.

### Task 3: Add stable agent guidance without changing caller prompts

**Files:**
- Modify: `Strands-runtime/agent.py`
- Modify: `Strands-runtime/tests/test_agent_guidance.py`

**Step 1: Write failing guidance tests**

Test a small pure helper that returns Code Interpreter guidance:

- semantic mode returns the structured-result instructions;
- legacy mode returns an empty string;
- the marker name, sample limits, artifact behavior, and no-unbounded-print rule are present;
- the guidance does not include request-specific data, S3 URIs, or user identity.

**Step 2: Run the test and verify it fails**

Run:

```bash
python3 -m unittest discover -s Strands-runtime/tests -p 'test_agent_guidance.py' -v
```

Expected: failure because the helper/guidance is not integrated.

**Step 3: Implement stable guidance assembly**

Expose a constant or function from `code_interpreter.py`, for example:

```python
def system_guidance() -> str:
    return SEMANTIC_RESULT_GUIDANCE if RESULT_MODE == "semantic" else ""
```

Append this stable guidance in `_prepare()` only when Code Interpreter is enabled and configured. Place it before caller-provided system guidance so existing caller instructions are retained while the Runtime-level output safety contract remains explicit.

Do not change message-history handling, AgentCore Memory, skill activation, MCP/Gateway tools, model configuration, stream events, or invocation limits.

**Step 4: Run all unit tests and compile checks**

Run:

```bash
python3 -m unittest discover -s Strands-runtime/tests -p 'test_*.py' -v
python3 -m compileall -q Strands-runtime
```

Expected: all tests pass and compilation exits zero.

### Task 4: Update runtime documentation and packaging manifest

**Files:**
- Modify: `Strands-runtime/README.md`
- Modify: `Strands-runtime/USER_GUIDE.md`
- Modify: `Strands-runtime/build_agentcore_bundle.ps1`

**Step 1: Add the new module to the bundle manifest**

Add `code_interpreter_result.py` to `$runtimeFiles` in `build_agentcore_bundle.ps1`. Tests must not be included in the deployment ZIP.

**Step 2: Document the contract and controls**

Document:

- semantic mode is default;
- exact marker format and allowed schema;
- 30-row and 20-column limits;
- `CODE_INTERPRETER_SEMANTIC_MAX_CHARS` default and range;
- `CODE_INTERPRETER_RESULT_MODE=legacy` emergency behavior;
- `CODE_INTERPRETER_MAX_RESULT_CHARS` applies to legacy mode;
- full datasets and generated files remain in the sandbox or S3 instead of model context;
- examples use v0.0.7 rather than stale v0.0.5 packaging names.

**Step 3: Validate documentation and Git diff**

Run:

```bash
rg -n "CODE_INTERPRETER_RESULT_MODE|CODE_INTERPRETER_SEMANTIC_MAX_CHARS|AGENTCORE_RESULT_JSON|v0.0.7" Strands-runtime/README.md Strands-runtime/USER_GUIDE.md Strands-runtime/build_agentcore_bundle.ps1
git diff --check
git diff -- Strands-runtime
```

Expected: documented values match code and no whitespace errors are introduced.

### Task 5: Build and validate the v0.0.7 ZIP on macOS

**Files:**
- Create locally, ignored/release output: `Strands-runtime/dist/strands_agent_v0.0.7.zip`

**Step 1: Confirm Docker Desktop and target architecture**

Run:

```bash
docker version --format 'client={{.Client.Version}} server={{.Server.Version}} os={{.Server.Os}} arch={{.Server.Arch}}'
```

Expected: Docker server is Linux ARM64. This was preflighted as Docker 29.4.1, Linux `arm64`.

**Step 2: Build using the README-equivalent Docker workflow**

Because `pwsh` is not installed on this Mac, reproduce `build_agentcore_bundle.ps1` exactly with a temporary staging directory:

1. create a `mktemp -d` staging root;
2. create its `strands_agent/` directory;
3. run `python:3.13-slim-bookworm` with `--platform linux/arm64/v8`;
4. install `Strands-runtime/requirements.txt` into the mounted bundle directory;
5. copy only the ten runtime files in the PowerShell manifest, including the new normalizer;
6. create the ZIP with paths rooted under `strands_agent/`;
7. move the completed ZIP to `Strands-runtime/dist/strands_agent_v0.0.7.zip` only after success.

Do not copy tests, caches, local credentials, `.env` files, or the entire repository.

**Step 3: Validate ZIP structure and imports**

Run checks equivalent to:

```bash
unzip -l Strands-runtime/dist/strands_agent_v0.0.7.zip
unzip -p Strands-runtime/dist/strands_agent_v0.0.7.zip strands_agent/code_interpreter_result.py | shasum -a 256
docker run --rm --platform linux/arm64/v8 \
  --mount type=bind,source="$PWD/Strands-runtime/dist/strands_agent_v0.0.7.zip",target=/tmp/runtime.zip,readonly \
  python:3.13-slim-bookworm sh -lc \
  'python -m zipfile -e /tmp/runtime.zip /tmp/runtime && cd /tmp/runtime/strands_agent && python -c "import main, agent, code_interpreter, code_interpreter_result"'
```

Expected:

- `strands_agent/main.py` exists;
- every manifest file exists exactly once;
- no `tests/`, `.git/`, `.env`, `__pycache__/`, or macOS metadata exists;
- Linux ARM64/Python 3.13 imports succeed.

**Step 4: Record artifact metadata**

Run:

```bash
shasum -a 256 Strands-runtime/dist/strands_agent_v0.0.7.zip
stat -f '%z bytes' Strands-runtime/dist/strands_agent_v0.0.7.zip
```

Record checksum and size in the delivery report.

### Task 6: Upload and update the live AgentCore Runtime safely

**Files:**
- No repository files.

**Step 1: Re-read and save current runtime configuration**

Immediately before deployment, run:

```bash
aws bedrock-agentcore-control get-agent-runtime \
  --region ap-southeast-1 \
  --agent-runtime-id Strands_runtime-mk6uFHBu9d \
  --output json
```

Expected pre-deployment state at planning time:

- status `READY`;
- version `33`;
- artifact `strands_agent_v0.0.6.zip`;
- role `arn:aws:iam::964340114883:role/agentcore-poc-runtime-role`;
- VPC security group `sg-07258677b7e691e48`;
- subnets `subnet-061205c705e0f41d4` and `subnet-0466b6e1fbb8a49f3`;
- HTTP protocol;
- lifecycle idle timeout 3600 and max lifetime 28800.

Treat this snapshot as time-sensitive and generate the update payload from the fresh response, not from hard-coded planning values.

**Step 2: Upload and verify the S3 object**

Run:

```bash
aws s3 cp Strands-runtime/dist/strands_agent_v0.0.7.zip \
  s3://bedrock-agentcore-runtime-964340114883-ap-southeast-1-7a3qgyspw/strands_agent_v0.0.7.zip \
  --region ap-southeast-1 \
  --only-show-errors
aws s3api head-object \
  --region ap-southeast-1 \
  --bucket bedrock-agentcore-runtime-964340114883-ap-southeast-1-7a3qgyspw \
  --key strands_agent_v0.0.7.zip
```

Verify remote content length and checksum/version metadata against the local artifact before updating the runtime.

**Step 3: Update the runtime with preserved configuration**

Build an `update-agent-runtime` input JSON from the fresh runtime snapshot. Change only:

```json
"agentRuntimeArtifact.codeConfiguration.code.s3.prefix": "strands_agent_v0.0.7.zip"
```

Keep runtime `PYTHON_3_13`, entry point `strands_agent/main.py`, role, network, lifecycle, protocol, request headers, authorizer, and every existing environment variable. Add no semantic-mode variables unless an explicit override is needed because semantic mode defaults are compiled into v0.0.7.

**Step 4: Wait for the new runtime version**

Poll `get-agent-runtime` at short intervals until it reports `READY` with the v0.0.7 artifact. Stop and investigate immediately if it reports `CREATE_FAILED`, `UPDATE_FAILED`, or another terminal failure.

### Task 7: Perform live regression and behavior validation

**Files:**
- No repository files.

**Step 1: Basic compatibility smoke test**

Invoke the live Runtime with a unique actor and session ID using a simple request that does not need Code Interpreter.

Expected: normal streaming completion, final stop event, and no regression in proxy-compatible output.

**Step 2: Semantic Code Interpreter smoke test**

Ask Code Interpreter to calculate a small aggregate and return a representative sample.

Expected CloudWatch/runtime evidence:

- Code Interpreter runs successfully;
- tool output returned to Strands is a compact semantic JSON object;
- `source` is `declared` when the model follows the marker instruction, otherwise `fallback`;
- no raw AgentCore event array or unbounded dataframe appears in model context;
- final user response completes.

**Step 3: Artifact and error smoke tests**

- Generate a small temporary file and return artifact metadata without inserting file contents into the tool result.
- Execute a controlled failing command and verify a concise `ok: false` result reaches the agent.

Expected: both invocations complete and the failure remains actionable without exposing a stack trace.

**Step 4: Verify unchanged integrations**

Confirm the fresh runtime configuration still contains the same:

- four Gateway definitions;
- Code Interpreter ID and Region;
- shared Memory ID;
- model ARN and timeout/retry values;
- skills bucket/prefix;
- prompt-cache setting;
- tool-detail limit;
- VPC, lifecycle, role, and protocol.

**Step 5: Roll back on regression**

If any required smoke test fails because of v0.0.7, update the runtime artifact back to `strands_agent_v0.0.6.zip` using the same freshly preserved configuration, wait for `READY`, and repeat the basic compatibility smoke test. Keep the failed v0.0.7 S3 artifact for diagnosis; do not delete or overwrite v0.0.6.

### Task 8: Final verification and handoff

**Files:**
- No additional changes unless a test reveals a defect.

**Step 1: Run the complete local verification set**

```bash
python3 -m unittest discover -s Strands-runtime/tests -p 'test_*.py' -v
python3 -m compileall -q Strands-runtime
git diff --check
git status --short --branch
```

Expected: tests and compilation pass, no whitespace errors, and only intentional files are modified/untracked. Preserve the existing unrelated deletion of `outstanding_task.md` and the existing untracked `Strands_agent_design.md`; do not stage or alter them.

**Step 2: Report exact outcome**

Report:

- changed files and behavioral contract;
- test counts and commands;
- local and S3 ZIP checksum/size;
- new AgentCore runtime version and final status;
- smoke-test results;
- whether fallback or rollback was needed;
- remaining limitations: the hybrid contract reduces result-context pressure but does not yet add the separate Strands turn/token budgets from section 2 of the design.

Do not commit or push unless the user separately requests Git publication.

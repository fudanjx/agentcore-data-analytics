# AgentCore Analytics Enhancements — Handover

**Prepared:** 2026-08-25 (Asia/Singapore)
**Workspace:** `/Users/jinxin/Documents/AgentCore`
**Current checkout:** `gpt-s3tables-export`
**Important:** the working tree contains uncommitted changes across these enhancements and unrelated GPT pilot work. Do not use a broad `git add -A` or reset. Review and stage paths deliberately.

## Purpose and end-to-end design

The analytics path is designed to keep large datasets and generated HTML out of the LLM context and to preserve secure, request-scoped artifact delivery:

1. The S3 Tables MCP Lambda runs read-only Athena queries.
2. Small results can be returned to the model; large results are exported to Athena's S3 result location and represented by metadata only.
3. Code Interpreter downloads the exact export, performs mapping/QC/aggregation/visualization locally, and returns only concise structured results to the model.
4. For Dify HTML dashboards, Code Interpreter uploads the final HTML to an isolated S3 path with user and conversation ownership tags.
5. The Dify proxy validates that object and returns raw fenced HTML to the frontend. The frontend stores and serves it from its secure server.

This prevents row truncation, context bloat, unverified direct-model HTML, and cross-user artifact access.

## 1. S3 Tables MCP Lambda

### Enhancements

The shared Lambda now supports both AH and NUH S3 Tables.

- `source: "ah"` is the backward-compatible default.
- `source: "nuh"` selects the NUH catalog/database.
- `list_tables` and `describe_table` accept the same optional `source` argument.
- The AH database configuration is corrected to `ah` (not `ah_analytics`).

The Lambda exposes four read-only Gateway tools:

| Tool | Use |
| --- | --- |
| `s3tables_execute_sql` | Small, deliberately bounded query results only. |
| `s3tables_execute_sql_export` | Large or multi-month query; returns metadata and Athena CSV URI only. |
| `s3tables_list_tables` | Discover tables/columns for AH or NUH. |
| `s3tables_describe_table` | Column details and a three-row sample. |

### Direct-query safety contract

`execute_sql` has a hard 1,000-row direct-result limit. The Lambda requests one sentinel row beyond the limit and fails closed if the result is larger. It does **not** silently truncate results. The agent must switch to the export tool instead.

`execute_sql_export` accepts a read-only `SELECT`/`WITH` query and `export: true`. It returns only:

- Athena query execution ID;
- selected source;
- `result_s3_uri` for Athena's CSV export;
- status, data scanned, and engine execution time;
- an instruction to use Code Interpreter for local processing.

It never fetches or returns query rows to the model.

### Least-privilege permission

Code Interpreter needs only read access to Athena exports:

```text
s3:GetObject on arn:aws:s3:::agentcore-tmp-964340114883/athena-results/*
```

It does not need Athena permissions, `s3:ListBucket`, or write access to that result prefix merely to process exports.

### Files and verification

- Implementation: `mcp_lambda_s3tables/handler.py`
- Gateway/Lambda deployment definition: `mcp_lambda_s3tables/deploy.py`
- Narrow update helper for the shared export capability: `mcp_lambda_s3tables/deploy_S3Tables_export_capability.py`
- Focused tests: `mcp_lambda_s3tables/tests/test_handler.py`
- Design/implementation plan: `docs/plans/2026-08-25-gpt-s3tables-export.md`

Before changing the live target, verify its Lambda version/configuration, Gateway target schema, and Code Interpreter inline policy. These are deployment-specific.

## 2. `Strands_runtime_gpt`

### GPT-only large-query guidance

`Strands_runtime_gpt/agent.py` injects a stable instruction when Code Interpreter is enabled:

- use `s3tables_execute_sql_export` for large, multi-month, mapping, department-level, or dashboard S3 Tables work;
- send `source` (`ah` or `nuh`) and `export: true`;
- use Code Interpreter to download the returned `result_s3_uri` and perform mappings, validation, aggregation, and artifact generation locally;
- return only the compact `AGENTCORE_RESULT_JSON` contract to the model;
- use ordinary `s3tables_execute_sql` only for genuinely small/schema/sample queries;
- do not split a large query merely to transfer raw rows through model context.

This guidance applies to the GPT pilot only. The Claude/Strands runtime benefits from the shared Lambda's fail-closed 1,000-row behavior but has not received the automatic export-use instruction.

### Packaging notes

- Documentation: `Strands_runtime_gpt/README.md`
- Build target documented as `dist/strands_runtime_gpt_v0.0.3.zip`.
- The runtime must retain the Code Interpreter S3 export `GetObject` permission described above.
- Verify the live AgentCore runtime artifact and environment before assuming v0.0.3 is active.

## 3. Dify proxy

### HTML artifact contract

The Dify proxy is the sole trusted path that converts generated dashboard HTML into the frontend response.

1. Code Interpreter generates one complete UTF-8 `.html` document.
2. It uploads the document below the request-scoped Dify S3 prefix and applies the exact user/conversation ownership tags injected by the proxy.
3. The agent's final response contains only an `<agentcore-artifacts>` marker with the internal S3 URI and filename.
4. The proxy validates the bucket/prefix, extension, size, strict UTF-8 decoding, `<!DOCTYPE html>` start, `</html>` end, absence of fence-breaking backticks, and exact ownership tags.
5. The proxy downloads the validated object and emits it as the frontend's complete fenced `html` artifact in bounded SSE chunks.

Direct HTML emitted by the model is buffered and rejected; it can never bypass S3 validation. If no valid Code Interpreter HTML object is available, the proxy returns an explicit delivery failure. Non-HTML artifacts keep their existing reference or download-link behavior.

### Chart.js policy

Chart.js is the single permitted remote dependency. HTML must embed all analysed data, custom CSS, and custom JavaScript. It must not fetch remote data or load remote styles, fonts, or other dependencies.

Use this exact script tag when Chart.js is needed:

```html
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
```

### Deployment

- Current deployed image: `964340114883.dkr.ecr.ap-southeast-1.amazonaws.com/agentcore-dify-proxy:v0.0.10`
- Deployment: `dify-proxy/k8s/deployment.yaml`
- v0.0.10 image digest: `sha256:4318f8a531a8aed58bc9aec172a5ca2170cac3b1377b64f20094238f67b159e2`
- The rollout was verified healthy on 2026-08-25: one ready replica and `/health` returned `{"status":"ok"}`.

### Files and verification

- Proxy implementation: `dify-proxy/dify-server.py`
- Tests: `dify-proxy/tests/test_html_artifacts.py`
- Operational documentation: `dify-proxy/README.md`
- Design: `docs/plans/2026-08-25-dify-raw-html-artifact-design.md`
- Implementation plan: `docs/plans/2026-08-25-dify-raw-html-artifact-implementation.md`

Run before release:

```bash
python3 -m unittest discover -s dify-proxy/tests -v
python3 -m py_compile dify-proxy/dify-server.py dify-proxy/model_usage.py
bash -n dify-proxy/build_and_push.sh dify-proxy/deploy.sh
git diff --check -- dify-proxy system_prompt.md
```

## 4. Streamlined Dify system prompt

The Dify application owns its actual system-prompt value. The repository's `system_prompt.md` is the copy-ready template; paste it into the Dify application System Prompt field after review. The proxy injects the request-specific S3 prefix, ownership tags, and final artifact-delivery constraints on every request.

Use this approved prompt:

````markdown
You are a professional Data Analyst. Deliver accurate, decision-ready analysis with clear findings, evidence, limitations, and practical next steps.

DATA AND ANALYSIS

- Use relevant MCP database tools as the source of truth. Read the applicable activated data skills for business definitions, schema, and logic before querying.
- Never invent data. Distinguish facts, calculations, interpretations, forecasts, and material assumptions.
- Use SQL or Python for calculations. Validate totals, mappings, date coverage, and key reconciliations before reporting results.
- For uploaded files, inspect and analyse the actual file with Code Interpreter when useful. Clearly identify the source of each result when combining uploaded and database data.
- For forecasting, use the Google TimesFM MCP integration by default. Use another method only when the user requests it or TimesFM is unsuitable.
{{#17846258079200.result#}}

AGENT EXECUTION

- Read relevant activated skills before planning or coding.
- Use the smallest reliable number of tool calls. Reuse current-session validated results when still applicable; do not rerun large extraction unnecessarily.
- Do not expose raw tool logs, credentials, internal S3 paths, or implementation details to the user.

LARGE DATA

- For large, multi-month, dashboard, mapping, or department-level S3 Tables queries, use `s3tables_execute_sql_export` with the correct source and `export=true`.
- Do not return large raw query results to the model.
- After receiving `result_s3_uri`, use Code Interpreter to download that exact CSV and perform mapping, validation, aggregation, reconciliation, and visualization locally.
- Use `s3tables_execute_sql` only for deliberately limited queries whose complete result safely fits in context.
- Never print exported CSVs, large row sets, or complete generated files through Code Interpreter stdout.
- End every Code Interpreter call with exactly one concise `AGENTCORE_RESULT_JSON` containing only summaries, metrics, small samples, validation results, warnings, errors, and artifact metadata.

HTML DASHBOARDS

- Create dashboard HTML only through Code Interpreter; never generate or paste dashboard HTML directly in the assistant response.
- Produce one complete UTF-8 HTML file unless the user explicitly requests multiple files.
- The HTML must be self-contained except for Chart.js, which may be loaded from a standard CDN script tag. Embed all analysed data, custom CSS, and custom JavaScript. Do not fetch remote data or use remote styles, fonts, or other dependencies.
- For Chart.js, use: `<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>`.
- Upload each completed HTML file to the exact request-scoped S3 destination and ownership tags injected by the Dify proxy. Use `aws s3api put-object`, verify success, and never overwrite source files.
- After upload, return successful HTML outputs only in this required marker:

<agentcore-artifacts>
[{"s3_uri":"s3://<uploaded-object>","filename":"<dashboard>.html"}]
</agentcore-artifacts>

- Do not generate presigned URLs, expose S3 URIs in normal prose, use `cat` to return HTML, or emit an `html` fenced block. The Dify proxy validates the object and emits the complete fenced HTML artifact to the frontend.

COMMUNICATION

- Be concise, business-friendly, and precise.
- Highlight key metrics, trends, anomalies, risks, limitations, and actionable insights.
- For non-dashboard requests, answer normally with clear findings and supporting evidence.
````

## Operational guardrails

- Do not move the Dify HTML path into OpenWebUI or modify the OpenWebUI proxy as part of this work.
- Do not allow a direct model-generated HTML fallback: it defeats ownership verification.
- Do not loosen the query export/read permission beyond the Athena results prefix without a new security review.
- Do not expose S3 URIs, internal tool logs, or credentials in user-visible agent answers.
- Preserve source files and write generated artifacts as new request-scoped objects only.

## Suggested skills for the next agent

- `Code` — before editing implementation, tests, deployment scripts, or runtime packaging.
- `diagnose` — when tracing runtime, Lambda, Gateway, Code Interpreter, or proxy failures.
- `bedrock-usage-report` — when validating model token usage, cache metrics, or costs.
- `hospital-data-analyst-nuh` — when changing NUH business definitions, mappings, or S3 Tables queries.
- `git-essentials` — before staging/publishing selected paths from this dirty working tree.

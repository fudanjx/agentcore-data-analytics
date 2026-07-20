# Outstanding Tasks

Rolling backlog of known limitations, deferred work, and follow-ups for this repo. Add new items to the top of the relevant section; delete when done.

---

## OpenWebUI Insights files, identity, and Code Interpreter

Shipped 2026-07-20. The isolated OpenWebUI v0.10.2-slim test service at
`https://insights.bot-alex.com` runs alongside the legacy EC2 deployment with
its own PostgreSQL database and Docker volume. Browser uploads are
server-mediated: browser → OpenWebUI → the dedicated bucket
`s3://agentcore-openwebui-insights-964340114883/openwebui-insights/`. The
bucket has private access, SSE-S3, versioning, and a seven-day current-object
lifecycle expiry.

For the `insights` / `agentcore.insights` model only, the OpenWebUI filter
checks chat ownership, builds a chat-wide manifest of the authenticated user's
files, and removes raw OpenWebUI file metadata before the request leaves the
application. The proxy validates each manifest entry's bucket, prefix, owner
tags, type, and size; then it maps identity to
`ActorID=openwebui-insights:<user UUID>` and
`runtimeSessionId=owui-insights-<user UUID>-<chat UUID>`. `/insights/v1` is a
separate proxy namespace but invokes the existing OpenWebUI harness
`harness_e52fs-Du2DM0RxvF`.

The harness receives the validated `s3_uri` manifest and, when analysis is
needed, uses the AgentCore Code Interpreter sandbox to copy the named object
with `aws s3 cp` before inspecting it. Verified end to end with an uploaded
test file (`E2E_SUM=6`) and a real `.xls` analysis that correlated to a Code
Interpreter session creation.

Known gaps:

- **Production identity hardening.** The POC uses plain identity headers only
  on the private OpenWebUI-server → proxy path. Replace them with a signed,
  short-lived JWT; verify issuer, audience, expiry, and replay protection;
  derive `actorId` only from verified claims.

- **Browser-direct S3 upload.** Uploads are currently mediated by OpenWebUI,
  not browser-to-S3 presigned PUTs. The Insights Caddy sidecar enforces
  `process=false` on the upload endpoint, so OpenWebUI's extraction/RAG job is
  not started; this does not make the browser upload directly to S3.

- **Dify identity and native-S3 compatibility are deferred.** The Insights
  actor/chat namespace and chat-wide S3 manifest filter currently apply only
  to OpenWebUI. Design a Dify-specific trusted identity contract and file
  manifest in the next phase.

- **Code Interpreter has broad POC read access to the Insights prefix.** The
  proxy enforces owner tags before disclosing a URI, but the shared Code
  Interpreter execution role can read the Insights prefix. Replace this with
  object-scoped temporary authorization for production.

- **`runtimeSessionId` must be ≥ 33 chars.**
  The Dify `/chat-messages` path forwards `conversation_id` verbatim as the harness session id, which AWS rejects if shorter than 33 chars. Dify's UI passes UUIDs so real users don't hit it; curl/scripted callers do.
  → Pad or hash short `conversation_id`s in `proxy/server.py:_dify_parse` before assigning to `runtimeSessionId`.

- **No S3 upload size streaming.**
  Current handler calls `await file.read()` — loads the whole body into memory before writing to S3. Fine at 50 MB cap but wasteful; use `s3.upload_fileobj` with streaming for larger caps.

- **CI sandbox lifecycle costs unmonitored.**
  Every invocation spins a sandbox microVM billed per second. No dashboard tracks daily invocations or duration.
  → CloudWatch metric filter on Code Interpreter session events → billing dashboard.

---

## S3 Tables backend (`ah-analytics`)

The Iceberg + Athena path shipped 2026-07-14. Source parquet uploads to `s3://ah-data-analytics/` auto-trigger the loader Lambda (`ah-analytics-s3tables-loader`) which full-overwrites the matching Iceberg table. Agent queries via MCP Gateway `ah-analytics-s3tables` → Athena. The following items are known gaps.

### Reliability / observability

- **No CloudWatch alarm on loader Lambda failures.**
  A bad parquet or schema drift silently leaves stale data — you only notice when Athena results look wrong.
  → Add alarm on `Errors > 0` for `ah-analytics-s3tables-loader` → SNS to an email.

- **No retry backstop after S3 event delivery.**
  S3 async invoke retries only twice; after that a failed load is lost until the next upload of that file.
  → Add a DLQ (SQS) on the Lambda, plus a nightly EventBridge schedule that force-reloads all 6 tables.

- **No RDS vs S3 Tables parity check.**
  The RDS path (`ah-analytics-db`) and S3 Tables path (`ah-analytics-s3tables`) can silently diverge if only one is loaded.
  → Nightly row-count parity check (both backends counting the same 6 tables) with alerting on drift > 0.5%.

### Scalability

- **Full-replace only, not incremental.**
  Every trigger rewrites the entire Iceberg table; wall-clock and Iceberg write cost grow linearly with row count. Fine at current volumes (max 1.1M rows), painful at 10x.
  → Switch to Iceberg `MERGE` on natural key (e.g. `case_no` for outpatient) once any table crosses ~10M rows.

- **MCP `execute_sql` result cap is 1000 rows.**
  Silently truncates larger result sets — the agent can't tell.
  → Return an explicit `truncated: true` flag when the cap is hit, or paginate via a cursor.

- **Athena query timeout is hard-coded to 60 s** in `mcp_lambda_s3tables/handler.py:32` (`POLL_MAX_SEC`).
  Long analytical queries from the agent will fail.
  → Expose as an env var with a sane default (e.g. 300 s).

### Ergonomics / maintainability

- **Loader only recognizes 6 exact filenames.**
  A renamed source file (`Combined_SOC_v2_encoded.parquet.gzip`) silently skips — no log, no alert.
  → Broaden S3 notification prefix filter and log a WARNING for unrecognised keys (loader already does this in handler; but it will never fire because the notification filter blocks the event).

- **Loader deps use `>=` not pinned versions** (`lambda_s3tables_loader/requirements.txt`).
  A future rebuild could pull a breaking pandas/pyarrow/pyiceberg upgrade.
  → Pin exact versions; bump only intentionally.

- **Harness-policy update pattern duplicated per deploy script.**
  `mcp_lambda_s3tables/deploy.py` has `add_gateway_to_harness_policy`; the next new gateway will need the same logic or hit the same 403.
  → Extract to `infra/agentcore_gateway_helpers.py`, import from all deploy scripts (existing `mcp_lambda/deploy_ah.py`, `mcp_lambda/deploy.py`, and future).

### Migration

- **No RDS deprecation criteria documented.**
  The plan said "add alongside RDS, deprecate later" but there's no checklist for when to cut over.
  → Define criteria: (a) N days of dual-run without parity drift, (b) agent-side eval benchmarks pass on the S3 Tables path, (c) query latency p95 within 2x of RDS.

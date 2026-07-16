# Outstanding Tasks

Rolling backlog of known limitations, deferred work, and follow-ups for this repo. Add new items to the top of the relevant section; delete when done.

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

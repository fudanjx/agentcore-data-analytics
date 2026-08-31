# S3 Tables Delta Pilot: backend handover and integration contract

**Status:** local pilot handover

**Service:** `s3tables_delta_pilot.webapp`

**Audience:** backend engineer integrating this service with the existing UI/authentication layer
**Last reviewed:** 2026-08-31 (Singapore)

## 1. What this service does

This FastAPI service provides a controlled ingestion path for Amazon S3 Tables
(Iceberg). It supports creating a table from an initial upload, appending later
uploads, mandatory healthcare sanitisation, schema enforcement, composite-key
de-duplication, audit history, and rollback to the latest uploader-managed
Iceberg snapshot.

The current browser application is only a local pilot UI. The backend service
is the source of truth for validation and authorization. A replacement UI must
call the APIs below; it must **not** reimplement schema, sanitisation,
de-duplication, or access-control rules in the browser.

### Source locations

| Responsibility | File |
|---|---|
| FastAPI endpoints, identity placeholder, preflight and staging | `s3tables_delta_pilot/webapp.py` |
| AWS Glue create/append/rollback implementation | `s3tables_delta_pilot/generic_glue_job.py` |
| Schema comparison, canonical names, documented date/time parsing | `s3tables_delta_pilot/ingest_contract.py` |
| Healthcare sanitisation and encryption | `s3tables_delta_pilot/sanitization.py` |
| Pilot AWS constants and deployment/runner CLI | `s3tables_delta_pilot/pilot.py` |
| Local runbook and behaviour overview | `s3tables_delta_pilot/README.md` |
| Date/time contract | `docs/s3_table_date_time_casting_rules.md` |

## 2. Deployment and dependencies

Install the service-specific dependencies, then run from repository root:

```bash
.venv/bin/pip install -r s3tables_delta_pilot/requirements.txt
.venv/bin/uvicorn s3tables_delta_pilot.webapp:app --host 127.0.0.1 --port 8090
```

The local pilot listens at `http://127.0.0.1:8090`. It uses the machine's
standard AWS credential chain; it does not accept AWS credentials in an API
request.

### AWS resources used

| Purpose | Location / resource |
|---|---|
| Sanitised staging input and archive | `s3://ah-data-analytics/temp_s3_update/web_ingest/uploads/<request-id>/` |
| Stored uploader contracts | `s3://ah-data-analytics/temp_s3_update/web_ingest/table_contracts/` |
| Compact history projections | `s3://ah-data-analytics/temp_s3_update/web_ingest/upload_history/` |
| Glue QC reports | `s3://ah-data-analytics/temp_s3_update/qc/web/<run-id>/report.json` |
| Generic Glue job | `ah-soc-delta-pilot-web-ingest` |
| Namespace-local audit table | `uploader_upload_history` |
| Default non-admin pilot scope | table bucket `ah-soc-delta-pilot`, namespace `pilot` |

The staged `uploads/` objects are the only uploader archive and the inputs
read by Glue. They are sanitised, canonicalised, Glue-compatible Parquet—not
the raw browser bytes. A prefix-only S3 lifecycle rule,
`agentcore-s3tables-upload-archive-30-days`, expires them after **30 days**.
Raw pre-sanitisation files must never be retained.

The job also configures S3 Tables snapshot management for uploader-created
tables: at least 12 snapshots and a maximum snapshot age of 365 days. That is
for Iceberg rollback history; it is separate from the 30-day staging archive
retention.

## 3. Production integration boundary

### Current pilot identity model — do not expose as production authentication

The local UI sends only this header:

```http
X-Pilot-User-Id: local-admin
```

`webapp.py` resolves that value against `PILOT_USER_ACCESS_JSON` (or local
defaults). The browser does not send roles or grants. This is deliberately a
trusted localhost placeholder, not proof of user identity. It is vulnerable if
exposed through an untrusted network.

For the existing UI integration, replace `_current_user()` with a verifier for
the existing frontend's authenticated identity (for example a gateway-verified
JWT/session). The verifier must construct the same effective fields:

```text
user_id
is_admin
can_view_upload_history
can_rollback_uploads
allowed (table_bucket_arn, namespace) scopes for non-admin users
```

Do not accept `is_admin`, rollback permission, or bucket grants directly from
the browser. The backend must independently resolve and enforce them.

### Required production changes

1. Replace `X-Pilot-User-Id` emulation with verified identity claims.
2. Disable/remove `GET /api/dev/identity-profiles`; it intentionally exposes
   local test profiles.
3. Set a strong, secret `PILOT_KEY_ANALYSIS_SECRET`. It signs the mandatory
   key-analysis acknowledgement token; the default is only suitable for local
   development.
4. Add deployment-appropriate CORS, TLS, rate limiting, request-size limits,
   CSRF protection if cookie authentication is used, and structured logging.
5. Give the service/Glue execution identity least-privilege access to its
   assigned S3 Table buckets/namespaces, Glue job, required S3 prefixes,
   S3 lifecycle configuration, and the encryption secret. Do not put the
   encryption key in frontend configuration.

## 4. Common API conventions

Base URL in the examples is `http://<host>:8090`.

- APIs return JSON unless serving `/` or `/static/...`.
- File endpoints use `multipart/form-data` and support multiple `files` parts.
- `POST /api/ingestions` and `POST /api/key-impact-analysis` carry their
  structured request in a form part named `request`, encoded as JSON text.
- `POST /api/preflight` uses individual form fields, not a JSON `request` part.
- Namespaces and persisted table names must match `^[a-z][a-z0-9_]{0,254}$`.
  For ingestion requests only, input table-name hyphens are canonicalised to
  underscores before validation.
- File types: `.parquet`, `.parquet.gzip`, `.xlsx`, `.xls`, `.csv`, `.tsv`.
- Treat human-readable error text as display text, not a stable machine
  contract. Key off HTTP status and documented JSON properties.

Typical failure responses:

| HTTP | Meaning |
|---:|---|
| 400 | Malformed request, missing required input, reserved audit table, or invalid rollback confirmation |
| 403 | Identity has no assigned/visible bucket or namespace, or lacks the requested privilege |
| 404 | Missing static asset or no QC report because a job failed before report creation |
| 409 | Browse-only table, invalid append/delete/rollback state, or non-latest rollback |
| 422 | Schema/type/key-analysis validation rejected the content; FastAPI validation errors may also use 422 |
| 500 | AWS configuration, lifecycle, Secrets Manager, or unexpected server-side failure |

For the structured ingestion rejection, render `detail.message`,
`detail.rejection_reasons`, and optionally `detail.preflight`; never expose raw
healthcare row values from a client-side error view.

## 5. API specification

### 5.1 Destination discovery and identity

#### `GET /api/buckets`

Returns table buckets that the effective user may select.

**Response 200**

```json
{
  "user_id": "alice",
  "is_admin": false,
  "can_view_upload_history": true,
  "can_rollback_uploads": true,
  "buckets": [
    {
      "table_bucket_arn": "arn:aws:s3tables:ap-southeast-1:<account>:bucket/ah-soc-delta-pilot",
      "label": "AH SOC delta pilot"
    }
  ]
}
```

An administrator receives all account-visible **customer** S3 Table buckets
that the service IAM role can discover. A non-admin receives only scopes
resolved server-side from its grant mapping.

#### `GET /api/namespaces?table_bucket_arn=<arn>`

The server validates bucket access before discovery.

**Response 200**

```json
{
  "table_bucket_arn": "arn:aws:s3tables:...:bucket/ah-soc-delta-pilot",
  "namespaces": ["pilot"]
}
```

Administrators get dynamically discovered one-level namespaces. Non-admins get
only their configured namespaces for that bucket.

#### `GET /api/identity`

Returns the effective server-resolved identity for diagnostics and UI display.
It is useful during integration testing, but it must not be interpreted as a
replacement for real authentication.

**Response 200**

```json
{
  "user_id": "alice",
  "is_admin": false,
  "can_view_upload_history": true,
  "can_rollback_uploads": true,
  "scope_mode": "configured-bucket-and-namespace-scopes",
  "buckets": [
    {
      "table_bucket_arn": "arn:aws:s3tables:...:bucket/ah-soc-delta-pilot",
      "namespace": "pilot",
      "label": "AH pilot"
    }
  ],
  "request_context": {
    "header_name": "X-Pilot-User-Id",
    "header_value": "alice",
    "roles_and_grants_sent_by_browser": false
  }
}
```

#### `GET /api/dev/identity-profiles` — local development only

Returns the local `local-admin`, `local-editor`, and `local-unassigned` test
definitions. Do not proxy or expose this endpoint in production.

#### `GET /api/tables?table_bucket_arn=<arn>&namespace=<namespace>`

Lists tables for the selected authorized destination. The reserved audit table
is omitted.

**Response 200**

```json
{
  "table_bucket": "arn:aws:s3tables:...:bucket/ah-soc-delta-pilot",
  "namespace": "pilot",
  "is_admin": true,
  "tables": [
    {
      "name": "soc",
      "created_at": "2026-08-01 10:00:00+00:00",
      "modified_at": "2026-08-31 10:00:00+00:00",
      "row_count": 1138633,
      "uploader_managed": true
    }
  ]
}
```

`row_count` is best effort and can be `null` if Iceberg metadata is not
available. `uploader_managed: false` means the table is browse-only: show it,
but do not offer append, delete, uploader-history, or rollback controls.

### 5.2 Preflight and immutable first-upload setup

#### `POST /api/preflight`

Runs server-side local inspection only. It reads temporary local copies of the
submitted files but does not stage S3 objects or start Glue.

**Content type:** `multipart/form-data`

| Form field | Type | Required | Notes |
|---|---|---:|---|
| `mode` | `create` or `append` | yes | `create` profiles first file as initial contract; `append` loads saved contract |
| `table_bucket_arn` | string | yes | Selected S3 Table bucket ARN |
| `namespace` | string | yes | Authorized namespace |
| `table` | string | yes | Table name; use canonical lower-case/underscore form |
| `files` | repeated file part | yes | One or more supported files |

**Response 200 (important fields)**

```json
{
  "mode": "append",
  "table_bucket_arn": "arn:aws:s3tables:...",
  "namespace": "pilot",
  "table": "soc",
  "target_schema": [
    {"name": "visit_date", "type": "DATE", "source_name": "Visit_Date"}
  ],
  "initial_table_column_count": 51,
  "minimum_append_schema_match_percent": 50.0,
  "files": [
    {
      "filename": "SOC_202606.parquet.gzip",
      "source_column_count": 51,
      "target_column_count": 51,
      "matching_column_count": 51,
      "matching_percentage": 100.0,
      "extra_columns_ignored": [],
      "missing_target_columns_filled_null": [],
      "type_conversions": [],
      "sanitization": {},
      "sanitized_columns": [],
      "sanitized_column_count": 0,
      "unsafe_casts": [],
      "accepted": true,
      "rejection_reasons": []
    }
  ],
  "type_selections": [],
  "deduplication_candidates": [],
  "deduplication_columns": ["pat_enc_csn_id"],
  "deduplication_policy": "skip-existing-key-report-conflict-v1",
  "incompatible_sensitive_columns": [],
  "accepted": true,
  "rejection_reasons": [],
  "sensitive_column_scan": "Sanitization is enforced before temporary S3 staging."
}
```

For a new table, `type_selections` contains only truly ambiguous columns. Each
selection includes the normalized column name, detected type, suggested type,
allowed target types and up to five safe, non-empty review samples. Render no
samples for sanitised healthcare fields. `deduplication_candidates` contains
all eligible first-upload columns, including identifier columns; sensitive
samples stay masked.

For an append, hard validation requires at least **50%** of the established
target columns after canonical-name normalisation. Extra source columns are
ignored; missing target columns are filled with `NULL`; incompatible/unsafe
conversions reject the request before staging/Glue.

### 5.3 Composite-key impact analysis (new tables only)

#### `POST /api/key-impact-analysis`

Runs a complete local analysis of the selected incoming files. It must be run
and acknowledged before the initial table can be created.

**Content type:** `multipart/form-data`

| Form field | Type | Required | Notes |
|---|---|---:|---|
| `request` | JSON string | yes | `KeyAnalysisRequest` below |
| `files` | repeated file part | yes | Same files that will be submitted for create |

`request` example:

```json
{
  "table_bucket_arn": "arn:aws:s3tables:ap-southeast-1:<account>:bucket/ah-soc-delta-pilot",
  "namespace": "pilot",
  "table": "soc",
  "type_overrides": {"some_ambiguous_column": "STRING"},
  "deduplication_columns": ["pat_enc_csn_id", "visit_date"]
}
```

**Response 200**

```json
{
  "metrics": {
    "incoming_rows": 38889,
    "unique_composite_keys": 30416,
    "exact_duplicate_rows": 0,
    "conflicting_key_groups": 8388,
    "rows_in_conflicting_key_groups": 16861,
    "expected_retained_rows": 22028,
    "expected_skipped_rows": 16861
  },
  "deduplication_columns": ["time", "msg_date", "rep_index", "ward"],
  "acknowledgement_token": "<opaque-signed-token>",
  "expires_at": "2026-08-31T14:30:00+00:00",
  "no_storage_or_glue_side_effects": true,
  "analysis_basis": "raw-local-pre-sanitization"
}
```

This operation deliberately happens **before** sanitisation, encryption, S3
staging and Glue. It normalises column names but compares raw source row values.
It is therefore an analysis tool only; it does not retain input data.

The acknowledgement token expires after 30 minutes and is bound to the
effective user, destination, table, selected key, selected type overrides, and
SHA-256 digests of all submitted files. Any change requires another analysis.

Composite keys are fixed tuples. Empty components are allowed and retained as
explicit components: `(A,B,C,blank)` is different from `(blank,B,C,D)`.

### 5.4 Start create or append ingestion

#### `POST /api/ingestions`

Performs final server-side preflight, validates type/key contract, sanitises and
stages files, stores required metadata, ensures the Glue job definition, and
starts asynchronous Glue work.

**Content type:** `multipart/form-data`

| Form field | Type | Required | Notes |
|---|---|---:|---|
| `request` | JSON string | yes | `IngestionRequest` below |
| `files` | repeated file part | yes | One or more supported files |

`request` example for the first table creation:

```json
{
  "mode": "create",
  "table": "soc",
  "table_bucket_arn": "arn:aws:s3tables:ap-southeast-1:<account>:bucket/ah-soc-delta-pilot",
  "namespace": "pilot",
  "request_id": "d2b1c0fc-5f33-4f90-a51c-22f0cfb5a6f1",
  "reporting_month": "SOC June 2026",
  "type_overrides": {"some_ambiguous_column": "STRING"},
  "deduplication_columns": ["pat_enc_csn_id", "visit_date"],
  "key_analysis_token": "<token returned by /api/key-impact-analysis>"
}
```

For an append, send `mode: "append"`; `type_overrides` and
`deduplication_columns` should normally be omitted/empty because the saved
table contract controls them. `reporting_month` is now a free-form audit tag,
not a date field; it is required and accepts 1–256 characters.

**Response 200**

```json
{
  "job_run_id": "jr_...",
  "qc_uri": "s3://ah-data-analytics/temp_s3_update/qc/web/<run-id>/report.json",
  "request_id": "d2b1c0fc-5f33-4f90-a51c-22f0cfb5a6f1",
  "upload_id": "UPLOAD-...",
  "operation": "ingestion"
}
```

The response only means Glue was started. Poll the status endpoint until it
reaches a terminal state, then retrieve QC if appropriate.

**Create safeguards:** the table name must not already exist; selected types
must preserve every value; a non-empty deduplication key is required; and the
matching fresh key-impact acknowledgement is required.

**Append safeguards:** the table must have an uploader contract; schema match
and safe casts are rechecked; the saved type/key contract applies; and S3
Tables snapshot retention is configured before the job starts.

### 5.5 Job state and QC

#### `GET /api/ingestions/{job_run_id}?operation=ingestion|rollback`

Poll this endpoint after starting a create, append or rollback. `operation`
defaults to `ingestion`; pass `rollback` for rollback-specific user messages.

**Response 200**

```json
{
  "state": "RUNNING",
  "message": "ETL is in process: validating, snapshotting, and appending the uploaded data…",
  "error": null,
  "started": "2026-08-31 10:35:44+08:00",
  "completed": "None",
  "retention_configured": null,
  "retention_warning": null
}
```

`state` is the Glue job state such as `STARTING`, `RUNNING`, `SUCCEEDED`,
`FAILED`, `TIMEOUT`, or `STOPPED`. `retention_configured` is only populated
after a newly-created table succeeds, because the table does not exist earlier.
It can therefore legitimately be `null` while a job is running, on append,
or when no post-create action is needed.

#### `GET /api/qc?uri=s3://ah-data-analytics/temp_s3_update/qc/web/<run-id>/report.json`

Fetches the value-free QC report written by Glue. The service accepts only
URIs below its web-ingestion QC prefix; callers cannot use this endpoint as a
general S3 reader. The exact report varies by create/append/rollback but
contains counts, schema/key validation, conflict/duplicate metrics, snapshot
information, audit IDs and failure summary. It must not contain source row
values or identifiers.

### 5.6 Upload history and rollback

#### `GET /api/upload-history?table_bucket_arn=<arn>&namespace=<ns>&table=<name>`

Available only for uploader-managed tables. The server gives admins full
history; a non-admin sees only their own history entries.

**Response 200**

```json
{
  "table_bucket_arn": "arn:aws:s3tables:...",
  "namespace": "pilot",
  "table": "soc",
  "history": [
    {
      "upload_id": "UPLOAD-...",
      "reporting_month": "SOC June 2026",
      "filenames": "[\"SOC_202606.parquet.gzip\"]",
      "uploaded_by": "alice",
      "uploaded_at": "2026-08-31T10:00:00+00:00",
      "previous_snapshot_id": "...",
      "new_snapshot_id": "...",
      "rows_before": 1117856,
      "rows_uploaded": 20777,
      "rows_after": 1138633,
      "status": "SUCCESS",
      "rollback_at": null,
      "rollback_by": null,
      "error_message": null
    }
  ],
  "latest_rollback_upload_id": "UPLOAD-..."
}
```

Only the history entry whose ID equals `latest_rollback_upload_id` is eligible
for an enabled rollback control. Earlier successful entries must be shown as
disabled because rollback is strictly last-in-first-out.

#### `POST /api/rollbacks`

Starts an asynchronous Glue rollback.

**JSON request body**

```json
{
  "table_bucket_arn": "arn:aws:s3tables:ap-southeast-1:<account>:bucket/ah-soc-delta-pilot",
  "namespace": "pilot",
  "table": "soc",
  "upload_id": "UPLOAD-...",
  "confirm": true
}
```

**Response 200**

```json
{
  "job_run_id": "jr_...",
  "qc_uri": "s3://ah-data-analytics/temp_s3_update/qc/web/<run-id>/report.json",
  "upload_id": "UPLOAD-...",
  "operation": "rollback"
}
```

The user needs rollback permission. Admins may roll back the latest qualifying
upload in an authorized scope; non-admins can roll back only their own upload
and still only if it is the globally latest successful uploader update. The
initial load cannot be rolled back because there is no previous snapshot.

### 5.7 Delete an uploader-managed table

#### `DELETE /api/tables`

**JSON request body**

```json
{
  "table_bucket_arn": "arn:aws:s3tables:ap-southeast-1:<account>:bucket/ah-soc-delta-pilot",
  "namespace": "pilot",
  "table": "soc"
}
```

**Response 200**

```json
{
  "deleted": "soc",
  "table_bucket_arn": "arn:aws:s3tables:...",
  "namespace": "pilot"
}
```

This is permanent and requires an administrator. The reserved audit table and
external/browse-only tables cannot be deleted through this service.

## 6. Data and validation rules

### Canonical columns and initial schema

Every source column name is lowercased. Spaces, `/`, hyphens and parentheses
become underscores. Duplicated normalized names are deterministic:

```text
Treatment OU, Treatment_OU, treatment-ou
→ treatment_ou, treatment_ou_01, treatment_ou_02
```

The first upload defines the immutable table schema. Its type profiler scans
all populated values, not merely the first row:

| Source pattern | Initial contract type |
|---|---|
| Valid `YYYYMMDD`, `YYYY-MM-DD`, `YYYY.MM.DD` | `DATE` |
| Valid `YYYY-MM-DD HH:mm:ss` | `TIMESTAMP` |
| `HH:mm:ss` time-only value | `STRING` |
| Any numeric fractional value | `DOUBLE` |
| All numeric values integral | `BIGINT` |
| Clear categorical/text fields | `STRING` |
| Sensitive healthcare field | locked `STRING` |
| Mixed/unsupported genuinely ambiguous values | manual operator choice |

The type selector is only for real ambiguities. A user selection that would
discard values is rejected with invalid-value counts. Subsequent uploads always
project to the immutable established schema.

### Healthcare sanitisation

At actual ingestion, before S3 staging:

- patient names, dates of birth, phone/fax/contact and home-address fields are
  dropped;
- patient/encounter/CSN/case/bill/HRN/MRN/subsidy/document identifiers are
  AES-CBC encrypted as strings using the key from AWS Secrets Manager;
- age becomes a five-year band capped at `90+`;
- postal code retains its first two digits.

New encrypted values use `enc:v1:`. Recognised legacy ciphertext produced with
the same key is normalised by adding that prefix without another encryption
pass. The browser, audit data and QC must not receive encryption keys or raw
identifier values.

### De-duplication and conflicts

For newer uploader-managed tables, the first uploader selects one or more
columns as the immutable composite key and reviews impact before creation:

- Same composite key + identical full row: retain one, skip the rest as exact
  duplicates.
- Same composite key + any different non-key value: treat the group as a
  conflict and skip it; report counts and differing column names, not values.
- Append row with an existing target key: skip it; categorise it as an exact
  duplicate or conflict.
- Empty key components are allowed, position-preserving tuple components—not a
  reason to reject a legacy record.

Older tables without a stored key contract retain the legacy null-safe full-row
duplicate behaviour. Do not silently initialise a key contract for such a
table; that requires an explicit future migration decision.

## 7. End-to-end integration flow

1. Obtain verified identity in the existing UI/backend and pass only its
   trusted identity context to this service.
2. Call `/api/buckets`, then `/api/namespaces`, then `/api/tables`.
3. User selects an existing uploader-managed table for append, or selects
   `create` plus a valid new name.
4. Submit files to `/api/preflight`. Display accepted/rejected state,
   matching percentage, sanitisation summary and type choices.
5. For create, collect manual type selections and composite key. Call
   `/api/key-impact-analysis`. Show key and metrics first. Do not enable
   upload until the user explicitly acknowledges the returned analysis.
6. Submit the same files and final request JSON to `/api/ingestions`.
7. Poll `/api/ingestions/{job_run_id}` until terminal. On success, fetch its
   QC report. On failure, show the sanitized error and QC if one exists.
8. Refresh tables and history. Enable rollback only for the
   `latest_rollback_upload_id` returned by the server.

Changing selected bucket, namespace, table, files, manual type selection or
composite key must clear preflight, analysis acknowledgement, job state and
history from the current UI state. This avoids applying an acknowledgement to
the wrong destination or file.

## 8. Operational notes and known limitations

- Preflight/key analysis run in the FastAPI process, using local temporary
  files; large uploads therefore consume local CPU, disk and memory before
  Glue starts. Key analysis uses Polars and is intentionally raw/pre-
  sanitisation to keep it faster and free of AWS side effects.
- The actual upload path must perform sanitisation and Glue-compatible Parquet
  conversion locally before it stages the files. That work is necessary for
  privacy, immutable schema enforcement, Spark compatibility and reliable
  Glue input.
- Glue is the component that atomically creates/appends/restores Iceberg table
  state. Starting a job does not imply a successful data mutation.
- The generic web Glue job is separate from the original SOC delta job. Do not
  modify the overwrite-based legacy Lambda loader for this workflow.
- The service lists tables that it did not create, but keeps them browse-only
  because there is no saved uploader schema/key/recovery contract.
- The historical `web_ingest/sanitized_backups/` prefix has been removed from
  active code. Legacy objects, if any, were intentionally not deleted by this
  change and require an explicit cleanup decision.

## 9. Handoff checklist for the receiving backend engineer

- [ ] Confirm the deployment identity can list required S3 Table buckets and
  namespaces, read/write assigned tables, start/poll the Glue job, read/write
  the web-ingest/QC prefixes, manage the 30-day lifecycle rule, and retrieve
  the encryption secret.
- [ ] Replace `X-Pilot-User-Id` with verified identity and server-side grants.
- [ ] Disable `/api/dev/identity-profiles` outside local development.
- [ ] Set a non-default `PILOT_KEY_ANALYSIS_SECRET` consistently across service
  instances.
- [ ] Route authenticated UI requests to the service over TLS.
- [ ] Enforce upload/request-size limits at the ingress and service layer.
- [ ] Preserve multipart field names and JSON schemas exactly as documented.
- [ ] Treat `uploader_managed: false` tables as browse-only.
- [ ] Persist/calculate user bucket/namespace grants in the existing identity
  system; do not use frontend-provided role claims.
- [ ] Test create → append → history → latest-only rollback with a non-admin
  scoped user and an administrator.

## 10. Suggested future improvements

1. Move local preflight/staging to an asynchronous worker for very large
   files, with a durable request state instead of holding an HTTP request open.
2. Store access grants in the existing authorization service or database rather
   than `PILOT_USER_ACCESS_JSON`.
3. Add a controlled migration/initialisation workflow for existing external
   S3 Tables that need uploader contracts.
4. Add table-level request idempotency and user-visible resumable uploads.
5. Add metrics for local profiling duration, conversion duration, staging
   bytes, Glue queue time, Glue run duration and de-duplication outcomes.
6. Define a formal data-retention/cleanup action for any historical objects in
   discontinued prefixes.

# S3 Tables Uploader V3 — Deployment and API Integration Guide

**Current as of 2026-09-12, Asia/Singapore**
Implementation: `s3tables_uploader_v2/`
Stack: `s3-uploader-v2`, region `ap-southeast-1`
URL: `https://s3-uploader-v2.bot-alex.com`

This is the current V3 deployment and frontend contract. It supersedes prior
descriptions of multiple blocking dispatchers, API/worker-side Glue start,
durable S3 table-queue records, fixed three-bucket administrator access, or
legacy required Glue lock/queue arguments.

## Architecture

```text
Browser / existing web application
        |
        v
ALB + FastAPI/UI service (one 1 vCPU / 2 GiB task)
        |                         \
        |                          \-- S3 Tables control plane + skill bundles
        v
S3 durable state: versioned raw objects, sessions, leases, requests/status,
contracts, prepared artifacts/manifests, QC and history
        |
        +--> BASE / LARGE FIFO -> EventBridge Pipe -> leased Fargate worker
        |                                      profile, key analysis, preparation
        |
        +--> `s3-uploader-v3-mutations.fifo` -> one dispatcher -> Glue -> S3 Tables
```

S3 is the source of truth. SQS is still the durable queue. The dispatcher is a
custom SQS consumer/scheduler, not a replacement for SQS. No DynamoDB, EFS,
EKS or Lambda is used.

### Live release

| Item | Value |
| --- | --- |
| API | `s3-uploader-v2-api:30`, desired/running 1 |
| Dispatcher | `s3-uploader-v3-mutation-dispatcher:4`, desired/running 1 |
| Base worker | `s3-uploader-v2-worker:45`, 4096 CPU / 16384 MiB / 100 GiB ephemeral |
| Large worker | `s3-uploader-v2-worker:44`, 8192 CPU / 32768 MiB / 100 GiB ephemeral |
| API image | `964340114883.dkr.ecr.ap-southeast-1.amazonaws.com/s3-uploader-v2-api:20260912-single-async-dispatcher-amd64-1` |
| Worker / dispatcher image | `964340114883.dkr.ecr.ap-southeast-1.amazonaws.com/s3-uploader-v2-worker:20260912-review-samples-amd64-1` |
| Mutation queue | `s3-uploader-v3-mutations.fifo`; 120-second visibility; five receives before DLQ |
| Glue | `s3-uploader-v3-ingest`; Glue 5.0, 4 `G.1X`, `MaxConcurrentRuns=5` |

The two ECR images are Linux/amd64 two-stage builds with Azure Linux distroless
runtime images. The landing bucket physical name is CloudFormation-owned; read
it from stack outputs/task environment instead of hard-coding it.

## Concurrency and FIFO guarantee

Every create, append and rollback is first persisted in S3 as a
`MutationCommand`, then sent to one SQS FIFO queue. Its `MessageGroupId` is:

```text
SHA-256(table_bucket_arn + separator + namespace + separator + table)
```

The single 0.5 vCPU/1 GiB dispatcher tracks up to 50 received messages and may
start up to five Glue operations for different tables, matching the Glue quota.
It polls every 10 seconds, renews message visibility every 30 seconds and
retains every SQS receipt until the corresponding preparation/Glue operation is
terminal, its status is persisted and its owned table lock is released.

Consequences:

- A1, B1 and C1 can run at once when they are different tables and Glue has
  capacity.
- A2 is withheld by SQS until A1 has terminal state and the dispatcher deletes
  A1’s receipt.
- A same-table request is queued, never rejected as “table busy”.
- On explicit Glue capacity errors the command returns to `READY_FOR_MUTATION`;
  its message remains durable for retry. Glue run queuing is enabled as a
  second account-quota guard.
- A long same-table queue may delay later message receipt in the one physical
  queue. This is an intentional simplicity trade-off, not loss of correctness.

The dispatcher conditionally acquires an S3 lock immediately before Glue:

```text
s3-uploader-v2/table-locks/<table-identity-hash>.json
```

The lock includes operational owner/request/session data only. Conditional S3
writes/ETags control acquisition, renewal, release and stale takeover. Startup
reconciles existing locks into the Glue-slot count, so an ECS restart cannot
over-admit work. The mutation DLQ alarms on any visible message.

New Glue invocations omit `--LOCK_*` and `--QUEUE_*`. They are optional only
for an in-flight legacy invocation. This prevents the historical
`GlueArgumentError: argument --QUEUE_KEY: expected one argument` failure.

## AWS services and required permissions

`infra/s3_uploader_v2_fargate.py` renders the stack. It owns:

| Service | V3 responsibility |
| --- | --- |
| ECS Fargate | one API/UI service, disposable base/large worker tasks, one dispatcher service |
| ECR | API and worker/dispatcher images |
| S3 | encrypted/versioned source and durable state; contracts/history/skills |
| SQS FIFO + DLQ | base/large worker dispatch and ordered mutations |
| EventBridge Pipes | SQS-to-one-shot worker launch |
| Glue | S3 Tables/Iceberg mutation, QC and audit writes |
| S3 Tables | table buckets, namespaces, tables and Iceberg metadata |
| CloudWatch | worker/dispatcher/Glue logs and mutation-DLQ alarm |
| ALB, Route 53, ACM | HTTPS listener and hostname |
| Secrets Manager | login password and signing secret |

IAM boundaries:

- API: its S3 state prefixes, mutation send, authorised S3 Tables control-plane
  actions, contracts/history and safe skill paths.
- Workers/dispatcher: required landing/state paths, mutation queue receive /
  delete / visibility, Glue start/get, and restricted `ListBucket` access for
  both `s3-uploader-v2/table-locks` and `s3-uploader-v2/table-locks/*`.
- Glue: staging, S3 Tables/Iceberg, QC and history. It does not own current SQS
  receipts or dispatcher locks.
- `s3tables:CreateTableBucket` is account scoped because a new bucket ARN does
  not yet exist; other S3 Tables actions stay bucket/table scoped.

Do not add a broad account-administrator policy to fix an access symptom;
identify the exact S3/S3 Tables/Glue action and resource first.

## Build, test and deploy

Build a fresh immutable tag for every release. From the repository root:

```bash
TAG=YYYYMMDD-description-amd64-N

docker buildx build --platform linux/amd64 --load \
  -f s3tables_uploader_v2/Dockerfile.api \
  -t local/s3-uploader-v3-api:$TAG .
docker buildx build --platform linux/amd64 --load \
  -f s3tables_uploader_v2/Dockerfile.worker \
  -t local/s3-uploader-v3-worker:$TAG .

docker run --rm local/s3-uploader-v3-worker:$TAG \
  python3 -m unittest discover -s s3tables_uploader_v2/tests
python3 -m unittest \
  s3tables_uploader_v2.tests.test_mutation_dispatcher \
  s3tables_uploader_v2.tests.test_api_jobs \
  infra.tests.test_s3_uploader_v2_fargate
git diff --check
```

Deploy in this order:

1. Publish tested API and worker images to their ECR repositories under the
   immutable tag; capture both manifest digests.
2. Upload `s3tables_uploader_v2/glue_job.py` to the V3 Glue script URI.
3. Deploy the CloudFormation stack with those image URIs. Keep
   `MutationDispatcherService.DesiredCount` at **one**.
4. Wait for CloudFormation `UPDATE_COMPLETE`, then verify both ECS services
   desired/running 1, their active task definitions/images and `GET /healthz`.
5. Check dispatcher logs, FIFO/DLQ metrics and stale table locks.
6. Smoke-test a disposable create/append, an eligible rollback, concurrent
   different-table work and ordered same-table work.

When changing task environment entries, locate the variable by its `Name`; do
not update an array element by position. A previous positional change set the
numeric dispatcher poll setting to a Glue job name and terminated the task.

## Authentication and UI identity

The existing signed login cookie is required for all API requests. The pilot
panel obtains three profiles through `GET /api/dev/identity-profiles` and sends
only `X-Pilot-User-Id`. `GET /api/identity` resolves effective permissions:

- `local-admin` sees every customer bucket in the current account and can
  create buckets/namespaces/delete uploader tables.
- `local-editor` sees only configured assignment(s), with history and rollback.
- `local-unassigned` receives a deny response.

For production integration, replace the development header with a trusted
identity mapping at the API boundary. Do not let a client provide admin flags,
bucket lists, history or rollback grants.

## API integration contract

All calls are same-origin, authenticated and JSON unless marked multipart.
FastAPI failures are `{ "detail": "..." }`; expect `401/403` for access,
`409` for state conflicts and `422` for request validation. Persist returned
session/job/mutation IDs and poll durable state—not ECS task state.

### Identity and destinations

| Endpoint | Request | Result |
| --- | --- | --- |
| `GET /api/identity` | — | Effective user, capabilities, scope mode and buckets. |
| `GET /api/buckets` | — | Buckets visible to user. |
| `POST /api/buckets` | `{ "name":"new-bucket" }` | `201`, bucket ARN; admin only. |
| `GET /api/namespaces?table_bucket_arn=...` | query | Namespaces for authorised bucket. |
| `POST /api/namespaces` | bucket ARN + `namespace` | `201`; admin only. |
| `GET /api/tables?table_bucket_arn=...&namespace=...` | query | Cards with timestamps, metadata row count, managed flag and locked key. Format time as `Asia/Singapore`. |
| `DELETE /api/tables` | bucket ARN, namespace, table | Admin managed-table delete; `409 TABLE_MUTATION_IN_PROGRESS` if locked. |

### Warm worker lease

On every file selection, call:

```http
POST /api/v3/worker-leases
Content-Type: application/json

{"files":[{"name":"source.parquet","size_bytes":104857600}]}
```

The `201` response has `lease_id`, `worker_state`, `worker_size`,
`routing_score`, `routing_reason`, `expires_at` and `can_retry_large`.

| Endpoint | Integration rule |
| --- | --- |
| `PUT /api/v3/worker-leases/{lease_id}` | New selection. Same tier: `reused:true`; changed tier: replacement lease and `replaced:true`. |
| `DELETE /api/v3/worker-leases/{lease_id}` | Cancel unattached selection; returns `204`. |
| `POST /api/v3/worker-leases/{lease_id}/retry-large` | `202`; owner-only after recognised base resource failure. |

The backend recalculates routing; never submit frontend-selected worker size.
If warm-up JavaScript fails, normal upload-session creation still creates a
lease as compatibility fallback.

### V1-compatible upload session

The static V3 UI submits a multipart form:

```http
POST /api/v2/upload-sessions
Content-Type: multipart/form-data

mode=create|append
table_bucket_arn=arn:aws:s3tables:...
namespace=...
table=...
worker_lease_id=<optional lease from selection>
files=<one or more files>
```

Poll `GET /api/v2/upload-sessions/{session_id}`. Its durable response includes
phase, progress/error, review/preflight, key-impact, ingestion job and safe
worker-lease fields. Do not wait for worker startup before upload.

The `review`/`preflight` payload exposes `sample_values` (at most five short,
non-empty examples) and `samples_masked` for de-duplication and optional manual
encryption candidates. Render examples only when `samples_masked` is `false`.
Automatically protected healthcare fields, including detected NRIC columns,
must remain masked; their values and quality counts are not returned. Never
reuse review examples in a manifest, audit/history view, or Glue request.

| Endpoint | Request / expected behavior |
| --- | --- |
| `POST /api/v2/upload-sessions/{id}/key-impact` | `{ "deduplication_columns":[...], "type_overrides":{} }`; returns `202`; poll session. |
| `POST /api/v2/upload-sessions/{id}/ingestions` | `request_id`, reporting month, dedup mode/columns, impact token when a new key is chosen, type overrides, manual encryption fields and temporal acknowledgement where applicable. Returns `202 {session_id,job_id,phase:"QUEUED"}`. |
| `GET /api/v2/jobs/{job_id}` | Owner-only immutable request and durable job status. |
| `DELETE /api/v2/upload-sessions/{id}` | Owner-only discard/cancel. |

For a configured table key, hide the selector and do not send replacement key
choices. The API derives the available source subset. For an unconfigured key,
the first selected key must have valid impact acknowledgement before it is
saved permanently.

### History and rollback mutation

`GET /api/upload-history?table_bucket_arn=...&namespace=...&table=...` returns
history and `latest_rollback_upload_id`. Only that latest successful update
with a previous snapshot is eligible.

```http
POST /api/rollbacks
Content-Type: application/json

{
  "table_bucket_arn":"arn:aws:s3tables:...",
  "namespace":"pilot",
  "table":"target_table",
  "upload_id":"UPLOAD-...",
  "confirm":true
}
```

This returns `202` with `mutation_id`, `phase`, `status_url`, `upload_id` and
`operation:"rollback"`. Poll `GET /api/mutations/{mutation_id}`, which returns
owner-scoped `{ mutation, status }`, including durable phase/message, Glue run
ID and error code. Refresh cards/history only after success. The same
owner/table/upload request is idempotent and reconnects to its original command.

### Skill bundle APIs

| Endpoint | Request | Result |
| --- | --- | --- |
| `GET /api/skills/files?table_bucket_arn=...` | query | Safe relative-path tree and S3 destination URI. |
| `POST /api/skills/files` | multipart `table_bucket_arn`, `paths_json`, repeated `files` | Add/overwrite safe files. |
| `GET /api/skills/files/download?table_bucket_arn=...&path=...` | query | Attachment stream. |
| `DELETE /api/skills/files` | `{ "table_bucket_arn":"...", "path":"...", "confirm":true }` | Confirmed deletion. |

## Frontend polling and operation display

Use 2–5 second polling for an active session/mutation, reducing frequency after
30 seconds. Recover the saved session ID on page reload and resume polling.

| State | User-facing meaning |
| --- | --- |
| `RECEIVED`, `PROFILING` | Source stored; worker is starting/profiling. |
| `READY_FOR_REVIEW` | Render schema/sanitisation review. |
| `KEY_ANALYSING`, `READY_FOR_ACKNOWLEDGEMENT` | Render/wait for key impact; acknowledge only a new locked key. |
| `QUEUED`, `PREPARING`, `READY_FOR_MUTATION` | Preparing or waiting in FIFO/for Glue capacity. Continue polling; do not resubmit. |
| `STARTING_GLUE`, `RUNNING_GLUE` | Dispatcher owns the mutation; show durable message. |
| `SUCCEEDED`, `FAILED` | Terminal; refresh cards/history only after success. |
| `RESOURCE_LIMIT_EXCEEDED` | Offer `Retry using large worker` to owner. |

An ECS worker stopping can be normal completion, cancellation or expiry. It is
not a UI status source. Start diagnosis from the durable session/job/mutation
record, then worker/dispatcher logs, Glue run/log, QC/manifest and Iceberg
snapshot/history.

## Release validation checklist

- Routing boundaries and combined multi-file scores match backend decisions.
- Same-tier selection edits reuse a lease; tier changes replace exactly once.
- Multi-file name sets match while type drift is tolerated and projected by
  contract.
- Profile, repeated key analysis and preparation use the same leased worker.
- Locked-key subset/no-key semantics preserve the immutable de-dup contract.
- Same-table operations are FIFO; different tables use up to five Glue slots.
- Dispatcher restart, visibility renewal, capacity wait and ambiguous Glue
  start cannot create a duplicate mutation.
- History/eligible rollback, metadata row cards, Singapore timestamps, skill
  routes, all-admin discovery and scoped-editor denial work.
- Monitor API health, mutation DLQ depth, FIFO queue age, stale-lock age,
  resource-limit events, Glue failure/timeout and unexpected worker cost.

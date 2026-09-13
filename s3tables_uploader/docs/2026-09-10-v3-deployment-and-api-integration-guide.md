# S3 Tables Uploader 3.0 — Deployment and API Integration

**Implementation:** `s3tables_uploader/`
**CloudFormation renderer:** `infra/s3_uploader_fargate.py`
**Public base URL:** `https://s3-uploader-v2.bot-alex.com`
**Active API namespace:** `/api/v3` (there is no `/api/v2` compatibility route)

## Published production release

The blue-green cutover completed on 13 September 2026. The active stack is
`s3-uploader-production`; the retained former `s3-uploader-v2` consumers are
disabled and must remain so until their seven-day retention window ends.

| Component | Immutable image |
| --- | --- |
| API | `s3-uploader-api:20260914-skill-bundle-access-amd64-1` (`sha256:d52c5b8eaa996421624554f742fae9ed9f70c645e4e4516ce1d46a5578150dcb`) |
| Worker and dispatcher | `s3-uploader-worker:20260914-numeric-contract-cast-amd64-1` (`sha256:d637a3234ba3e33927c92f604e6d0a754a8269f2043cf4476f59d380cca449a7`) |

The worker image passed Docker Scout with zero detected critical, high, medium,
or low vulnerabilities. Docker Scout was unavailable for the 14 September API
release because scanning would have transmitted the private application image
to Docker's external service; no scan result is claimed for that API image. The
production API and dispatcher each run one healthy task; both named DLQ alarms
were `OK` at cutover. Verify those facts again before any later infrastructure
update rather than treating this record as live monitoring.

## Production architecture

```text
Browser / existing web app
        | authenticated same-origin HTTPS
        v
API service (ECS/Fargate) ---- S3 Tables control plane, contracts, skills
        |       \ durable sessions, status, source versions, manifests
        |        v
        |    existing encrypted/versioned S3 landing bucket
        v
s3-uploader-base.fifo / s3-uploader-large.fifo
        | EventBridge Pipes
        v
disposable leased Fargate worker
        | profile -> key impact -> sanitise/prepare
        v
s3-uploader-mutations.fifo -> one permanent dispatcher -> Glue -> S3 Tables
```

The base and large FIFO queues are EventBridge Pipe sources. The mutation queue
is not: the one dispatcher service long-polls it and applies global order,
per-table serialisation, Glue quota control, lock recovery, and idempotent Glue
submission. SQS contains only IDs, never source data or review samples.

## Stack inputs and resources

Deploy the parallel `s3-uploader-production` stack with the existing landing
bucket passed by name and ARN. The stack intentionally does **not** create,
replace, empty, or delete that bucket. The first deployment creates a
non-matching `HostRuleHostname=s3-uploader-production.invalid`, which attaches
the target group without accepting public traffic. After smoke tests, update it
to the public hostname at priority `48999`, which takes precedence over the
retained old `49000` rule. Disable old queue consumers before that update.

Required CloudFormation parameters are documented in
[`infra/s3_uploader_fargate.parameters.example.json`](../../infra/s3_uploader_fargate.parameters.example.json).

The stack creates:

- ECR image consumers: `s3-uploader-api`, `s3-uploader-worker`; the dispatcher
  uses the worker image with `python3 -m s3tables_uploader.mutation_dispatcher`.
- ECS families `s3-uploader-api`, `s3-uploader-worker`, and
  `s3-uploader-mutation-dispatcher`.
- FIFO queues `s3-uploader-base.fifo`, `s3-uploader-large.fifo`, and
  `s3-uploader-mutations.fifo`.
- `s3-uploader-worker-launch-dlq.fifo` shared by base/large queues and
  `s3-uploader-mutations-dlq.fifo` for the dispatcher, each with a distinct
  zero-visible-message CloudWatch alarm.
- two EventBridge Pipes to launch disposable workers, one API service, one
  dispatcher service, a stack-managed Glue job and least-privilege Glue role.

The Glue job gets the target table-bucket ARN only in dispatcher-validated
arguments. It has no warehouse default and no dependency on the retired
`ah-soc-delta-pilot-glue-role`.

### Runtime configuration

Every active API/worker/dispatcher configuration is version-neutral:

```text
S3_UPLOADER_LANDING_BUCKET
S3_UPLOADER_LANDING_PREFIX=s3-uploader
S3_UPLOADER_BASE_QUEUE_URL
S3_UPLOADER_LARGE_QUEUE_URL
S3_UPLOADER_MUTATION_QUEUE_URL
S3_UPLOADER_LOGIN_PASSWORD
S3_UPLOADER_LOGIN_SECRET
S3_UPLOADER_COOKIE_SECURE=true
S3_UPLOADER_SESSION_TTL_SECONDS
S3_UPLOADER_RAW_RETENTION_DAYS
S3_UPLOADER_API_BASE_URL=https://s3-uploader-v2.bot-alex.com
S3_UPLOADER_GLUE_JOB_NAME=s3-uploader-ingest
S3_UPLOADER_CONTRACT_BUCKET=ah-data-analytics
S3_UPLOADER_CONTRACT_PREFIX=temp_s3_update/web_ingest/table_contracts
S3_UPLOADER_SKILL_BUNDLE_BUCKET=agentcore-harness-dev
S3_UPLOADER_SKILL_BUNDLE_PREFIX=skills
S3_UPLOADER_JOB_ID                 # Pipe override for a disposable worker only
```

The API fails startup if its landing prefix, all three queues, login secrets,
API base URL, Glue job, or contract location is absent. There is no legacy
queue fallback or leases feature flag.

### Skill bundle storage

The non-versioned `/api/skills/files` endpoints store only the selected table
bucket's skill bundle under
`s3://agentcore-harness-dev/skills/<table-bucket-name>/`. The API task role is
limited to listing that `skills/` prefix and reading, writing, or deleting its
objects. The worker, dispatcher, and Glue roles have no skill-bundle access.
The `S3_UPLOADER_SKILL_BUNDLE_*` variables are the only supported runtime
configuration; legacy `PILOT_SKILL_BUNDLE_*` variables are ignored.

## Build, test, and blue-green release

Use immutable tags and Linux/amd64 two-stage Azure Linux distroless images.

```bash
TAG=YYYYMMDD-description-amd64-N
docker buildx build --platform linux/amd64 --load -f s3tables_uploader/Dockerfile.api -t local/s3-uploader-api:$TAG .
docker buildx build --platform linux/amd64 --load -f s3tables_uploader/Dockerfile.worker -t local/s3-uploader-worker:$TAG .

uv --cache-dir /tmp/s3-uploader-uv-cache tool run \
  --with-requirements s3tables_uploader/requirements.txt \
  pytest -q s3tables_uploader/tests infra/tests/test_s3_uploader_fargate.py
python3 infra/s3_uploader_fargate.py > /tmp/s3-uploader-production-template.json
aws cloudformation validate-template --template-body file:///tmp/s3-uploader-production-template.json
```

1. Scan and publish the tested API and worker tags to the neutral ECR
   repositories. Record the digests.
2. Upload `s3tables_uploader/glue_job.py` to the `GLUE_SCRIPT_URI` defined in
   the renderer.
3. Create/update the parallel stack with host routing disabled.
4. Wait for ECS API/dispatcher desired and running counts of one; call its
   target-group health endpoint and inspect worker/dispatcher logs.
5. Smoke-test create, append, eligible rollback, cancelled pre-acceptance
   upload, same-table FIFO, and concurrent different-table uploads.
6. Verify all queue/DLQ depths, status events, Glue arguments, history, and
   rollback through the new target group.
7. Disable old queue consumers, update `HostRuleHostname` to the public
   hostname, and repeat the smoke tests through it.
8. Retain the disabled old stack, task definitions, queues and images seven
   days. Preserve S3 recovery objects, contracts, CloudWatch logs, and audit
   history; only then remove obsolete runtime resources.

## Frontend API contract

All endpoints are same-origin and require the signed login cookie. Responses
use JSON except multipart file submission. Failure payloads are
`{"detail":"..."}`. Persist identifiers and poll durable status; never infer
completion from an ECS task lifecycle.

### Identity, bucket, and table endpoints

| Endpoint | Method | Use |
| --- | --- | --- |
| `/api/identity` | GET | Current effective profile and capabilities. |
| `/api/dev/identity-profiles` | GET | Pilot identity-emulation choices only. |
| `/api/buckets` | GET | Authorised S3 Tables buckets. |
| `/api/buckets` | POST | Admin creates table bucket: `{name}`. |
| `/api/namespaces` | GET/POST | List or admin-create namespace. |
| `/api/tables` | GET/DELETE | List cards or admin-delete managed table. |
| `/api/upload-history` | GET | Per-table audit/history projection. |
| `/api/rollbacks` | POST | Admin/authorised confirmed rollback. |
| `/api/skills/files` | GET/POST/DELETE | Skill list, upload, confirmed delete. |
| `/api/skills/files/download` | GET | Skill file download. |

Use Singapore timezone (`Asia/Singapore`) when rendering card and history
timestamps. Do not calculate table row counts in the browser.

### Worker lease and file selection

```http
POST /api/v3/worker-leases
Content-Type: application/json

{"files":[{"name":"source.parquet","size_bytes":104857600}]}
```

The API returns a `lease_id`, `worker_state`, fixed `worker_size`, routing
score/reason, expiry and retry eligibility. On a changed selection call
`PUT /api/v3/worker-leases/{lease_id}`. Same size reuses the idle worker;
different size returns a replacement lease. `DELETE` cancels an unattached
lease. `POST /api/v3/worker-leases/{lease_id}/retry-large` is owner-only after
a recognised base-worker resource limit.

### Upload lifecycle

1. Submit files with `POST /api/v3/upload-sessions` as multipart fields:
   `mode`, `table_bucket_arn`, `namespace`, `table`, optional
   `worker_lease_id`, and one or more `files`.
2. Poll `GET /api/v3/upload-sessions/{session_id}` until review is ready.
3. For a table without a locked key, call
   `POST /api/v3/upload-sessions/{id}/key-impact` with selected columns and
   poll the session for the acknowledgement token. Hide the selector for every
   table with a locked key.
4. Submit `POST /api/v3/upload-sessions/{id}/ingestions` with an idempotent
   `request_id`, reporting month, deduplication mode/columns only when a key
   is being set, key-impact token, optional type/manual-encryption choices and
   required temporal acknowledgement. The response includes `job_id`.
5. Poll `GET /api/v3/jobs/{job_id}` until a terminal status.

Before ingestion acceptance, `DELETE /api/v3/upload-sessions/{id}` provides
**Cancel & Start Over** and resets the UI. Once an ingestion request succeeds,
hide/disable that control permanently for the session.

Status fields include worker `STARTING`, `AWAITING_UPLOAD`, `PROFILING`,
`AWAITING_KEY`, `ANALYSING_KEY`, `AWAITING_CONFIRMATION`, `PREPARING`,
`STARTING_GLUE`, `COMPLETED`, `RESOURCE_LIMIT_EXCEEDED`, `FAILED`, `EXPIRED`,
and `CANCELLED`. Treat unknown transient states as pollable, not terminal.

## DLQ recovery

Never automatically redrive a DLQ message. First inspect CloudWatch logs plus
the S3 command/request/status and verify that no Glue run has started. Correct
the image, permissions, malformed state, or AWS quota first. For a safe worker
launch retry, recreate/renew the lease through the API. For a mutation retry,
verify idempotency, table lock and Glue run ambiguity, then redrive one message
while watching the matching job status.

The historical S3 prefix is read-only. New status, recovery and mutation
records always belong under `s3-uploader/`.

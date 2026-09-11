# S3 Tables Uploader V3 — Deployment and API Integration Guide

Updated: 2026-09-10 (Asia/Singapore)
Audience: AWS operators and frontend engineers
Base URL: https://s3-uploader-v2.bot-alex.com

## Architecture

~~~text
Browser/existing application
  -> ALB HTTPS host rule -> small API Fargate service (1 vCPU / 2 GiB)
  -> encrypted/versioned landing S3 and FIFO worker queues
  -> EventBridge Pipe -> one fixed-size leased Fargate worker
  -> prepared Parquet, manifest and durable status in S3
  -> Glue s3-uploader-v3-ingest -> S3 Tables/Iceberg + audit/QC/history
~~~

The base worker is 4 vCPU/16 GiB and the large worker is 8 vCPU/32 GiB; both
have 100 GiB ephemeral storage. The API does not parse user datasets. One
worker remains alive through the human review/key choices to reuse local cached
immutable sources. S3 remains authoritative if it must be recreated.

## AWS services to set up

The source of truth is infra/s3_uploader_v2_fargate.py; render it rather than
manually maintaining a divergent CloudFormation template.

### Inputs to provide

| Parameter | Purpose |
| --- | --- |
| ClusterArn | Existing ECS cluster, currently embedded-web-app |
| VpcId, PrivateSubnets | Fargate placement |
| ExistingAlbSecurityGroupId | ALB-to-API port 8090 ingress |
| AlbListenerArn, AlbDnsName, AlbCanonicalHostedZoneId | HTTPS route target |
| HostedZoneId, CertificateArn | DNS alias and TLS |
| LoginPasswordSecretArn, LoginSigningSecretArn | Login password and HMAC signing secret (32+ chars) |
| EncryptionSecretArn | Worker encryption material |
| ApiImageUri, WorkerImageUri | Immutable linux/amd64 ECR image URIs |
| EnableV3Leases | true in production |

### Stack-created services

| Service | Required configuration |
| --- | --- |
| S3 landing bucket | Versioning, SSE-KMS, public access blocked, one-day raw lifecycle, 30-day jobs lifecycle |
| SQS FIFO | default jobs, base leases, large leases, and 14-day DLQ; 3,600-second visibility, redrive after two receives |
| EventBridge Pipes | one per queue, batch size 1, target worker task and S3_UPLOADER_V2_JOB_ID=$.body override |
| ECS/Fargate | API service desired count 1; task definitions for base/large/default worker |
| ALB/Route 53 | target group health check /healthz, certificate, host rule and alias A for s3-uploader-v2.bot-alex.com |
| IAM | distinct API, worker, execution and Pipe roles; least privilege; Glue deletes only its managed table-lock prefix |
| CloudWatch | API and worker log groups, 30-day retention |
| Glue | V3 job s3-uploader-v3-ingest, Glue 5.0, 4 G.1X, timeout 60 minutes, max concurrency 5 |

The API role needs landing multipart/session/lock S3, SQS send, Glue
start/status, contracts/history reads, and S3 Tables control/data access. The
worker role needs landing/contract S3, the encryption secret, Glue start, and
lock release only when Glue could not start. The Glue role has
`s3:DeleteObject` only for `s3-uploader-v2/table-locks/*` in the generated
landing bucket so its terminal handler can release the lock it was passed. Pipe
role needs queue receive/delete, ECS RunTask and worker-role PassRole. Add
exact missing action/ARN pairs from CloudWatch errors; never solve AccessDenied
with unbounded S3 or administrator permissions.

## Deployment runbook

~~~bash
cd /Users/jinxin/Documents/AgentCore
TAG=YYYYMMDD-v3-change-1
REGISTRY=964340114883.dkr.ecr.ap-southeast-1.amazonaws.com

aws ecr get-login-password --region ap-southeast-1 \
 | docker login --username AWS --password-stdin "$REGISTRY"
docker build --platform linux/amd64 -f s3tables_uploader_v2/Dockerfile.api \
 -t "s3-uploader-v2-api:$TAG" .
docker build --platform linux/amd64 -f s3tables_uploader_v2/Dockerfile.worker \
 -t "s3-uploader-v2-worker:$TAG" .
docker run --rm -v "$PWD:/workspace" -w /workspace -e PYTHONPATH=/workspace \
 "s3-uploader-v2-worker:$TAG" python -m unittest discover -s s3tables_uploader_v2/tests -v
node --check s3tables_uploader_v2/static/app.js
git diff --check

docker tag "s3-uploader-v2-api:$TAG" "$REGISTRY/s3-uploader-v2-api:$TAG"
docker tag "s3-uploader-v2-worker:$TAG" "$REGISTRY/s3-uploader-v2-worker:$TAG"
docker push "$REGISTRY/s3-uploader-v2-api:$TAG"
docker push "$REGISTRY/s3-uploader-v2-worker:$TAG"

python3 -B infra/s3_uploader_v2_fargate.py > /private/tmp/s3-uploader-v3.json
~~~

Create a CloudFormation update change set using all existing parameters, set
EnableV3Leases=true, and replace only ApiImageUri and WorkerImageUri with the
new immutable tags. Review it before execution. For an image-only change,
expected resources are API service/task definition, worker task definitions and
their EventBridge Pipes. Stop for unexpected DNS, S3 lifecycle/deletion, Glue
or IAM widening changes.

Copy-ready change-set command:

~~~bash
aws cloudformation create-change-set --region ap-southeast-1 \
 --stack-name s3-uploader-v2 --change-set-name "v3-$TAG" --change-set-type UPDATE \
 --template-body file:///private/tmp/s3-uploader-v3.json \
 --capabilities CAPABILITY_NAMED_IAM \
 --parameters \
 ParameterKey=ClusterArn,UsePreviousValue=true \
 ParameterKey=VpcId,UsePreviousValue=true \
 ParameterKey=PrivateSubnets,UsePreviousValue=true \
 ParameterKey=AlbListenerArn,UsePreviousValue=true \
 ParameterKey=AlbDnsName,UsePreviousValue=true \
 ParameterKey=AlbCanonicalHostedZoneId,UsePreviousValue=true \
 ParameterKey=HostedZoneId,UsePreviousValue=true \
 ParameterKey=CertificateArn,UsePreviousValue=true \
 ParameterKey=LoginPasswordSecretArn,UsePreviousValue=true \
 ParameterKey=LoginSigningSecretArn,UsePreviousValue=true \
 ParameterKey=EncryptionSecretArn,UsePreviousValue=true \
 ParameterKey=ExistingAlbSecurityGroupId,UsePreviousValue=true \
 ParameterKey=EnableV3Leases,ParameterValue=true \
 ParameterKey=ApiImageUri,ParameterValue="$REGISTRY/s3-uploader-v2-api:$TAG" \
 ParameterKey=WorkerImageUri,ParameterValue="$REGISTRY/s3-uploader-v2-worker:$TAG"

aws cloudformation wait change-set-create-complete --region ap-southeast-1 \
 --stack-name s3-uploader-v2 --change-set-name "v3-$TAG"
aws cloudformation describe-change-set --region ap-southeast-1 \
 --stack-name s3-uploader-v2 --change-set-name "v3-$TAG"
~~~

~~~bash
aws cloudformation execute-change-set \
 --region ap-southeast-1 --stack-name s3-uploader-v2 --change-set-name "v3-$TAG"
aws cloudformation wait stack-update-complete \
 --region ap-southeast-1 --stack-name s3-uploader-v2
curl -fsS https://s3-uploader-v2.bot-alex.com/healthz
~~~

After execution, verify the API's primary task definition, both worker image
URIs, all Pipe states, and the health response. Do not stop active leased
workers merely because a new revision exists; let them complete, cancel, or
expire safely.

## Authentication contract

- /login and /healthz are public; all other routes require cookie
  s3_uploader_v2_session.
- Login accepts an HTML form or JSON with password.
- Use credentials: include on every frontend fetch request.
- The production cookie is Secure, HttpOnly and SameSite=Strict.
- Current identity is a shared administrator; do not infer future editor
  permissions from browser-supplied roles.

~~~js
async function requireJson(response) {
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || "HTTP request failed");
  return body;
}
await requireJson(await fetch("/login", {
  method: "POST", credentials: "include",
  headers: {"Content-Type": "application/json"},
  body: JSON.stringify({password})
}));
~~~

## API integration: recommended browser flow

~~~text
identity -> file selection lease -> multipart review upload -> session poll
-> optional key impact -> ingestion -> session/Glue poll -> history refresh
~~~

Never calculate worker size, schema, sanitisation, type compatibility or locked
key availability in the browser. Render the worker/API result instead.

### Discovery and table APIs

| Method/path | Request | Result |
| --- | --- | --- |
| GET /api/identity | — | User/capabilities/bucket scope |
| GET /api/buckets | — | All discoverable table buckets |
| GET /api/skills/files?table_bucket_arn=... | query | Optional bundle files for the selected bucket |
| GET /api/namespaces?table_bucket_arn=... | query | Namespace list |
| POST /api/namespaces | table_bucket_arn, namespace JSON | Created namespace |
| GET /api/tables?table_bucket_arn=...&namespace=... | query | Cards with timestamps, row_count, managed/key info |
| DELETE /api/tables | table_bucket_arn, namespace, table JSON | Delete uploader-managed table |

Format card timestamps only for display using Asia/Singapore; retain server ISO
values for state. Example:

~~~js
new Intl.DateTimeFormat("en-SG", {
  dateStyle:"medium", timeStyle:"medium", timeZone:"Asia/Singapore"
}).format(new Date(iso))
~~~

### Selection-time V3 lease APIs

Create: POST /api/v3/worker-leases

~~~json
{"files":[{"name":"January.parquet","size_bytes":104857600}]}
~~~

Save lease_id. The response includes worker_state, worker_size, routing_score,
routing_reason, expires_at, and can_retry_large.

Update: PUT /api/v3/worker-leases/{lease_id}

Send the same files body whenever selection changes before Review. A same-tier
result contains reused:true and the same ID. A route-size change contains
replaced:true, replaced_lease_id, and a new lease_id; replace local state.

Cancel: DELETE /api/v3/worker-leases/{lease_id} returns 204 for an idle lease.
Call it when selection becomes empty.

Manual retry: POST /api/v3/worker-leases/{lease_id}/retry-large returns 202
only after server-recognised RESOURCE_LIMIT_EXCEEDED with can_retry_large:true.
A 409 means retry is unsafe/unavailable; do not retry Glue from JavaScript.

Warm-up is non-blocking: if lease creation fails, Review can omit
worker_lease_id; the API creates a fallback lease. Do not create a new lease on
every file-input event when an existing idle lease can be PUT-updated.

### Multipart review session

POST /api/v2/upload-sessions accepts multipart/form-data:

| Field | Required | Value |
| --- | --- | --- |
| mode | yes | create or append |
| table_bucket_arn | yes | Destination S3 Tables bucket ARN |
| namespace, table | yes | Lowercase destination identifiers |
| files | yes, repeated | Parquet, Parquet GZIP, CSV, TSV, XLSX or XLS |
| worker_lease_id | recommended | Current V3 lease ID |

~~~js
const form = new FormData();
form.set("mode", "append"); form.set("table_bucket_arn", bucketArn);
form.set("namespace", namespace); form.set("table", table);
if (state.leaseId) form.set("worker_lease_id", state.leaseId);
for (const file of input.files) form.append("files", file, file.name);
const session = await requireJson(await fetch("/api/v2/upload-sessions", {
  method:"POST", credentials:"include", body:form
}));
~~~

The API uploads into encrypted/versioned landing S3 using bounded 8 MiB parts,
computes per-file SHA-256, creates a durable session and attaches a valid lease.
Poll GET /api/v2/upload-sessions/{session_id} every 2–5 seconds. Render phase,
progress_message, error, preflight, key_impact, ingestion, and worker_lease;
404 means expired/deleted, 403 means wrong user/session.

Preflight is the source of truth. Enable ingestion only when accepted is true.
Show target_schema, per-file warnings/sanitisation, and
multi_file_schema.type_conflicts_stored_as_string as informational output.
Do not reject a matching-column multi-file selection just because source types
differ. Hide the key selector when deduplication_locked_columns is non-empty;
show its derived deduplication_columns or the no-active-key notice.

### Key impact and ingestion

For an unlocked table only:

~~~http
POST /api/v2/upload-sessions/{session_id}/key-impact
Content-Type: application/json

{"deduplication_columns":["epic_csn","sap_csn"],"type_overrides":{}}
~~~

It returns 202. Poll until READY_FOR_ACKNOWLEDGEMENT, show metrics, and retain
the acknowledgement token for its 30-minute expiry.

Start processing:

~~~json
POST /api/v2/upload-sessions/{session_id}/ingestions
{
  "request_id":"client-reference",
  "reporting_month":"2026-09",
  "deduplication_mode":"keyed",
  "deduplication_columns":["epic_csn","sap_csn"],
  "key_analysis_token":"token-from-session",
  "type_overrides":{},
  "manual_encryption_columns":[]
}
~~~

An unlocked keyed request must exactly match the unexpired worker impact token.
For a locked append table, the API ignores a browser replacement key and uses
the worker-derived locked-key subset. With no available locked columns it
proceeds as unkeyed. Success is 202 with session_id, job_id and phase QUEUED.

Continue polling the session through STARTING_GLUE, GLUE_RUNNING, SUCCEEDED or
FAILED; ingestion.job_run_id appears after Glue starts. Never retry an ambiguous
create/append merely because the browser request was lost. Query the session/job
first. GET /api/v2/jobs/{job_id} exposes the owner-visible durable
request/status. GET /api/ingestions/{job_run_id} returns Glue state/message.

### Optional direct-to-S3 multipart protocol

The V1-compatible multipart form is the recommended frontend path. A future
direct-S3 client can instead use these retained endpoints:

| Method/path | Request / response |
| --- | --- |
| POST /api/v2/upload-sessions | JSON: file_name, content_type, optional source_sha256. Returns session_id, upload_id, source_key. |
| POST /api/v2/upload-sessions/{session_id}/parts | JSON: part_number. Returns one presigned PUT URL. |
| POST /api/v2/upload-sessions/{session_id}/complete | JSON: parts, operation, destination. Returns job_id and QUEUED. |
| GET /api/v2/jobs/{job_id} | Durable request and status for its owner. |

The landing bucket CORS rule permits production-origin PUT and exposes ETag.
Send every completed S3 part's ETag/PartNumber to complete. If source_sha256
was supplied at session creation, the API validates the resulting object
checksum metadata before dispatch.

### History and rollback APIs

| Method/path | Request | Rule |
| --- | --- | --- |
| GET /api/upload-history?... | bucket ARN, namespace, table query | Returns history and latest_rollback_upload_id |
| POST /api/rollbacks | table_bucket_arn, namespace, table, upload_id, confirm:true JSON | Starts Glue rollback |
| GET /api/ingestions/{job_run_id} | path | Poll Glue mutation result |

Only show rollback for the exact latest_rollback_upload_id, after explicit human
confirmation. A rollback cannot restore a table's initial create because there
is no prior snapshot.

## Operations and alarms

### Concurrent users and table mutation exclusion

Users may create leases, upload sources, profile files, analyse keys and
prepare different tables concurrently. Each upload has an owner, session ID,
lease ID, request/job ID, version-pinned source object and separate S3 prefix.
No worker reads another session's raw sources.

Lease and compatibility-session objects are changed with S3 ETag `IfMatch`
compare-and-swap writes. Each accepted mutation increments `state_version`.
If the API binds a session while a worker has an older lease copy, the worker
reloads and merges its heartbeat instead of overwriting `session_id`, expiry,
cancellation, retry state or terminal result. This prevents a stale heartbeat
from detaching an already-uploaded session and leaving the UI in `RECEIVED`
until lease expiry.

Final table writes use a separate conditional S3 lock:

~~~text
s3-uploader-v2/table-locks/<sha256(bucket-arn, namespace, table)>.json
~~~

The worker acquires it immediately before Glue submission; rollback acquires
the same lock. Glue receives lock bucket/key/ETag and releases it in its
terminal `finally` path. A lock contains operational IDs only, never source or
healthcare values. A stale lock is bounded by expiry and may be safely taken
over with an ETag-matched write.

Current policy is deliberate exclusion, not a costly 16/32 GiB wait: a second
mutation of the same table receives a clear table-busy failure and the user
retries after the first Glue run completes. Different tables do not block one
another. A future table-operation queue may add automatic wait/retry, but must
retain the lock as the final safety gate and must not start duplicate Glue jobs.

### Idempotency and retry rules

- Reuse the existing lease on selection changes when its fixed worker size is
  still suitable; do not POST another lease for every browser event.
- Replace a worker only for a deterministic size change or terminal/unsafe old
  lease.
- Reuse accepted request/session/lease IDs on browser, API and SQS retries.
- Never retry an ambiguous create, append, rollback or Glue start. Query
  durable status, manifest, QC, audit history and Iceberg snapshot first.
- A lease expiring with an attached `RECEIVED`, `PROFILING`, `KEY_ANALYSING` or
  `QUEUED` session writes terminal `WORKER_LEASE_EXPIRED`, stopping infinite
  UI polling and allowing a fresh review.

For a stuck UI, check session state first, then lease/job JSON in landing S3,
then worker CloudWatch/ECS stopped-task reason, then Glue/QC/manifest. A worker
stopping after completion/cancellation/expiry is normal. Recommended alarms:
DLQ messages, Pipe failure, non-zero worker exits, API unhealthy targets, Glue
failure/timeout, Fargate memory/storage pressure, and long-running leases.

Before every release: preserve V1, add a regression test, run the full image
suite, push immutable images, review the change set, confirm Pipe task target
revisions and /healthz, then use a disposable test table for a real smoke test.

## Latest production deployment

| Component | Live value after 2026-09-10 deployment |
| --- | --- |
| CloudFormation | `s3-uploader-v2` — `UPDATE_COMPLETE` |
| API | `s3-uploader-v2-api:24`, image `20260910-production-readiness-1` |
| Base worker | `s3-uploader-v2-worker:31`, 4 vCPU / 16 GiB |
| Large worker | `s3-uploader-v2-worker:30`, 8 vCPU / 32 GiB |
| API digest | `sha256:47490762f54467109c225b5ff343c89da163aa251816282a8f9f5fb1fcf3e7df` |
| Worker digest | `sha256:b4aa1408946ba2204bf98e32da0044b42ae169742940b789feaa7046fa37cd29` |
| Pipes | base, large and legacy worker pipes — `RUNNING` |
| Health | `GET /healthz` returned `{"status":"ok"}` |
| Glue script | `generic_glue_job.py`, ETag `96eafa899ec674accc9aad8d396e8a4f` |

The 2026-09-10 release passed 48 containerised automated tests, infrastructure-template
validation, Python compilation, and `git diff --check`. It did not write test
data to a live production S3 Table. Before a high-volume release, exercise two
disposable tables in parallel and confirm a second mutation of one table is
rejected while the first lock remains active.

## 2026-09-11 FIFO corrective deployment

For this release, all ingestion submissions create a durable S3 queue record
under `s3-uploader-v2/table-queues/<destination-hash>/` before their worker is
allowed to start the Glue mutation. This is intentionally separate from the
SQS FIFO message group: SQS only orders ECS task dispatch, whereas the durable
table queue remains present until Glue reaches a terminal result. A second
same-table upload therefore reports a queued position rather than failing with
"table busy"; uploads targeting different tables remain independent.

The worker must have `s3:ListBucket` constrained to the queue, lock, job,
compatibility-session and lease prefixes. The V3 Glue role must be permitted
to delete only landing-bucket lock and queue objects. The worker passes
`QUEUE_BUCKET`, `QUEUE_KEY` and `QUEUE_ETAG` to Glue; rollback passes empty
values because it continues to use its existing direct mutation lock.

Build explicitly for the Fargate platform. A local Apple Silicon image is not
deployable to this ECS service:

~~~bash
docker buildx build --platform linux/amd64 --load \
  -f s3tables_uploader_v2/Dockerfile.api -t local/s3-uploader-v3-api:$TAG .
docker buildx build --platform linux/amd64 --load \
  -f s3tables_uploader_v2/Dockerfile.worker -t local/s3-uploader-v3-worker:$TAG .
~~~

The `20260911-table-fifo-amd64-1` production deployment used API task
definition `s3-uploader-v2-api:27`, base worker `:36`, and large worker `:37`.
The API ECR digest is
`sha256:16fb49725f31987c9afc3759f79800f1bfcbb1a3cbd4446910c0bb10f600444d`;
the worker digest is
`sha256:9394ca0b34155c1f4b2a000ebe332ca8ff50d7aa989299b479e4be4a0759f2e2`.
All three Pipes were `RUNNING`, the stack was `UPDATE_COMPLETE`, and
`GET /healthz` returned `{"status":"ok"}` after rollout.

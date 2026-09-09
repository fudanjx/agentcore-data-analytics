# S3 Uploader V2 Fargate handover

Updated: 2026-09-09 (Asia/Singapore)  
Branch: `feature/s3-uploader-v2-fargate`

## Purpose and operating model

V2 keeps the V1 browser experience while moving heavy file work away from the
shared EC2 host. The small, continuously running API/UI task handles login,
session state, browser upload orchestration and status reads. Every profile,
key-impact analysis, sanitisation and staging operation runs in a disposable
large Fargate worker. S3 is the durable record for raw objects, review
sessions, immutable requests, job status, manifests, prepared artefacts and
table contracts. DynamoDB is intentionally not used.

The public endpoint is `https://s3-uploader-v2.bot-alex.com`.

## Live deployment snapshot

Region/account: `ap-southeast-1` / `964340114883`.

| Component | Current deployment |
| --- | --- |
| ECS cluster | `embedded-web-app` |
| API service task | `s3-uploader-v2-api:12`; 1 vCPU, 2 GiB; image `s3-uploader-v2-api:20260909-12` |
| Worker task | `s3-uploader-v2-worker:13`; 8 vCPU, 32 GiB RAM, 100 GiB ephemeral storage; image `s3-uploader-v2-worker:20260909-15` |
| EventBridge Pipe | `WorkerPipe-rHmdPIRvYlol`, state `RUNNING` |
| Queue | `s3-uploader-v2-jobs.fifo` |
| Pipe target | one Fargate task in `embedded-web-app`; `S3_UPLOADER_V2_JOB_ID=$.body` |
| Landing bucket/prefix | `s3-uploader-v2-landingbucket-zfuu59kvitxh` / `s3-uploader-v2` |
| Glue job | `ah-soc-delta-pilot-web-ingest` |
| Worker log group | `s3-uploader-v2-WorkerLogGroup-OsqjUWyygrxh` |

The worker uses subnets `subnet-068627db92c9b6578`,
`subnet-080ed6c037942eb82`, and `subnet-0b2f7e53780b7dd45`; security group
`sg-0385914105bd32e30`; `AssignPublicIp=ENABLED`.

## Request lifecycle

```text
Browser / unchanged V1 static assets
        |
        v
ALB -> small API Fargate service
        |  durable session/raw object in landing S3
        v
SQS FIFO -> EventBridge Pipe -> one large worker Fargate task
        |  profile, key impact, raw selection, sanitise, typed Parquet
        v
prepared manifest + status in landing S3 -> Glue -> S3 Tables Iceberg table
```

The API does not parse or transform the submitted data file. Browser status is
reconstructed from durable S3 job status and Glue `GetJobRun`, so it survives
API task replacement and page refreshes.

## V1 compatibility boundary

V1 remains the behavioural reference. Do not modify
`s3tables_delta_pilot/static/` when working on V2. V2's copied static assets
must remain an exact V1 frontend migration; backend changes belong only under
`s3tables_uploader_v2/`.

The Glue program is byte-for-byte the V1 generic ingestion program:

```text
s3tables_delta_pilot/generic_glue_job.py
== s3tables_uploader_v2/glue_job.py
```

The required V1 ordering is:

1. inspect raw values and establish the reviewed schema/key policy;
2. for keyed loads, select retained rows from raw values;
3. sanitise only retained rows;
4. apply the reviewed, ordered Arrow/S3 Tables type contract;
5. write Glue-safe Parquet and its manifest;
6. start Glue and reconcile the resulting snapshot row count.

Never deduplicate a sanitised file. Removing/masking identifiers can make
distinct raw rows equal and invalidates the review result.

## Changes delivered in this handover

### `2b5bb2a` — `fix: align v2 worker staging with v1 contracts`

- Added V1-equivalent raw keyed row selection to
  `worker_analysis.raw_key_row_selection`.
- The worker now selects rows before sanitisation, passes those offsets through
  bounded Parquet batches, and writes `local_key_deduplication` plus the V1
  metrics in the manifest.
- The worker loads the durable reviewed session and applies its target schema
  before Parquet staging: canonical field names, ordering, null columns, date,
  timestamp, boolean, integer and double coercions are enforced before Glue.
- Nanosecond timestamps are converted to microseconds and time-only values to
  strings, because Glue rejects Parquet `TIMESTAMP(NANOS)` and `TIME(MICROS)`.
- Create-mode writes the immutable V1-style uploader contract used to validate
  future append uploads.
- The API rejects expired key-impact acknowledgements and unsupported manual
  encryption selections.

### `82a0f1c` — `fix: permit worker contract writes`

- Added the missing `s3:PutObject` permission for exactly
  `arn:aws:s3:::ah-data-analytics/temp_s3_update/web_ingest/table_contracts/*`
  to the worker role and the infrastructure source
  `infra/s3_uploader_v2_fargate.py`.

## Incident timeline and lessons

| Symptom | Cause | Corrective action | Prevention |
| --- | --- | --- | --- |
| Shared EC2 became unresponsive during a 300 MB Parquet upload | Large decompression/parsing competed with interactive containers and exhausted host memory | Moved heavy work to one-shot Fargate workers | Keep all file inspection/transformation out of API/EC2; retain bounded batch processing and resource limits |
| UI stuck in `QUEUED` / incorrect worker invocation | Pipe sent an SQS body representation that required explicit JSON decoding | Pipe uses `$.body`; worker accepts raw and JSON-string forms | Verify Pipe target override after every task-definition/Pipe update |
| Browser received HTML instead of JSON for APIs | API route/error handling mismatch | API errors now use the durable/session API response path | Check browser Network response content type and CloudWatch API logs before changing frontend |
| `TIME(MICROS)` Glue failure | Excel time-only values were staged as Parquet TIME | Convert time-only Arrow fields to strings | Add fixtures with time-only cells to worker tests |
| `TIMESTAMP(NANOS)` Glue failure | Parquet source carried nanosecond timestamps unsupported by Glue/Spark | Cast timestamps to `timestamp[us]` before staging | Keep Glue compatibility conversion in the worker, not Glue |
| Glue `Post-append reconciliation failed: before=0, appended=3339767, after=3335436` | V2 deduplicated after sanitisation; 4,331 raw-distinct rows collapsed after protected fields changed | Raw V1 de-dup selection is now performed before sanitisation | Test raw-conflict versus post-sanitisation-equality fixtures |
| `Worker preparation failed`, `AccessDenied`, no Glue run | Worker role could read but not write V1 table contracts to `ah-data-analytics` | Added prefix-scoped `s3:PutObject` | Every new worker write target must have both code and IAM reviewed together |

## Data-safety rules

- A Glue create run can write a table before a subsequent reconciliation check
  reports failure. Do **not** retry `CREATE` against that table name.
- Before cleanup, inspect target table existence and row count. Deleting or
  overwriting a partially created table requires explicit operator approval.
- The later `AccessDenied` incident failed before Glue started; its target
  `pilot.nuh_soc_v2_test_rd1` was confirmed not to exist and is safe to use in
  a new upload session.
- Do not replace this append workflow with the existing
  `lambda_s3tables_loader/handler.py`; it calls `table.overwrite(...)` and has
  a different, destructive contract.
- Keep raw uploads versioned, preserve object version IDs in job records, and
  treat retried job IDs as immutable/claimed rather than reusing a worker task.

## IAM and security contract

The worker task role is
`s3-uploader-v2-WorkerTaskRole-nMydEoB3frGG`.

Required worker capabilities are deliberately scoped:

- read/write landing-bucket objects required for sessions, requests, manifests,
  prepared Parquet and status;
- `s3:ListBucket` only for necessary landing prefixes;
- read/write V1 contract objects only under
  `ah-data-analytics/temp_s3_update/web_ingest/table_contracts/*`;
- `glue:StartJobRun` only for `ah-soc-delta-pilot-web-ingest`;
- `secretsmanager:GetSecretValue` only for the encryption secret.

The encryption secret remains injected as
`S3_UPLOADER_V2_ENCRYPTION_SECRET_ARN`; its ARN/value must not be logged. The
worker code contains no fixed secret ARN.

## Operations and diagnosis

### Normal observation path

1. ECS console -> `embedded-web-app` -> Tasks: API is persistent; workers are
   expected to appear and stop per request.
2. Inspect the worker task definition revision and image tag.
3. Open the worker CloudWatch stream under the worker log group.
4. Read the S3 job record at
   `s3://<landing-bucket>/s3-uploader-v2/jobs/<job-id>/status.json` and the
   request/manifest alongside it.
5. If status is `RUNNING_GLUE`, inspect the recorded Glue run ID with
   `aws glue get-job-run`.

### Failure triage checklist

- `AccessDenied` before `STARTING_GLUE`: identify the exact AWS action and ARN
  from the stopped worker's CloudWatch traceback. Add only that action/resource
  pair to both the live task role and `infra/s3_uploader_v2_fargate.py`.
- `FAILED` after Glue starts: retrieve the manifest and compare
  `incoming_row_count`, `prepared_row_count`, `local_key_deduplication`, and
  local metrics against Glue's error. Do not assume an ECS failure.
- `TIME(MICROS)` or `TIMESTAMP(NANOS)`: inspect the prepared Parquet schema,
  not merely the raw upload schema.
- Pipe dispatch failure: verify Pipe state, task-definition ARN, subnets,
  security group and `S3_UPLOADER_V2_JOB_ID=$.body` override.
- Stuck UI: query the durable job status, then Glue status. A stopped Fargate
  task is normal after durable status is written.

## Deployment procedure

1. Keep V1 files untouched. Change the smallest V2 worker/API/infra surface.
2. Add a regression test for the exact failure boundary.
3. Build the Linux/amd64 worker image:

   ```bash
   docker build --platform linux/amd64 \
     -f s3tables_uploader_v2/Dockerfile.worker \
     -t s3-uploader-v2-worker:YYYYMMDD-N .
   ```

4. Run the full V2 suite in that image:

   ```bash
   docker run --rm -v "$PWD:/workspace" -w /workspace \
     -e PYTHONPATH=/workspace s3-uploader-v2-worker:YYYYMMDD-N \
     python -m unittest discover -s s3tables_uploader_v2/tests -v
   ```

5. Push the immutable image tag to ECR, register a new worker task definition
   from the prior revision, changing only its image, and update the Pipe to
   that task definition.
6. Confirm Pipe state is `RUNNING`, task definition is the new revision, and
   the job-id override is unchanged.
7. Use a new disposable test table for a create-mode smoke test. Do not use a
   target table affected by any earlier failed create run.
8. Check worker logs, manifest and Glue completion. Only then ask users to
   retry business data.

## Test evidence

The worker image `20260909-15` was built for `linux/amd64`. The full V2 suite
passed with 18 tests, including regressions for:

- raw-key conflicts versus exact raw duplicates;
- selection before sanitisation;
- reviewed V1 contract ordering, null columns and date/integer casts;
- Excel ingestion, manual encryption, time-only conversion and nanosecond
  timestamp conversion;
- create-contract persistence;
- API login/session isolation and durable job storage.

## Remaining enhancement guardrails

- Preserve one worker per job. Do not move processing back into the API task.
- Multi-file V1 ingestion is not yet enabled in V2; V2 intentionally rejects
  it until one worker can create one atomic multi-file manifest with the same
  raw row-selection semantics across files.
- Before adding rollback/history or table-lock enhancements, port their V1
  S3 contract and tests together; do not create a second state store.
- Add CloudWatch alarms for worker non-zero exits, Pipe failures, Glue failures,
  SQS DLQ messages, and Fargate memory/ephemeral-storage pressure.
- Tune Fargate capacity from observed peak use. The current 8 vCPU/32 GiB/100
  GiB configuration isolates large uploads but should not be treated as a
  permanent cost optimum.

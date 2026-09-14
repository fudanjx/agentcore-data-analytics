# Complete V3 Flow

## End-to-End Process

```text
1. User selects files
        |
        v
2. Browser sends filenames + sizes to API
        |
        v
3. API calculates BASE or LARGE deterministically
        |
        +-- BASE --> s3-uploader-v3-base.fifo
        |
        +-- LARGE -> s3-uploader-v3-large.fifo
                         |
                         v
4. EventBridge Pipe calls ECS RunTask
                         |
                         v
5. Leased Fargate worker starts and waits for the upload
                         |
                         v
6. User clicks Review upload
                         |
                         v
7. API streams raw files into encrypted, versioned S3
                         |
                         v
8. Existing leased worker downloads/reuses the files
   and performs:
   - schema profiling
   - anonymisation detection
   - repeated key-impact analysis
                         |
                         v
9. User clicks Upload and run ETL
                         |
             +-----------+-----------+
             |                       |
             v                       v
10a. Mutation command        10b. Existing leased worker
     is written to S3              sanitises data and writes
     and queued in                 Glue-safe Parquet + manifest
     mutations.fifo
             |                       |
             |                 status becomes
             |              READY_FOR_MUTATION
             +-----------+-----------+
                         |
                         v
11. Permanent mutation dispatcher consumes the command
                         |
                         v
12. Dispatcher checks:
    - preparation is complete
    - same-table lock is available
    - Glue concurrency quota is available
                         |
                         v
13. Dispatcher directly calls glue:StartJobRun
                         |
                         v
14. Glue creates/appends/rolls back the S3 Table
    and writes QC + upload history
                         |
                         v
15. Dispatcher monitors Glue until terminal
                         |
                         v
16. Dispatcher updates S3 status, releases the table lock,
    and deletes the SQS message
                         |
                         v
17. UI polling observes the terminal status
```

## 1. File Selection and Routing

File selection triggers:

```http
POST /api/v3/worker-leases
```

Only metadata is submitted initially:

- Filename
- File size
- User identity

No healthcare data is placed in SQS.

The API calculates:

```text
score = sum(file size / format allowance)
```

- Parquet/Parquet GZIP: 128 MiB
- CSV/TSV: 64 MiB
- XLS/XLSX: 32 MiB
- Score ≤ 1: base worker, 4 vCPU/16 GiB
- Score > 1: large worker, 8 vCPU/32 GiB

This logic is implemented in `api.py` around line 416:

```text
/Users/jinxin/Documents/AgentCore/s3tables_uploader_v2/api.py:416
```

## 2. Base and Large Worker Queues

These queues only launch leased workers:

- `s3-uploader-v3-base.fifo`
- `s3-uploader-v3-large.fifo`

EventBridge Pipes consumes these messages and calls ECS `RunTask`.

The worker remains attached to that upload session through:

- Initial profiling
- Sanitisation detection
- One or more key-impact analyses
- Final sanitisation and Parquet preparation

Changing files reuses the same worker when its size remains suitable. A base-to-large routing change terminates the base lease and launches a large worker.

The leased-worker lifecycle is implemented in `worker.py` around line 570:

```text
/Users/jinxin/Documents/AgentCore/s3tables_uploader_v2/worker.py:570
```

## 3. Review Upload

When the user clicks **Review upload**, the files are streamed through the API into the versioned S3 landing bucket.

S3 stores:

- Immutable raw object versions
- Checksums
- Upload session
- Worker lease
- Profiling results
- Key-impact results
- Sanitised staging artefacts
- Job and mutation status

S3 is therefore the authoritative state store. The worker's local disk is only a temporary cache.

## 4. Upload and Run ETL

When this button is pressed, the API:

1. Revalidates ownership, schema, sanitisation choices, and the deduplication contract.
2. Disables **Cancel & Start Over**.
3. Creates the immutable job request.
4. Creates a mutation command.
5. Sends the mutation ID to `s3-uploader-v3-mutations.fifo`.
6. Changes the worker session to `QUEUED`.

The worker then performs final sanitisation and pre-Glue validation. The mutation message may already be held by the dispatcher, but Glue cannot start until the worker changes the job state to `READY_FOR_MUTATION`.

## 5. Mutation Dispatcher and Glue

There is one permanently running dispatcher ECS task.

It directly polls:

```text
s3-uploader-v3-mutations.fifo
```

There is no EventBridge Pipe between this queue and Glue.

For each command, the dispatcher:

- Waits for `READY_FOR_MUTATION`.
- Acquires the per-table S3 lock.
- Enforces the configured maximum of five concurrent Glue runs.
- Calls `glue:StartJobRun`.
- Monitors the Glue job.
- Renews SQS visibility while processing.
- Persists the terminal status.
- Releases the table lock.
- Deletes the SQS message.

This logic is implemented in `mutation_dispatcher.py` around line 160:

```text
/Users/jinxin/Documents/AgentCore/s3tables_uploader_v2/mutation_dispatcher.py:160
```

## 6. FIFO Behaviour

The mutation queue uses a `MessageGroupId` derived from:

```text
table bucket ARN + namespace + table name
```

Therefore:

- `Table A job 1` must finish before `Table A job 2`.
- `Table A`, `Table B`, and `Table C` can run concurrently.
- Different tables are limited only by the configured Glue concurrency budget.
- A same-table upload is queued rather than rejected as “table busy”.

## 7. Data-Security Boundary

```text
Raw data:
Browser -> API -> encrypted/versioned S3 -> leased worker

Sanitised data:
Worker -> temporary S3 staging -> Glue -> S3 Tables

Queue contents:
Lease IDs and mutation IDs only
```

SQS never carries the uploaded dataset or sample values.

## 8. DLQ Behaviour

All three operational queues use:

```text
s3-uploader-v2-dlq.fifo
```

Messages that repeatedly fail are quarantined there:

- Base/large worker launch: after 2 failed attempts
- Mutation processing: after 5 failed attempts

This prevents a poison message from retrying indefinitely.

## Concise Corrected Flow

```text
File selection
 -> API
 -> base/large FIFO
 -> EventBridge Pipe
 -> leased Fargate worker
 -> profile/key analysis
 -> final preparation
 -> mutation FIFO
 -> permanent dispatcher
 -> Glue
 -> S3 Tables
```

> **Key point:** The mutation dispatcher, not EventBridge, is the component between the mutation FIFO queue and Glue.

# S3 Uploader v2 Fargate Design

## Goal

Deploy a new S3 Tables uploader at `https://s3-uploader-v2.bot-alex.com` without modifying `s3tables_delta_pilot/`. The current pilot is behaviour-only reference material; the new implementation owns its source, deployment, and operational state.

## Decision

Run two ECS Fargate workloads behind the existing AWS account:

1. An always-on FastAPI API service behind an internet-facing ALB. It retains the existing minimal password/cookie login temporarily, serves the current UI/API contract, creates jobs, and reports status. It does not parse or stage uploaded data.
2. A one-shot Fargate worker for each profiling/preparation request. It reads a job record from S3, validates and sanitises the input, writes prepared artifacts and a manifest to S3, starts the existing Glue ingestion workflow, records status, then exits.

## Data and job contract

All durable state is in an encrypted private S3 prefix. DynamoDB is intentionally not used.

```text
s3://<landing-bucket>/s3-uploader-v2/
  uploads/<session-id>/raw/<object-version-or-key>   # short-lived, encrypted raw source
  jobs/<job-id>/request.json                         # immutable request and source version
  jobs/<job-id>/claim.json                           # conditional, worker ownership
  jobs/<job-id>/status.json                          # latest UI summary
  jobs/<job-id>/events/<sequence>.json               # immutable audit events
  jobs/<job-id>/prepared/...                         # sanitised, Glue-readable artifacts
  jobs/<job-id>/result.json                          # terminal result and Glue run ID
```

The API uploads through a scoped pre-signed multipart URL. It writes the request only after the browser completes the upload and S3 object version/checksum is verified. Workers use a conditional S3 claim object so SQS duplicate delivery cannot execute the same job twice. Raw source data expires rapidly after preparation; prepared, sanitised artifacts follow the existing uploader archive lifecycle.

## Runtime topology

```text
Browser -- HTTPS --> ALB --> FastAPI ECS service --> S3 job objects
                                      |                    |
                                      |                    +--> SQS --> Fargate worker --> Glue
                                      +--> reads status.json                 |
                                                                           S3 Tables
```

The ALB receives a new `ip` target group for the Fargate API. Route 53 creates an alias A/AAAA record for `s3-uploader-v2.bot-alex.com` to that ALB after health checks pass. The existing EC2 target group and hostname stay untouched.

## Security and operations

- The API and worker use distinct least-privilege task roles.
- Landing and artifact buckets use SSE-KMS; raw and prepared data have separate prefixes and lifecycle rules.
- The existing password/cookie implementation is retained only as the user-authorised minimal bridge. Production configuration requires strong injected secrets, secure cookies, HTTPS-only access, request-size limits, and no development identity endpoint.
- The API starts as one replica until its stateless job/session contract is proven; it can then scale horizontally because sessions are S3-backed.
- The worker starts at 4 vCPU, 16 GiB memory, and 100 GiB ephemeral storage. It processes Arrow/Parquet in bounded batches; an OOM fails only the worker job.


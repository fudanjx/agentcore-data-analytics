# S3 Uploader v2 Fargate Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a new Fargate-hosted S3 Tables uploader that preserves the existing pilot's processing rules while isolating large-file work from the UI and EC2 workloads.

**Architecture:** `s3tables_uploader_v2/` is a new package. Its API persists immutable S3-backed jobs and dispatches worker work; its worker contains the copied-and-adapted validation, sanitisation, staging, and Glue-launch logic. `s3tables_delta_pilot/` is never edited.

**Tech Stack:** Python 3.12, FastAPI, PyArrow/Polars/Pandas, boto3, ECS Fargate, ALB, S3/SQS/Glue/S3 Tables, CloudFormation.

---

### Task 1: Establish the isolated package and compatibility contract

**Files:**
- Create: `s3tables_uploader_v2/__init__.py`
- Create: `s3tables_uploader_v2/config.py`
- Create: `s3tables_uploader_v2/tests/test_config.py`
- Reference only: `s3tables_delta_pilot/{webapp,ingest_contract,sanitization,generic_glue_job}.py`

1. Write failing configuration tests for required bucket/prefix, job retention, API/worker role separation, and production-safe cookie settings.
2. Implement typed configuration with no production defaults for secrets or AWS resource IDs.
3. Run the focused tests.
4. Commit only new v2 paths.

### Task 2: Add versioned S3 job records and idempotent worker claims

**Files:**
- Create: `s3tables_uploader_v2/job_store.py`
- Create: `s3tables_uploader_v2/models.py`
- Create: `s3tables_uploader_v2/tests/test_job_store.py`

1. Write failing tests using botocore stubs for immutable request creation, conditional `claim.json` creation, status ETag updates, terminal result writes, and duplicate-delivery rejection.
2. Implement the S3 object layout from the design with JSON schema/version validation.
3. Require raw source object key, S3 version ID, SHA-256, owner ID, destination and operation in every request.
4. Run focused tests and commit.

### Task 3: Build the lightweight FastAPI control plane

**Files:**
- Create: `s3tables_uploader_v2/api.py`
- Create: `s3tables_uploader_v2/auth.py`
- Create: `s3tables_uploader_v2/static/`
- Create: `s3tables_uploader_v2/tests/test_api_jobs.py`

1. Write failing tests for login/cookie protection, upload-session creation, scoped pre-signed multipart URLs, completion verification, job creation, and status polling.
2. Adapt only UI/auth/API behaviour from the pilot; remove local `UploadSessionStore`, `BackgroundTasks`, and in-memory progress state.
3. Reject file bytes at preflight/ingestion APIs; accept S3 session/job identifiers instead.
4. Run API tests and commit.

### Task 4: Port processing rules into a batch-oriented worker

**Files:**
- Create: `s3tables_uploader_v2/{contract,ingest_contract,sanitization,local_deduplication,worker}.py`
- Create: `s3tables_uploader_v2/tests/test_worker_contract.py`
- Create: `s3tables_uploader_v2/tests/test_worker_idempotency.py`

1. Copy processing policy through new v2-owned modules; retain existing sanitisation, canonicalisation, temporal parsing, schema matching, de-duplication, manifest, and error semantics through regression fixtures.
2. Write failing tests comparing selected v2 outputs to reference fixtures from the pilot without importing production v2 code from the pilot at runtime.
3. Replace full-table scans with bounded Arrow batches where behaviour remains identical; document any intentionally deferred parity edge case.
4. Have the worker claim the job, emit phase events, write sanitised artifacts/manifest, launch Glue, and write terminal status.
5. Run focused tests and commit.

### Task 5: Containerise API and worker separately

**Files:**
- Create: `s3tables_uploader_v2/Dockerfile.api`
- Create: `s3tables_uploader_v2/Dockerfile.worker`
- Create: `s3tables_uploader_v2/requirements.txt`
- Create: `s3tables_uploader_v2/tests/test_container_contract.py`

1. Test expected image entry points and non-root execution.
2. Build a minimal API image exposing 8090 and a non-listening worker image.
3. Ensure images contain no `.env`, AWS credentials, or pilot source imports.
4. Build locally and run package tests; commit.

### Task 6: Define deployable AWS infrastructure

**Files:**
- Create: `infra/s3_uploader_v2_fargate.py`
- Create: `infra/s3_uploader_v2_fargate.parameters.example.json`
- Create: `infra/tests/test_s3_uploader_v2_fargate.py`

1. Write template tests for separate API/worker roles, KMS S3 encryption, SQS DLQ, Fargate API service, one-shot worker task definition, CloudWatch logs, private subnets/security groups, and ALB IP target group.
2. Implement a parameterised CloudFormation template generator without embedding secrets or account-specific IDs.
3. Include Route 53 alias record inputs for `s3-uploader-v2.bot-alex.com`, but do not create the live DNS record until deployment health checks pass.
4. Render and validate the template; commit.

### Task 7: Verify and hand off the deployment

**Files:**
- Create: `docs/runbooks/s3-uploader-v2-fargate.md`
- Modify: `README.md` only if a repository deployment index exists

1. Document image publishing, stack parameters, required secrets, S3 lifecycle rules, health endpoint, rollback, worker OOM handling, and post-deployment ALB/Route 53 verification.
2. Run all v2 tests plus existing pilot tests to prove the reference directory stayed unchanged.
3. Compare `git diff -- s3tables_delta_pilot/` and require no changes.
4. Deploy only with explicit user approval; verify the new HTTPS hostname, target health, job lifecycle, and an approved non-sensitive test file.
5. Commit documentation and hand off the feature branch.


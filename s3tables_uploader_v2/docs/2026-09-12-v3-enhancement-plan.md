# V3 Single Asynchronous FIFO Dispatcher — Completed Implementation Record

**Completed: 2026-09-12, Asia/Singapore**

This document records the completed enhancement. The current authoritative
architecture and frontend contract are in:

- `2026-09-10-v3-handover.md`
- `2026-09-10-v3-deployment-and-api-integration-guide.md`

## Delivered design

- One physical FIFO mutation queue: `s3-uploader-v3-mutations.fifo`.
- Table-specific ordering via `MessageGroupId = SHA-256(table bucket ARN,
  namespace, table)`.
- One 0.5 vCPU / 1 GiB ECS dispatcher service, desired count one.
- Up to five Glue mutations for different tables at once, matching Glue
  `MaxConcurrentRuns=5`; up to 50 received SQS messages tracked in memory.
- The dispatcher holds every SQS receipt through terminal state, renews the
  120-second visibility timeout every 30 seconds, writes durable terminal
  status, releases its owned S3 lock, and only then deletes the receipt.
- Later same-table work remains hidden by SQS until the earlier command is
  terminal and acknowledged. Different tables can run concurrently.
- The dispatcher includes recovered S3 table locks in its capacity count after
  restart. A mutation DLQ receives a message only after five receives and has a
  CloudWatch alarm.
- Create, append and rollback all use a durable S3 `MutationCommand` and the
  same FIFO path. Rollback is owner/table/upload idempotent and is polled via
  `GET /api/mutations/{mutation_id}`.
- Workers only profile, analyse and prepare sanitised artifacts; the dispatcher
  owns Glue start/poll/terminal status. New Glue calls omit legacy lock/queue
  arguments. Legacy arguments are optional in `glue_job.py` solely to let
  in-flight pre-rollout calls complete.
- Deleting an uploader-managed table is refused while its mutation lock exists.

## Safety and behavior retained

- V1-compatible sanitisation, typed staging, timestamp/time conversion,
  multi-file rules, immutable table de-duplication, history and rollback stay
  unchanged by this enhancement.
- Base/large leased workers and their deterministic routing queues remain in
  place for pre-Glue work. The dispatcher does not replace worker queues.
- S3 remains the system of record; no DynamoDB, EFS, EKS or Lambda was added.

## Verification and deployment

- Complete containerised worker suite: 58 passing tests.
- Focused API/dispatcher/infrastructure suite: 24 passing tests.
- `git diff --check` passed.
- Published API and worker images:
  `20260912-single-async-dispatcher-amd64-1`.
- Deployed API task definition `s3-uploader-v2-api:30` and dispatcher task
  definition `s3-uploader-v3-mutation-dispatcher:3`, each desired/running one.
- CloudFormation stack `s3-uploader-v2` reached `UPDATE_COMPLETE`; API health
  endpoint returned `{"status":"ok"}`.
- Follow-up review-sample release: worker/dispatcher image
  `20260912-review-samples-amd64-1` (digest
  `sha256:1d3cc049c18d002fe996f150e34ac6b4b4c62a4d78a863b77cab4238b717f682`),
  base worker `:45`, large worker `:44`, and dispatcher `:4`. The API remained
  on `s3-uploader-v2-api:30` because no API/UI behavior changed.

## Deployment lessons captured

- Resolve task-definition environment entries by name, never list position.
- Dispatcher restart reconciliation needs landing-bucket `ListBucket` access
  for both the `table-locks` prefix and `table-locks/*`.
- Do not acknowledge a mutation message merely because Glue has started;
  acknowledgement occurs after Glue terminal status and lock release.

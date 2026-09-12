"""Durable multiplexing consumer for ordered S3 Tables Glue mutations.

One physical FIFO queue is shared by every table. SQS message groups keep each
table ordered while this process can supervise several independent Glue runs.
S3 remains authoritative, so an ECS restart reconstructs state from durable
command, status, and table-lock records.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any, Literal

import boto3
from botocore.exceptions import ClientError

from .config import _required
from .job_store import MissingRecord, S3JobStore
from .models import Destination, JobRequest, JobStatus, MutationCommand
from .table_lock import S3TableLockManager, TableLockError, TableLockedError


_TERMINAL_GLUE_STATES = {"SUCCEEDED", "FAILED", "ERROR", "TIMEOUT", "STOPPED"}
_PREPARATION_STATES = {"QUEUED", "CLAIMED", "PROFILING", "PREPARING"}
_ACTIVE_GLUE_PHASES = {"STARTING_GLUE", "RUNNING_GLUE"}
_HISTORY_BUCKET = "ah-data-analytics"
_HISTORY_PREFIX = "temp_s3_update/web_ingest/upload_history"


@dataclass(frozen=True)
class MutationDispatcherSettings:
    region: str
    landing_bucket: str
    landing_prefix: str
    queue_url: str
    glue_job_name: str
    max_concurrent_glue: int = 5
    max_tracked_messages: int = 50
    visibility_seconds: int = 120
    visibility_renewal_seconds: int = 30
    poll_seconds: int = 10

    @classmethod
    def from_environ(cls, environ: dict[str, str] | None = None) -> "MutationDispatcherSettings":
        env = dict(os.environ if environ is None else environ)
        return cls(
            region=_required("AWS_REGION", env),
            landing_bucket=_required("S3_UPLOADER_V2_LANDING_BUCKET", env),
            landing_prefix=env.get("S3_UPLOADER_V2_LANDING_PREFIX", "s3-uploader-v2").strip("/"),
            queue_url=_required("S3_UPLOADER_V3_MUTATION_QUEUE_URL", env),
            glue_job_name=_required("S3_UPLOADER_V2_GLUE_JOB_NAME", env),
            max_concurrent_glue=int(env.get("S3_UPLOADER_V3_MAX_CONCURRENT_GLUE", "5")),
            max_tracked_messages=int(env.get("S3_UPLOADER_V3_MAX_TRACKED_MUTATIONS", "50")),
            visibility_seconds=int(env.get("S3_UPLOADER_V3_MUTATION_VISIBILITY_SECONDS", "120")),
            visibility_renewal_seconds=int(env.get("S3_UPLOADER_V3_MUTATION_VISIBILITY_RENEWAL_SECONDS", "30")),
            poll_seconds=int(env.get("S3_UPLOADER_V3_MUTATION_POLL_SECONDS", "10")),
        )


@dataclass
class _TrackedMessage:
    job_id: str
    receipt_handle: str
    last_visibility_at: float


def _history_prefix(destination: Destination) -> str:
    import hashlib

    scope = hashlib.sha256(f"{destination.table_bucket_arn}|{destination.namespace}".encode("utf-8")).hexdigest()[:16]
    return f"{_HISTORY_PREFIX}/{scope}/{destination.table}/"


def _terminal(state: str) -> bool:
    return state in _TERMINAL_GLUE_STATES


def _glue_state(glue: Any, settings: MutationDispatcherSettings, run_id: str) -> tuple[str, str]:
    run = glue.get_job_run(JobName=settings.glue_job_name, RunId=run_id, PredecessorsIncluded=False)["JobRun"]
    state = str(run.get("JobRunState", "UNKNOWN"))
    return state, str(run.get("ErrorMessage") or run.get("StateDetail") or f"Glue job is {state.lower()}.")


def _find_ambiguous_run(glue: Any, settings: MutationDispatcherSettings, job_id: str) -> str | None:
    """Reconnect after a process dies between Glue start and status write."""
    matches = [
        str(run["Id"])
        for run in glue.get_job_runs(JobName=settings.glue_job_name, MaxResults=100).get("JobRuns", [])
        if str((run.get("Arguments") or {}).get("--RUN_ID", "")) == job_id
    ]
    return matches[0] if len(matches) == 1 else None


def _lock_manager(s3: Any, settings: MutationDispatcherSettings) -> S3TableLockManager:
    return S3TableLockManager(s3, settings.landing_bucket, f"{settings.landing_prefix}/table-locks")


def _release_lock(manager: S3TableLockManager, command: MutationCommand) -> None:
    manager.release_if_owned(
        table_bucket_arn=command.destination.table_bucket_arn,
        namespace=command.destination.namespace,
        table=command.destination.table,
        owner_token=command.mutation_id,
    )


def _legacy_command(request: JobRequest) -> MutationCommand:
    """Read uploads written before mutation envelopes were introduced."""
    return MutationCommand(
        mutation_id=request.job_id,
        request_id=request.job_id,
        owner_user_id=request.owner_user_id,
        operation=request.operation,
        destination=request.destination,
        upload_id=request.upload_id,
        source_job_id=request.job_id,
        reporting_month=request.reporting_month,
        filenames_json=json.dumps([source.name for source in request.source_files] or [request.source_key.rsplit("/", 1)[-1]]),
        original_uploaded_by=request.owner_user_id,
        original_uploaded_at=request.created_at.isoformat(),
        created_at=request.created_at,
    )


def _command_and_source(store: S3JobStore, mutation_id: str) -> tuple[MutationCommand, JobRequest | None]:
    try:
        command = store.get_mutation_command(mutation_id)
    except MissingRecord:
        request = store.get_request(mutation_id)
        return _legacy_command(request), request
    return command, store.get_request(command.source_job_id) if command.source_job_id else None


def _glue_arguments(command: MutationCommand, source: JobRequest | None, settings: MutationDispatcherSettings) -> dict[str, str]:
    manifest_uri = (
        f"s3://{settings.landing_bucket}/{settings.landing_prefix}/jobs/{command.source_job_id}/prepared/manifest.json"
        if command.source_job_id
        else "s3://ah-data-analytics/temp_s3_update/web_ingest/uploads/not-used-for-rollback.json"
    )
    filenames = command.filenames_json
    if source is not None:
        filenames = json.dumps([item.name for item in source.source_files] or [source.source_key.rsplit("/", 1)[-1]])
    return {
        "--MODE": command.operation,
        "--MANIFEST_URI": manifest_uri,
        "--TABLE_BUCKET_ARN": command.destination.table_bucket_arn,
        "--NAMESPACE": command.destination.namespace,
        "--TABLE": command.destination.table,
        "--RUN_ID": command.mutation_id,
        "--UPLOAD_ID": command.upload_id or f"UPLOAD-{command.mutation_id.replace('-', '')[:12].upper()}",
        "--UPLOADED_BY": command.owner_user_id,
        "--QC_PREFIX": f"s3://{settings.landing_bucket}/{settings.landing_prefix}/qc",
        "--AUDIT_PREFIX": f"s3://{_HISTORY_BUCKET}/{_history_prefix(command.destination)}",
        "--REPORTING_MONTH": command.reporting_month or "not-applicable",
        "--FILENAMES_JSON": filenames or "[]",
        "--ROLLBACK_SNAPSHOT_ID": command.rollback_snapshot_id or "not-applicable",
        "--ORIGINAL_UPLOADED_BY": command.original_uploaded_by or command.owner_user_id,
        "--ORIGINAL_UPLOADED_AT": command.original_uploaded_at or command.created_at.isoformat(),
    }


def dispatch_job(mutation_id: str, settings: MutationDispatcherSettings, s3_client: Any, glue_client: Any, *, allow_glue_start: bool = True) -> Literal["waiting", "terminal"]:
    """Advance exactly one durable mutation without acknowledging its receipt."""
    store = S3JobStore(s3_client, settings.landing_bucket, settings.landing_prefix)
    try:
        command, source = _command_and_source(store, mutation_id)
        status = store.get_status(mutation_id).status
    except MissingRecord:
        return "terminal"

    manager = _lock_manager(s3_client, settings)
    if status.phase in {"SUCCEEDED", "FAILED"}:
        _release_lock(manager, command)
        return "terminal"
    if status.phase in _PREPARATION_STATES:
        return "waiting"
    if status.phase == "READY_FOR_MUTATION":
        if not allow_glue_start:
            return "waiting"
        try:
            manager.acquire(
                table_bucket_arn=command.destination.table_bucket_arn,
                namespace=command.destination.namespace,
                table=command.destination.table,
                owner_token=mutation_id,
                user_id=command.owner_user_id,
                request_id=mutation_id,
                session_id=source.session_id if source else None,
                operation=command.operation,
                phase="STARTING_GLUE",
            )
        except TableLockedError:
            return "waiting"
        store.put_status(JobStatus(job_id=mutation_id, phase="STARTING_GLUE", message="Starting the S3 Tables ingestion job in per-table FIFO order."))
        try:
            response = glue_client.start_job_run(
                JobName=settings.glue_job_name,
                JobRunQueuingEnabled=True,
                Arguments=_glue_arguments(command, source, settings),
            )
        except ClientError as error:
            code = str(error.response.get("Error", {}).get("Code", ""))
            if code in {"ConcurrentRunsExceededException", "ResourceNumberLimitExceededException"}:
                store.put_status(JobStatus(
                    job_id=mutation_id, phase="READY_FOR_MUTATION",
                    message="Waiting for available Glue capacity before starting this table mutation.",
                ))
                _release_lock(manager, command)
                return "waiting"
            raise
        store.put_status(JobStatus(job_id=mutation_id, phase="RUNNING_GLUE", message="Glue ingestion is running.", glue_run_id=str(response["JobRunId"])))
        return "waiting"
    if status.phase == "STARTING_GLUE" and not status.glue_run_id:
        run_id = _find_ambiguous_run(glue_client, settings, mutation_id)
        if not run_id:
            store.put_status(JobStatus(job_id=mutation_id, phase="FAILED", message="Glue start outcome is ambiguous; no duplicate ingestion was started.", error_code="GLUE_START_AMBIGUOUS"))
            _release_lock(manager, command)
            return "terminal"
        status = JobStatus(job_id=mutation_id, phase="RUNNING_GLUE", message="Reconnected to the existing Glue ingestion.", glue_run_id=run_id)
        store.put_status(status)
    if status.phase == "RUNNING_GLUE" and status.glue_run_id:
        state, message = _glue_state(glue_client, settings, status.glue_run_id)
        if not _terminal(state):
            return "waiting"
        store.put_status(JobStatus(
            job_id=mutation_id,
            phase="SUCCEEDED" if state == "SUCCEEDED" else "FAILED",
            message="Glue ingestion succeeded." if state == "SUCCEEDED" else message,
            error_code=None if state == "SUCCEEDED" else f"GLUE_{state}",
            glue_run_id=status.glue_run_id,
        ))
        _release_lock(manager, command)
        return "terminal"
    return "waiting"


class MutationScheduler:
    """A bounded, non-blocking coordinator for FIFO receipts and Glue runs."""

    def __init__(self, settings: MutationDispatcherSettings, s3_client: Any, sqs_client: Any, glue_client: Any):
        self.settings = settings
        self.s3 = s3_client
        self.sqs = sqs_client
        self.glue = glue_client
        self.tracked: dict[str, _TrackedMessage] = {}

    def _status(self, mutation_id: str) -> JobStatus | None:
        try:
            return S3JobStore(self.s3, self.settings.landing_bucket, self.settings.landing_prefix).get_status(mutation_id).status
        except MissingRecord:
            return None

    def _active_glue_count(self) -> int:
        # After an ECS replacement the outstanding FIFO receipts are still
        # invisible, but their S3 locks and statuses remain. Count both those
        # recovered runs and locally tracked messages so a fresh dispatcher
        # cannot exceed the configured Glue concurrency budget.
        active_ids = {
            item for item in self.tracked
            if (status := self._status(item)) and status.phase in _ACTIVE_GLUE_PHASES
        }
        try:
            leases = _lock_manager(self.s3, self.settings).list_leases()
        except TableLockError as error:
            print(json.dumps({"dispatcher_active_count": "lock_scan_failed", "error": str(error)}))
            return len(active_ids)
        for lease in leases:
            status = self._status(str(lease.owner_token))
            if status and status.phase in _ACTIVE_GLUE_PHASES:
                active_ids.add(str(lease.owner_token))
        return len(active_ids)

    def receive(self, now: float) -> None:
        remaining = self.settings.max_tracked_messages - len(self.tracked)
        if remaining <= 0:
            return
        response = self.sqs.receive_message(
            QueueUrl=self.settings.queue_url,
            MaxNumberOfMessages=min(10, remaining),
            WaitTimeSeconds=1 if self.tracked else 20,
            VisibilityTimeout=self.settings.visibility_seconds,
        )
        for message in response.get("Messages", []):
            mutation_id = str(message["Body"])
            self.tracked[mutation_id] = _TrackedMessage(mutation_id, str(message["ReceiptHandle"]), now)

    def tick(self, now: float | None = None) -> None:
        moment = time.monotonic() if now is None else now
        self.receive(moment)
        active = self._active_glue_count()
        for mutation_id, tracked in list(self.tracked.items()):
            try:
                before = self._status(mutation_id)
                was_active = bool(before and before.phase in _ACTIVE_GLUE_PHASES)
                outcome = dispatch_job(mutation_id, self.settings, self.s3, self.glue, allow_glue_start=active < self.settings.max_concurrent_glue)
                after = self._status(mutation_id)
                is_active = bool(after and after.phase in _ACTIVE_GLUE_PHASES)
                if not was_active and is_active:
                    active += 1
                elif was_active and not is_active:
                    active -= 1
                if outcome == "terminal":
                    self.sqs.delete_message(QueueUrl=self.settings.queue_url, ReceiptHandle=tracked.receipt_handle)
                    del self.tracked[mutation_id]
                    continue
                if moment - tracked.last_visibility_at >= self.settings.visibility_renewal_seconds:
                    self.sqs.change_message_visibility(
                        QueueUrl=self.settings.queue_url,
                        ReceiptHandle=tracked.receipt_handle,
                        VisibilityTimeout=self.settings.visibility_seconds,
                    )
                    tracked.last_visibility_at = moment
            except Exception as error:
                print(json.dumps({"mutation_id": mutation_id, "dispatcher_step": "failed", "error": str(error)}))

    def reconcile_locks(self) -> None:
        """Clear terminal locks and reconnect active runs after an ECS restart."""
        for lease in _lock_manager(self.s3, self.settings).list_leases():
            status = self._status(str(lease.owner_token))
            if status is None or status.phase not in {"SUCCEEDED", "FAILED", "STARTING_GLUE", "RUNNING_GLUE"}:
                continue
            try:
                dispatch_job(str(lease.owner_token), self.settings, self.s3, self.glue, allow_glue_start=False)
            except Exception as error:
                print(json.dumps({"mutation_id": lease.owner_token, "dispatcher_reconcile": "failed", "error": str(error)}))

    def run_forever(self) -> None:
        self.reconcile_locks()
        while True:
            self.tick()
            time.sleep(self.settings.poll_seconds)


def run_dispatcher(settings: MutationDispatcherSettings, *, s3_client: Any | None = None, sqs_client: Any | None = None, glue_client: Any | None = None) -> None:
    scheduler = MutationScheduler(
        settings,
        s3_client or boto3.client("s3", region_name=settings.region),
        sqs_client or boto3.client("sqs", region_name=settings.region),
        glue_client or boto3.client("glue", region_name=settings.region),
    )
    scheduler.run_forever()


def main() -> None:
    run_dispatcher(MutationDispatcherSettings.from_environ())


if __name__ == "__main__":
    main()

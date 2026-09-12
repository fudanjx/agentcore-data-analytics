"""Versioned, serialisable S3-backed session and job records."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class Destination(BaseModel):
    table_bucket_arn: str = Field(min_length=1)
    namespace: str = Field(pattern=r"^[a-z][a-z0-9_]{0,254}$")
    table: str = Field(pattern=r"^[a-z][a-z0-9_]{0,254}$")


class UploadSession(BaseModel):
    schema_version: Literal[1] = 1
    session_id: str
    owner_user_id: str
    file_name: str = Field(min_length=1, max_length=512)
    content_type: str = Field(min_length=1, max_length=255)
    source_key: str
    multipart_upload_id: str
    expected_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class JobSource(BaseModel):
    """An immutable raw object belonging to a single worker job."""

    name: str = Field(min_length=1, max_length=512)
    source_key: str = Field(min_length=1)
    source_version_id: str = Field(min_length=1)
    source_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    source_size_bytes: int = Field(gt=0)


class JobRequest(BaseModel):
    schema_version: Literal[1] = 1
    job_id: str
    session_id: str
    owner_user_id: str = Field(min_length=1)
    operation: Literal["create", "append"]
    destination: Destination
    source_key: str = Field(min_length=1)
    source_version_id: str = Field(min_length=1)
    source_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    source_size_bytes: int = Field(gt=0)
    # Empty means an older persisted single-file request; workers derive its
    # sole source from the fields above. New compatibility sessions populate
    # this with every immutable uploaded object.
    source_files: list[JobSource] = Field(default_factory=list)
    # New jobs use the V1-compatible immutable identifier.  The default keeps
    # already-persisted V2 requests readable during the deployment transition.
    upload_id: str = Field(default="", max_length=128)
    reporting_month: str = Field(default="", max_length=128)
    deduplication_mode: Literal["none", "keyed"] = "none"
    deduplication_columns: list[str] = Field(default_factory=list)
    manual_encryption_columns: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("source_key")
    @classmethod
    def source_must_be_raw_upload(cls, value: str) -> str:
        if "/uploads/" not in f"/{value}" or "/raw/" not in value:
            raise ValueError("source_key must reference a raw upload object")
        return value


class JobStatus(BaseModel):
    schema_version: Literal[1] = 1
    job_id: str
    phase: Literal["QUEUED", "CLAIMED", "PROFILING", "PREPARING", "READY_FOR_MUTATION", "STARTING_GLUE", "RUNNING_GLUE", "SUCCEEDED", "FAILED"]
    message: str
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    error_code: str | None = None
    glue_run_id: str | None = None


class MutationCommand(BaseModel):
    """Durable command consumed by the per-table FIFO Glue dispatcher.

    Upload jobs retain their existing immutable ``JobRequest`` records.  This
    envelope gives the dispatcher one shape for those jobs and for rollback,
    which has no uploaded source object.
    """

    schema_version: Literal[1] = 1
    mutation_id: str
    request_id: str = Field(min_length=1, max_length=128)
    owner_user_id: str = Field(min_length=1)
    operation: Literal["create", "append", "rollback"]
    destination: Destination
    upload_id: str = Field(default="", max_length=128)
    source_job_id: str | None = None
    rollback_snapshot_id: str | None = None
    original_uploaded_by: str | None = None
    original_uploaded_at: str | None = None
    reporting_month: str = Field(default="", max_length=128)
    filenames_json: str = Field(default="[]", max_length=8192)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class JobEvent(BaseModel):
    schema_version: Literal[1] = 1
    job_id: str
    sequence: int = Field(ge=1)
    status: JobStatus

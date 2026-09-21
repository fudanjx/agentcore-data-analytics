"""Worker-owned job request, status and per-file source records."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from .destination import Destination


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
    # already-persisted historical requests readable during the transition.
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
    phase: Literal[
        "QUEUED",
        "CLAIMED",
        "PROFILING",
        "PREPARING",
        "READY_FOR_MUTATION",
        "STARTING_GLUE",
        "RUNNING_GLUE",
        "SUCCEEDED",
        "FAILED",
    ]
    message: str
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    error_code: str | None = None
    glue_run_id: str | None = None

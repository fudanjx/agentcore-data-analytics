"""Durable Glue mutation command consumed by the per-table FIFO dispatcher."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

from .destination import Destination


class MutationCommand(BaseModel):
    """Durable command consumed by the per-table FIFO Glue dispatcher.

    Upload jobs retain their existing immutable ``JobRequest`` records. This
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

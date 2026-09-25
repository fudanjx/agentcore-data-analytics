"""Append-only per-job event record."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .job import JobStatus


class JobEvent(BaseModel):
    schema_version: Literal[1] = 1
    job_id: str
    sequence: int = Field(ge=1)
    status: JobStatus

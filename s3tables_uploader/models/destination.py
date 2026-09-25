"""Fully-qualified S3 Tables destination coordinates."""

from __future__ import annotations

from pydantic import BaseModel, Field


class Destination(BaseModel):
    table_bucket_arn: str = Field(min_length=1)
    namespace: str = Field(pattern=r"^[a-z][a-z0-9_]{0,254}$")
    table: str = Field(pattern=r"^[a-z][a-z0-9_]{0,254}$")

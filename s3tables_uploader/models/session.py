"""Direct-S3 upload session record."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field


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

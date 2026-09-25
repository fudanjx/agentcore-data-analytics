"""Abstract audit reader for upload-history projections."""

from __future__ import annotations

from typing import Any, Protocol


class AuditReader(Protocol):
    """Return upload-history records for a fully-qualified target table."""

    def read_entries(
        self,
        table_bucket_arn: str,
        namespace: str,
        table: str,
    ) -> list[dict[str, Any]]:
        """Return every audit entry matching the given ``(bucket, namespace, table)``.

        Entries are sorted by ``uploaded_at`` descending; the newest event per
        upload_id wins.
        """
        ...

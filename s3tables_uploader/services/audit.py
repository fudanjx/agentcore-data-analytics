"""Scoped audit-history reader.

Replaces the previous ``_history_entries`` which read three prefixes on every
request and filtered client-side. This reader only touches the canonical
scoped prefix (``<history_prefix>/<scope>/<table>/``), so listing cost stays
proportional to the target table, not to the account-wide audit trail.

Bucket and prefix are both settings-driven so staging and production can
point at different history stores.
"""

from __future__ import annotations

import json
from typing import Any

from ..core.constants import HISTORY_BUCKET, HISTORY_PREFIX
from ..utils.hashing import scope_key


class ScopedS3AuditReader:
    """Return audit entries for a specific table from the canonical prefix."""

    def __init__(
        self,
        s3_client: Any,
        history_bucket: str = HISTORY_BUCKET,
        history_prefix: str = HISTORY_PREFIX,
    ):
        self._s3 = s3_client
        self._bucket = history_bucket
        self._prefix_root = history_prefix.strip("/")

    def read_entries(
        self,
        table_bucket_arn: str,
        namespace: str,
        table: str,
    ) -> list[dict[str, Any]]:
        prefix = self._prefix(table_bucket_arn, namespace, table)
        entries: list[dict[str, Any]] = []
        paginator = self._s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
            for item in page.get("Contents", []):
                try:
                    body = self._s3.get_object(Bucket=self._bucket, Key=item["Key"])[
                        "Body"
                    ].read()
                    entry = json.loads(body)
                except Exception:
                    continue
                if (
                    entry.get("table_bucket_arn") == table_bucket_arn
                    and entry.get("namespace") == namespace
                    and entry.get("target_table") == table
                ):
                    entries.append(entry)
        latest_by_upload = {
            str(item.get("upload_id")): item
            for item in sorted(entries, key=lambda item: item.get("uploaded_at") or "")
        }
        return sorted(
            latest_by_upload.values(),
            key=lambda item: item.get("uploaded_at") or "",
            reverse=True,
        )

    def _prefix(self, table_bucket_arn: str, namespace: str, table: str) -> str:
        return f"{self._prefix_root}/{scope_key(table_bucket_arn, namespace)}/{table}/"

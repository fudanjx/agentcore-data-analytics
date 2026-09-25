"""Uploader schema and de-duplication contract service."""

from __future__ import annotations

import json
from typing import Any

from botocore.exceptions import ClientError

from ..config import Settings
from ..core.constants import S3_SSE
from ..core.exceptions import ControlPlaneError, UploaderError
from ..utils.hashing import scope_key
from ..utils.time import now_iso


class ContractService:
    """Read and mutate the durable per-table uploader contract."""

    def __init__(self, s3_client: Any, settings: Settings):
        self._s3 = s3_client
        self._settings = settings

    def _key(self, table_bucket_arn: str, namespace: str, table: str) -> str:
        scope = scope_key(table_bucket_arn, namespace)
        return f"{self._settings.contract_prefix}/{scope}/{table}.json"

    def is_uploader_managed(self, table_bucket_arn: str, namespace: str, table: str) -> bool:
        try:
            self._s3.head_object(
                Bucket=self._settings.contract_bucket,
                Key=self._key(table_bucket_arn, namespace, table),
            )
            return True
        except ClientError:
            return False

    def load(self, table_bucket_arn: str, namespace: str, table: str) -> dict[str, Any]:
        try:
            body = self._s3.get_object(
                Bucket=self._settings.contract_bucket,
                Key=self._key(table_bucket_arn, namespace, table),
            )["Body"].read()
            record = json.loads(body)
        except Exception as error:
            raise UploaderError(
                f"No uploader schema contract is available for table {table!r}",
                error_code="CONTRACT_MISSING",
            ) from error
        columns = record.get("deduplication_columns") or []
        if not isinstance(columns, list) or any(
            not isinstance(column, str) or not column for column in columns
        ):
            raise UploaderError(
                f"The stored de-duplication contract for {table!r} is invalid",
                error_code="CONTRACT_INVALID",
            )
        if len(columns) != len(set(columns)):
            raise UploaderError(
                f"The stored de-duplication contract for {table!r} contains duplicate columns",
                error_code="CONTRACT_INVALID",
            )
        record["deduplication_columns"] = columns
        return record

    def activate_late_deduplication(
        self,
        *,
        table_bucket_arn: str,
        namespace: str,
        table: str,
        contract: dict[str, Any],
        columns: list[str],
        user_id: str,
    ) -> dict[str, Any]:
        """Persist the user-selected composite key for a previously keyless table."""
        if contract["deduplication_columns"]:
            return contract
        updated = {
            **contract,
            "contract_version": max(int(contract.get("contract_version", 1)), 3),
            "deduplication_columns": columns,
            "deduplication_mode": "keyed",
            "deduplication_policy": "derived-locked-key-v3",
            "deduplication_activated_by": user_id,
            "deduplication_activated_at": now_iso(),
        }
        key = self._key(table_bucket_arn, namespace, table)
        try:
            etag = self._s3.head_object(
                Bucket=self._settings.contract_bucket, Key=key
            ).get("ETag", "").strip('"')
            self._s3.put_object(
                Bucket=self._settings.contract_bucket,
                Key=key,
                Body=json.dumps(updated, sort_keys=True).encode(),
                ContentType="application/json",
                ServerSideEncryption=S3_SSE,
                IfMatch=etag,
            )
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code")
            if code in {"PreconditionFailed", "ConditionalRequestConflict", "412"}:
                raise ControlPlaneError(
                    "Contract changed while assigning its first composite key",
                    status_code=409,
                    error_code="CONTRACT_CONFLICT",
                ) from error
            raise ControlPlaneError(
                "Unable to save the table's composite de-duplication key",
                status_code=503,
                error_code="CONTRACT_WRITE_FAILED",
            ) from error
        return updated

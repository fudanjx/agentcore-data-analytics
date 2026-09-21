"""S3 Tables bucket / namespace / table service.

Wraps the ``s3tables`` boto3 client and adds:

- Filtering to only buckets tagged ``APP=Data-Insights`` (per-app scoping so
    listings never show buckets belonging to other projects).
- Per-process tag cache (6-hour TTL) since ``list_table_buckets`` does not
    return tags inline; without the cache every listing would issue N+1
    ``list_tags_for_resource`` calls.
- Post-create tagging with automatic bucket rollback if tagging fails, so
    the account never ends up with a bucket the API would refuse to list.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

from botocore.exceptions import ClientError

from ..core.constants import (
    APP_TAG_FILTER_KEY,
    APP_TAG_FILTER_VALUE,
    APP_TAGS,
    BUCKET_TAG_CACHE_TTL_SECONDS,
)
from ..core.exceptions import ControlPlaneError


@dataclass
class _CachedTagState:
    matches_app: bool
    fetched_at: float


def control_plane_error(error: ClientError, resource: str) -> ControlPlaneError:
    """Map a boto3 ``ClientError`` from S3 Tables into a typed exception."""
    payload = error.response.get("Error", {})
    code = payload.get("Code", "S3TablesError")
    message = payload.get("Message", "S3 Tables control-plane request failed")
    if code in {"AccessDenied", "AccessDeniedException"}:
        return ControlPlaneError(f"{resource}: {message}", status_code=403, error_code=code)
    if code in {"ResourceNotFoundException", "NotFoundException"}:
        return ControlPlaneError(f"{resource}: {message}", status_code=404, error_code=code)
    if code in {"ConflictException", "AlreadyExistsException"}:
        return ControlPlaneError(f"{resource}: {message}", status_code=409, error_code=code)
    return ControlPlaneError(f"{resource}: {message}", status_code=400, error_code=code)


class TableBucketService:
    """List, create and delete S3 Tables buckets scoped to this application."""

    def __init__(
        self,
        s3tables_client: Any,
        *,
        clock: "callable[[], float]" = time.monotonic,
        cache_ttl_seconds: int = BUCKET_TAG_CACHE_TTL_SECONDS,
    ):
        self._s3tables = s3tables_client
        self._clock = clock
        self._ttl = cache_ttl_seconds
        self._tag_cache: dict[str, _CachedTagState] = {}
        self._cache_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Listing
    # ------------------------------------------------------------------

    def list_buckets(self) -> list[dict[str, str]]:
        buckets = []
        for bucket in self._list_all_customer_buckets():
            if self._bucket_matches_app(bucket["arn"]):
                buckets.append(
                    {"table_bucket_arn": bucket["arn"], "label": bucket["name"]}
                )
        return sorted(buckets, key=lambda item: item["label"])

    def _list_all_customer_buckets(self) -> list[dict[str, str]]:
        results: list[dict[str, str]] = []
        request: dict[str, str] = {}
        try:
            while True:
                response = self._s3tables.list_table_buckets(**request)
                for item in response.get("tableBuckets", []):
                    if item.get("type", "customer") != "customer":
                        continue
                    results.append(
                        {
                            "arn": item["arn"],
                            "name": item.get("name", item["arn"].rsplit("/", 1)[-1]),
                        }
                    )
                token = response.get("continuationToken")
                if not token:
                    return results
                request = {"continuationToken": token}
        except ClientError as error:
            raise control_plane_error(error, "S3 Tables buckets") from error

    def _bucket_matches_app(self, arn: str) -> bool:
        now = self._clock()
        with self._cache_lock:
            cached = self._tag_cache.get(arn)
            if cached and (now - cached.fetched_at) < self._ttl:
                return cached.matches_app
        matches = self._fetch_and_check(arn)
        with self._cache_lock:
            self._tag_cache[arn] = _CachedTagState(matches_app=matches, fetched_at=now)
        return matches

    def _fetch_and_check(self, arn: str) -> bool:
        try:
            response = self._s3tables.list_tags_for_resource(resourceARN=arn)
        except ClientError:
            return False
        tags = {tag["key"]: tag["value"] for tag in response.get("tags", [])}
        return tags.get(APP_TAG_FILTER_KEY) == APP_TAG_FILTER_VALUE

    def purge_cache(self) -> int:
        """Clear the per-process tag cache. Returns the number of dropped entries."""
        with self._cache_lock:
            count = len(self._tag_cache)
            self._tag_cache.clear()
        return count

    # ------------------------------------------------------------------
    # Bucket lifecycle
    # ------------------------------------------------------------------

    def create_bucket(self, name: str) -> dict[str, str]:
        try:
            result = self._s3tables.create_table_bucket(name=name)
        except ClientError as error:
            raise control_plane_error(error, "S3 Tables bucket") from error
        arn = result["arn"]
        try:
            self._s3tables.tag_resource(
                resourceARN=arn,
                tags=[{"key": key, "value": value} for key, value in APP_TAGS.items()],
            )
        except ClientError as tag_error:
            self._safe_delete(arn)
            raise ControlPlaneError(
                f"Unable to tag bucket {name!r}; rolled back to keep account clean",
                status_code=502,
                error_code="BUCKET_TAG_FAILED",
            ) from tag_error
        self._prime_cache(arn, matches=True)
        return {"table_bucket_arn": arn, "label": name}

    def delete_bucket(self, arn: str) -> None:
        try:
            self._s3tables.delete_table_bucket(tableBucketARN=arn)
        except ClientError as error:
            raise control_plane_error(error, "S3 Tables bucket") from error
        self._invalidate(arn)

    def _safe_delete(self, arn: str) -> None:
        try:
            self._s3tables.delete_table_bucket(tableBucketARN=arn)
        except ClientError:
            # Best-effort rollback; the outer error still surfaces.
            pass

    def _prime_cache(self, arn: str, *, matches: bool) -> None:
        with self._cache_lock:
            self._tag_cache[arn] = _CachedTagState(matches_app=matches, fetched_at=self._clock())

    def _invalidate(self, arn: str) -> None:
        with self._cache_lock:
            self._tag_cache.pop(arn, None)

    # ------------------------------------------------------------------
    # Namespace + table pass-throughs
    # ------------------------------------------------------------------

    def list_namespaces(self, table_bucket_arn: str) -> list[str]:
        namespaces: list[str] = []
        request: dict[str, str] = {"tableBucketARN": table_bucket_arn}
        try:
            while True:
                response = self._s3tables.list_namespaces(**request)
                for item in response.get("namespaces", []):
                    parts = item.get("namespace", [])
                    if len(parts) == 1:
                        namespaces.append(parts[0])
                token = response.get("continuationToken")
                if not token:
                    return sorted(namespaces)
                request = {"tableBucketARN": table_bucket_arn, "continuationToken": token}
        except ClientError as error:
            raise control_plane_error(error, "namespace") from error

    def create_namespace(self, table_bucket_arn: str, namespace: str) -> dict[str, str]:
        try:
            result = self._s3tables.create_namespace(
                tableBucketARN=table_bucket_arn, namespace=[namespace]
            )
        except ClientError as error:
            raise control_plane_error(error, "namespace") from error
        returned = result.get("namespace", [namespace])
        return {
            "table_bucket_arn": result.get("tableBucketARN", table_bucket_arn),
            "namespace": returned[0],
        }

    def list_tables(self, table_bucket_arn: str, namespace: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        request: dict[str, str] = {"tableBucketARN": table_bucket_arn, "namespace": namespace}
        try:
            while True:
                response = self._s3tables.list_tables(**request)
                for item in response.get("tables", []):
                    rows.append({"name": item["name"], "raw": item})
                token = response.get("continuationToken")
                if not token:
                    return rows
                request = {
                    "tableBucketARN": table_bucket_arn,
                    "namespace": namespace,
                    "continuationToken": token,
                }
        except ClientError as error:
            raise control_plane_error(error, "S3 Tables") from error

    def get_table(self, table_bucket_arn: str, namespace: str, name: str) -> dict[str, Any]:
        try:
            return self._s3tables.get_table(
                tableBucketARN=table_bucket_arn, namespace=namespace, name=name
            )
        except ClientError as error:
            raise control_plane_error(error, f"S3 Table {namespace}.{name}") from error

    def delete_table(self, table_bucket_arn: str, namespace: str, name: str) -> None:
        try:
            self._s3tables.delete_table(
                tableBucketARN=table_bucket_arn, namespace=namespace, name=name
            )
        except ClientError as error:
            raise control_plane_error(error, f"S3 Table {namespace}.{name}") from error

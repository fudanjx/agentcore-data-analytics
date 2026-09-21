"""S3-backed implementation of :class:`BaseStorage`."""

from __future__ import annotations

from typing import Any, Iterable

from botocore.exceptions import ClientError

from ...core.constants import S3_SSE


def _missing(error: ClientError) -> bool:
    return error.response.get("Error", {}).get("Code") in {"NoSuchKey", "404", "NotFound"}


class S3Storage:
    """Thin wrapper around a boto3 S3 client used by services that need I/O."""

    def __init__(self, s3_client: Any, bucket: str):
        self._s3 = s3_client
        self._bucket = bucket

    @property
    def client(self) -> Any:
        return self._s3

    @property
    def bucket(self) -> str:
        return self._bucket

    def get(self, key: str) -> bytes:
        try:
            return self._s3.get_object(Bucket=self._bucket, Key=key)["Body"].read()
        except ClientError as error:
            if _missing(error):
                raise KeyError(key) from error
            raise

    def put(
        self,
        key: str,
        body: bytes,
        *,
        content_type: str = "application/octet-stream",
        if_match: str | None = None,
        if_none_match: str | None = None,
    ) -> str:
        params: dict[str, Any] = {
            "Bucket": self._bucket,
            "Key": key,
            "Body": body,
            "ContentType": content_type,
            "ServerSideEncryption": S3_SSE,
        }
        if if_match is not None:
            params["IfMatch"] = if_match
        if if_none_match is not None:
            params["IfNoneMatch"] = if_none_match
        response = self._s3.put_object(**params)
        return response.get("ETag", "").strip('"')

    def delete(self, key: str) -> None:
        self._s3.delete_object(Bucket=self._bucket, Key=key)

    def head(self, key: str) -> dict[str, str]:
        try:
            response = self._s3.head_object(Bucket=self._bucket, Key=key)
        except ClientError as error:
            if _missing(error):
                raise KeyError(key) from error
            raise
        return {
            "etag": str(response.get("ETag", "").strip('"')),
            "content_type": str(response.get("ContentType", "")),
            "content_length": str(response.get("ContentLength", "")),
        }

    def list(self, prefix: str) -> Iterable[str]:
        paginator = self._s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
            for item in page.get("Contents", []):
                yield item["Key"]

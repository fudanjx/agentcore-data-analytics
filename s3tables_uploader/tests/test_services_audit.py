"""Unit tests for the scoped audit reader."""

from __future__ import annotations

import io
import json
import unittest

from s3tables_uploader.services.audit import ScopedS3AuditReader
from s3tables_uploader.utils.hashing import scope_key


class _Body:
    def __init__(self, data: bytes) -> None:
        self._stream = io.BytesIO(data)

    def read(self) -> bytes:
        return self._stream.read()


class _FakeS3:
    def __init__(self, objects: dict[str, bytes]):
        self.objects = objects

    def get_paginator(self, _name: str) -> "_FakeS3":
        return self

    def paginate(self, Bucket: str, Prefix: str):  # noqa: N803
        contents = [
            {"Key": key} for key in self.objects if key.startswith(Prefix)
        ]
        yield {"Contents": contents}

    def get_object(self, Bucket: str, Key: str) -> dict[str, _Body]:  # noqa: N803
        return {"Body": _Body(self.objects[Key])}


class ScopedS3AuditReaderTests(unittest.TestCase):
    def test_only_reads_scoped_prefix(self):
        scope = scope_key("arn:aws:s3tables:x", "ns")
        matching_prefix = f"temp_s3_update/web_ingest/upload_history/{scope}/tbl/"
        matching = {
            f"{matching_prefix}entry-1.json": json.dumps(
                {
                    "table_bucket_arn": "arn:aws:s3tables:x",
                    "namespace": "ns",
                    "target_table": "tbl",
                    "upload_id": "UPLOAD-1",
                    "uploaded_at": "2026-09-18T00:00:00Z",
                    "status": "SUCCESS",
                }
            ).encode(),
            f"{matching_prefix}entry-2.json": json.dumps(
                {
                    "table_bucket_arn": "arn:aws:s3tables:x",
                    "namespace": "ns",
                    "target_table": "tbl",
                    "upload_id": "UPLOAD-1",  # supersedes entry-1
                    "uploaded_at": "2026-09-19T00:00:00Z",
                    "status": "SUCCESS",
                }
            ).encode(),
            "some/legacy/path/entry.json": b"{}",  # never read
        }
        s3 = _FakeS3(matching)
        reader = ScopedS3AuditReader(s3)
        entries = reader.read_entries("arn:aws:s3tables:x", "ns", "tbl")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["uploaded_at"], "2026-09-19T00:00:00Z")

    def test_filters_entries_that_do_not_match_target(self):
        scope = scope_key("arn", "ns")
        prefix = f"temp_s3_update/web_ingest/upload_history/{scope}/tbl/"
        objects = {
            f"{prefix}foreign.json": json.dumps(
                {"table_bucket_arn": "other", "namespace": "ns", "target_table": "tbl"}
            ).encode(),
        }
        reader = ScopedS3AuditReader(_FakeS3(objects))
        self.assertEqual(reader.read_entries("arn", "ns", "tbl"), [])


if __name__ == "__main__":
    unittest.main()

"""Unit tests for the table bucket service (tag filtering + cache + create)."""

from __future__ import annotations

import unittest
from typing import Any

from botocore.exceptions import ClientError

from s3tables_uploader.core.constants import APP_TAGS
from s3tables_uploader.core.exceptions import ControlPlaneError
from s3tables_uploader.services.tables import TableBucketService


class _StepClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class _FakeS3Tables:
    def __init__(self, buckets: list[dict[str, Any]], tags: dict[str, dict[str, str]]):
        self._buckets = buckets
        self._tags = tags
        self.list_tag_calls: list[str] = []
        self.tag_calls: list[dict[str, Any]] = []
        self.created: list[dict[str, Any]] = []
        self.deleted: list[str] = []
        self.deleted_namespaces: list[tuple[str, str]] = []
        self.raise_on_tag = False

    def list_table_buckets(self, **_kwargs: Any) -> dict[str, Any]:
        return {"tableBuckets": self._buckets}

    def list_tags_for_resource(self, resourceArn: str) -> dict[str, Any]:  # noqa: N803
        self.list_tag_calls.append(resourceArn)
        return {"tags": dict(self._tags.get(resourceArn, {}))}

    def create_table_bucket(self, name: str) -> dict[str, str]:
        arn = f"arn:aws:s3tables:ap-southeast-1:0:bucket/{name}"
        self.created.append({"name": name, "arn": arn})
        self._buckets.append({"arn": arn, "name": name, "type": "customer"})
        return {"arn": arn}

    def tag_resource(self, resourceArn: str, tags: dict[str, str]) -> None:  # noqa: N803
        self.tag_calls.append({"arn": resourceArn, "tags": tags})
        if self.raise_on_tag:
            raise ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "no"}},
                "TagResource",
            )
        self._tags[resourceArn] = dict(tags)

    def delete_table_bucket(self, tableBucketARN: str) -> None:  # noqa: N803
        self.deleted.append(tableBucketARN)
        self._buckets = [
            item for item in self._buckets if item["arn"] != tableBucketARN
        ]

    def delete_namespace(self, tableBucketARN: str, namespace: str) -> None:  # noqa: N803
        self.deleted_namespaces.append((tableBucketARN, namespace))


class TableBucketServiceTests(unittest.TestCase):
    def _service(self) -> tuple[TableBucketService, _FakeS3Tables, _StepClock]:
        buckets = [
            {"arn": "arn:app", "name": "app", "type": "customer"},
            {"arn": "arn:other", "name": "other", "type": "customer"},
            {"arn": "arn:system", "name": "sys", "type": "system"},
        ]
        tags = {
            "arn:app": {"APP": "Data-Insights"},
            "arn:other": {"APP": "SomeOther"},
        }
        clock = _StepClock()
        client = _FakeS3Tables(buckets, tags)
        service = TableBucketService(client, clock=clock, cache_ttl_seconds=3600)
        return service, client, clock

    def test_list_buckets_filters_by_app_tag_and_skips_system(self):
        service, client, _ = self._service()
        buckets = service.list_buckets()
        self.assertEqual([b["table_bucket_arn"] for b in buckets], ["arn:app"])
        self.assertEqual(client.list_tag_calls, ["arn:app", "arn:other"])

    def test_cache_hits_avoid_repeat_tag_calls(self):
        service, client, _ = self._service()
        service.list_buckets()
        service.list_buckets()
        # Second call should reuse cache — no new tag lookups.
        self.assertEqual(len(client.list_tag_calls), 2)

    def test_cache_expiry_refetches(self):
        service, client, clock = self._service()
        service.list_buckets()
        clock.advance(4000)
        service.list_buckets()
        self.assertEqual(len(client.list_tag_calls), 4)

    def test_create_bucket_applies_tags_and_primes_cache(self):
        service, client, _ = self._service()
        result = service.create_bucket("new-thing")
        self.assertIn(result["table_bucket_arn"], {c["arn"] for c in client.created})
        self.assertEqual(len(client.tag_calls), 1)
        self.assertEqual(client.tag_calls[0]["tags"], APP_TAGS)
        # Cached as matching → listing includes it without another tag call.
        before_list_calls = len(client.list_tag_calls)
        arns = [b["table_bucket_arn"] for b in service.list_buckets()]
        self.assertIn(result["table_bucket_arn"], arns)
        # Only the pre-existing buckets triggered new tag lookups; the new
        # bucket was already primed.
        self.assertEqual(len(client.list_tag_calls) - before_list_calls, 2)

    def test_create_bucket_rolls_back_on_tag_failure(self):
        service, client, _ = self._service()
        client.raise_on_tag = True
        with self.assertRaises(ControlPlaneError) as ctx:
            service.create_bucket("doomed")
        self.assertEqual(ctx.exception.status_code, 502)
        self.assertEqual(len(client.deleted), 1)
        self.assertTrue(client.deleted[0].endswith("doomed"))

    def test_purge_cache_returns_dropped_count(self):
        service, _, _ = self._service()
        service.list_buckets()
        purged = service.purge_cache()
        self.assertGreaterEqual(purged, 1)
        self.assertEqual(service.purge_cache(), 0)

    def test_delete_namespace_passes_bucket_and_namespace(self):
        service, client, _ = self._service()
        service.delete_namespace("arn:app", "reporting")
        self.assertEqual(client.deleted_namespaces, [("arn:app", "reporting")])

    def test_delete_bucket_invalidates_cached_tag_state(self):
        service, client, _ = self._service()
        service.list_buckets()
        service.delete_bucket("arn:app")
        self.assertEqual(client.deleted[-1], "arn:app")
        self.assertNotIn("arn:app", [item["table_bucket_arn"] for item in service.list_buckets()])


if __name__ == "__main__":
    unittest.main()

"""Unit tests for the mutation enqueuer and identity profile service."""

from __future__ import annotations

import unittest
from dataclasses import dataclass

from s3tables_uploader.core.exceptions import TableBucketForbidden
from s3tables_uploader.models import Destination
from s3tables_uploader.services.mutations import MutationEnqueuerService
from s3tables_uploader.services.profiles import LocalIdentityProfileService


@dataclass
class _StubSettings:
    mutation_queue_url: str = "https://sqs.example/mutations"


class _FakeSqs:
    def __init__(self) -> None:
        self.sent: list[dict[str, str]] = []

    def send_message(self, **kwargs: str) -> None:
        self.sent.append(kwargs)


class MutationEnqueuerServiceTests(unittest.TestCase):
    def test_enqueue_uses_deterministic_group_id_per_destination(self):
        sqs = _FakeSqs()
        service = MutationEnqueuerService(sqs, _StubSettings())
        destination = Destination(
            table_bucket_arn="arn", namespace="ns", table="tbl"
        )
        service.enqueue("mut-1", destination)
        service.enqueue("mut-2", destination)
        group_ids = {message["MessageGroupId"] for message in sqs.sent}
        self.assertEqual(len(group_ids), 1)
        self.assertEqual(len(sqs.sent), 2)
        # Deduplication ids match the mutation ids so accidental replays
        # coalesce.
        self.assertEqual(
            [m["MessageDeduplicationId"] for m in sqs.sent], ["mut-1", "mut-2"]
        )


class LocalIdentityProfileServiceTests(unittest.TestCase):
    def test_admin_profile_resolves_with_flags(self):
        service = LocalIdentityProfileService()
        user = service.resolve("local-admin")
        self.assertTrue(user.is_admin)
        self.assertTrue(user.can_view_upload_history)
        self.assertEqual(user.user_id, "local-admin")

    def test_editor_has_bucket_grants(self):
        service = LocalIdentityProfileService()
        user = service.resolve("local-editor")
        self.assertFalse(user.is_admin)
        self.assertTrue(user.visible_buckets)
        self.assertEqual(user.visible_buckets[0].namespace, "pilot")

    def test_unknown_user_forbidden(self):
        service = LocalIdentityProfileService()
        with self.assertRaises(TableBucketForbidden):
            service.resolve("nobody")

    def test_list_profiles_reports_expected_access(self):
        service = LocalIdentityProfileService()
        rows = service.list_profiles()
        by_user = {row["user_id"]: row for row in rows}
        self.assertTrue(by_user["local-admin"]["expected_access"])
        self.assertFalse(by_user["local-unassigned"]["expected_access"])


if __name__ == "__main__":
    unittest.main()

"""Per-table FIFO Glue mutation enqueuer."""

from __future__ import annotations

import hashlib
from typing import Any

from ..config import Settings
from ..models import Destination


class MutationEnqueuerService:
    """Publish idempotent durable Glue mutation commands to their table group."""

    def __init__(self, sqs_client: Any, settings: Settings):
        self._sqs = sqs_client
        self._settings = settings

    def enqueue(self, mutation_id: str, destination: Destination) -> None:
        self._sqs.send_message(
            QueueUrl=self._settings.mutation_queue_url,
            MessageBody=mutation_id,
            MessageDeduplicationId=mutation_id,
            MessageGroupId=self._group_id(destination),
        )

    @staticmethod
    def _group_id(destination: Destination) -> str:
        payload = f"{destination.table_bucket_arn}\x1f{destination.namespace}\x1f{destination.table}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

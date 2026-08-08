"""Safely inventory or delete all records from one AgentCore long-term memory.

The script is dry-run by default. Deletion requires both ``--execute`` and an
exact ``--confirm-memory-id`` match. It does not delete short-term events, the
Memory resource, or its strategies.
"""

import argparse
import os
from collections import Counter
from collections.abc import Sequence

import boto3


ROOT_NAMESPACE_PATH = "/"
DELETE_BATCH_SIZE = 100


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Inventory or delete all long-term records in AgentCore Memory."
    )
    parser.add_argument(
        "--memory-id",
        required=True,
        help="Exact AgentCore Memory ID to inspect or clear.",
    )
    parser.add_argument(
        "--region",
        default=os.environ.get("MEMORY_REGION", "ap-southeast-1"),
        help="AWS region (default: MEMORY_REGION or ap-southeast-1).",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Delete the records. Without this flag, the script is read-only.",
    )
    parser.add_argument(
        "--confirm-memory-id",
        help="Required with --execute; must exactly match --memory-id.",
    )
    args = parser.parse_args(argv)

    if args.execute and args.confirm_memory_id != args.memory_id:
        parser.error(
            "--execute requires --confirm-memory-id to exactly match --memory-id"
        )
    return args


def _list_records(client, memory_id: str) -> list[dict]:
    """List every long-term record under all namespace hierarchies."""
    paginator = client.get_paginator("list_memory_records")
    records: list[dict] = []
    seen_ids: set[str] = set()
    for page in paginator.paginate(
        memoryId=memory_id,
        namespacePath=ROOT_NAMESPACE_PATH,
        PaginationConfig={"PageSize": 100},
    ):
        for record in page.get("memoryRecordSummaries", []):
            record_id = record.get("memoryRecordId")
            if isinstance(record_id, str) and record_id and record_id not in seen_ids:
                seen_ids.add(record_id)
                records.append(record)
    return records


def _print_inventory(records: list[dict]) -> None:
    print(f"Long-term records found: {len(records)}")
    counts = Counter(
        record.get("memoryStrategyId") or "<unknown strategy>" for record in records
    )
    for strategy_id, count in sorted(counts.items()):
        print(f"  {strategy_id}: {count}")


def _delete_records(client, memory_id: str, records: list[dict]) -> int:
    deleted = 0
    for offset in range(0, len(records), DELETE_BATCH_SIZE):
        batch = records[offset : offset + DELETE_BATCH_SIZE]
        response = client.batch_delete_memory_records(
            memoryId=memory_id,
            records=[{"memoryRecordId": record["memoryRecordId"]} for record in batch],
        )
        failed = response.get("failedRecords", [])
        if failed:
            details = ", ".join(
                f"{item.get('memoryRecordId', '<unknown>')}: "
                f"{item.get('errorMessage', item.get('status', 'unknown error'))}"
                for item in failed
            )
            raise RuntimeError(f"Batch deletion failed for {len(failed)} record(s): {details}")
        deleted += len(response.get("successfulRecords", []))
    return deleted


def main(argv: Sequence[str] | None = None, *, client=None) -> int:
    args = _parse_args(argv)
    if client is None:
        client = boto3.client("bedrock-agentcore", region_name=args.region)

    print(f"Memory ID: {args.memory_id}")
    print(f"Region: {args.region}")
    records = _list_records(client, args.memory_id)
    _print_inventory(records)

    if not args.execute:
        print("DRY RUN: no records were deleted.")
        print(
            "To delete them, rerun with --execute and "
            f"--confirm-memory-id {args.memory_id}"
        )
        return 0

    if not records:
        print("Nothing to delete.")
        return 0

    deleted = _delete_records(client, args.memory_id, records)
    print(f"Deletion API accepted {deleted} of {len(records)} record(s).")

    remaining = _list_records(client, args.memory_id)
    if remaining:
        print(
            f"WARNING: {len(remaining)} long-term record(s) remain. "
            "They may have been created by an in-flight extraction job; rerun "
            "the script after extraction completes."
        )
        return 1

    print("Verification complete: no long-term records remain.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

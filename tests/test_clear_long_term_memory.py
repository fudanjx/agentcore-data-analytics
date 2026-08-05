import importlib.util
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).parents[1] / "infra" / "clear_long_term_memory.py"
SPEC = importlib.util.spec_from_file_location("clear_long_term_memory_tests", MODULE_PATH)
clear_memory = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(clear_memory)


class FakePaginator:
    def __init__(self, client):
        self.client = client

    def paginate(self, **kwargs):
        self.client.list_calls.append(kwargs)
        yield {"memoryRecordSummaries": list(self.client.records)}


class FakeClient:
    def __init__(self, count=3):
        self.records = [
            {
                "memoryRecordId": f"mem-{index:040d}",
                "memoryStrategyId": "semantic-id" if index % 2 else "summary-id",
            }
            for index in range(count)
        ]
        self.list_calls = []
        self.delete_calls = []

    def get_paginator(self, operation_name):
        assert operation_name == "list_memory_records"
        return FakePaginator(self)

    def batch_delete_memory_records(self, **kwargs):
        self.delete_calls.append(kwargs)
        deleted_ids = {record["memoryRecordId"] for record in kwargs["records"]}
        self.records = [
            record
            for record in self.records
            if record["memoryRecordId"] not in deleted_ids
        ]
        return {
            "successfulRecords": [
                {"memoryRecordId": record_id} for record_id in deleted_ids
            ],
            "failedRecords": [],
        }


def test_dry_run_only_inventories_records(capsys):
    client = FakeClient()

    result = clear_memory.main(["--memory-id", "memory-example-1234567890"], client=client)

    assert result == 0
    assert client.delete_calls == []
    assert "DRY RUN: no records were deleted." in capsys.readouterr().out
    assert client.list_calls == [
        {
            "memoryId": "memory-example-1234567890",
            "namespacePath": "/",
            "PaginationConfig": {"PageSize": 100},
        }
    ]


def test_execute_requires_exact_memory_id_confirmation():
    with pytest.raises(SystemExit) as error:
        clear_memory.main(
            [
                "--memory-id",
                "memory-example-1234567890",
                "--execute",
                "--confirm-memory-id",
                "a-different-memory-id",
            ],
            client=FakeClient(),
        )

    assert error.value.code == 2


def test_execute_deletes_in_batches_and_verifies(capsys):
    client = FakeClient(count=101)
    memory_id = "memory-example-1234567890"

    result = clear_memory.main(
        [
            "--memory-id",
            memory_id,
            "--execute",
            "--confirm-memory-id",
            memory_id,
        ],
        client=client,
    )

    assert result == 0
    assert [len(call["records"]) for call in client.delete_calls] == [100, 1]
    assert all(call["memoryId"] == memory_id for call in client.delete_calls)
    assert client.records == []
    assert len(client.list_calls) == 2
    assert "Verification complete: no long-term records remain." in capsys.readouterr().out

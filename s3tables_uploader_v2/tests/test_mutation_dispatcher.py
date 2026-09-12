import io
import unittest

from botocore.exceptions import ClientError

from s3tables_uploader_v2.job_store import S3JobStore
from s3tables_uploader_v2.models import Destination, JobRequest, JobStatus
from s3tables_uploader_v2.mutation_dispatcher import MutationDispatcherSettings, MutationScheduler, dispatch_job


class FakeS3:
    def __init__(self):
        self.items = {}
        self.version = 0

    @staticmethod
    def _error(code):
        return ClientError({"Error": {"Code": code}}, "S3")

    def put_object(self, Bucket, Key, Body, **kwargs):
        if kwargs.get("IfNoneMatch") == "*" and Key in self.items:
            raise self._error("PreconditionFailed")
        if kwargs.get("IfMatch") and (Key not in self.items or kwargs["IfMatch"] != self.items[Key][1]):
            raise self._error("PreconditionFailed")
        self.version += 1
        self.items[Key] = (bytes(Body), f"etag-{self.version}")
        return {"ETag": f"etag-{self.version}"}

    def get_object(self, Bucket, Key):
        try:
            body, etag = self.items[Key]
        except KeyError as error:
            raise self._error("NoSuchKey") from error
        return {"Body": io.BytesIO(body), "ETag": etag}

    def delete_object(self, Bucket, Key, **kwargs):
        if kwargs.get("IfMatch") != self.items[Key][1]:
            raise self._error("PreconditionFailed")
        del self.items[Key]

    def get_paginator(self, operation):
        assert operation == "list_objects_v2"
        client = self

        class Paginator:
            def paginate(self, Bucket, Prefix):
                yield {"Contents": [{"Key": key} for key in sorted(client.items) if key.startswith(Prefix)]}

        return Paginator()


class FakeGlue:
    def __init__(self):
        self.calls = []
        self.states = {}
        self.capacity_error = False

    def start_job_run(self, **kwargs):
        if self.capacity_error:
            raise ClientError({"Error": {"Code": "ConcurrentRunsExceededException"}}, "StartJobRun")
        self.calls.append(kwargs)
        run_id = f"jr-{len(self.calls)}"
        self.states[run_id] = "RUNNING"
        return {"JobRunId": run_id}

    def get_job_run(self, JobName, RunId, PredecessorsIncluded):
        return {"JobRun": {"JobRunState": self.states[RunId]}}

    def get_job_runs(self, JobName, MaxResults):
        return {"JobRuns": []}


class FakeSqs:
    def __init__(self):
        self.messages = []
        self.deleted = []
        self.visibility_changes = []

    def receive_message(self, **kwargs):
        messages, self.messages = self.messages, []
        return {"Messages": messages}

    def delete_message(self, **kwargs):
        self.deleted.append(kwargs)

    def change_message_visibility(self, **kwargs):
        self.visibility_changes.append(kwargs)


class MutationDispatcherTests(unittest.TestCase):
    def setUp(self):
        self.s3 = FakeS3()
        self.glue = FakeGlue()
        self.settings = MutationDispatcherSettings("region", "landing", "prefix", "queue", "glue")
        self.store = S3JobStore(self.s3, "landing", "prefix")

    def _ready_job(self, job_id, *, table="target"):
        request = JobRequest(
            job_id=job_id, session_id=f"session-{job_id}", owner_user_id="user", operation="append",
            destination=Destination(table_bucket_arn="arn", namespace="pilot", table=table),
            source_key=f"prefix/uploads/{job_id}/raw/input.parquet", source_version_id="version", source_size_bytes=1,
        )
        self.store.put_request(request)
        self.store.put_status(JobStatus(job_id=job_id, phase="READY_FOR_MUTATION", message="prepared"))

    def test_same_table_starts_one_glue_run_at_a_time_and_releases_on_terminal_state(self):
        self._ready_job("one")
        self._ready_job("two")

        self.assertEqual(dispatch_job("one", self.settings, self.s3, self.glue), "waiting")
        self.assertEqual(len(self.glue.calls), 1)
        self.assertNotIn("--LOCK_BUCKET", self.glue.calls[0]["Arguments"])
        self.assertNotIn("--QUEUE_BUCKET", self.glue.calls[0]["Arguments"])

        self.assertEqual(dispatch_job("two", self.settings, self.s3, self.glue), "waiting")
        self.assertEqual(len(self.glue.calls), 1)

        self.glue.states["jr-1"] = "SUCCEEDED"
        self.assertEqual(dispatch_job("one", self.settings, self.s3, self.glue), "terminal")
        self.assertEqual(self.store.get_status("one").status.phase, "SUCCEEDED")

        self.assertEqual(dispatch_job("two", self.settings, self.s3, self.glue), "waiting")
        self.assertEqual(len(self.glue.calls), 2)

    def test_one_dispatcher_starts_different_tables_concurrently_but_holds_same_table(self):
        self._ready_job("a-one", table="a")
        self._ready_job("a-two", table="a")
        self._ready_job("b-one", table="b")
        sqs = FakeSqs()
        sqs.messages = [
            {"Body": "a-one", "ReceiptHandle": "receipt-a-one"},
            {"Body": "a-two", "ReceiptHandle": "receipt-a-two"},
            {"Body": "b-one", "ReceiptHandle": "receipt-b-one"},
        ]
        scheduler = MutationScheduler(self.settings, self.s3, sqs, self.glue)

        scheduler.tick(now=0)

        self.assertEqual(len(self.glue.calls), 2)
        self.assertEqual(self.store.get_status("a-one").status.phase, "RUNNING_GLUE")
        self.assertEqual(self.store.get_status("a-two").status.phase, "READY_FOR_MUTATION")
        self.assertEqual(self.store.get_status("b-one").status.phase, "RUNNING_GLUE")

        self.glue.states["jr-1"] = "SUCCEEDED"
        self.glue.states["jr-2"] = "SUCCEEDED"
        scheduler.tick(now=40)

        self.assertEqual(len(self.glue.calls), 3)
        self.assertEqual(self.store.get_status("a-two").status.phase, "RUNNING_GLUE")
        self.assertEqual(len(sqs.deleted), 2)

    def test_restart_counts_recovered_glue_lock_before_starting_another_table(self):
        limited = MutationDispatcherSettings("region", "landing", "prefix", "queue", "glue", max_concurrent_glue=1)
        self._ready_job("already-running", table="a")
        self.assertEqual(dispatch_job("already-running", limited, self.s3, self.glue), "waiting")
        self._ready_job("waiting", table="b")
        sqs = FakeSqs()
        sqs.messages = [{"Body": "waiting", "ReceiptHandle": "receipt-waiting"}]

        MutationScheduler(limited, self.s3, sqs, self.glue).tick(now=0)

        self.assertEqual(len(self.glue.calls), 1)
        self.assertEqual(self.store.get_status("waiting").status.phase, "READY_FOR_MUTATION")

    def test_glue_capacity_response_returns_the_mutation_to_ready_without_losing_fifo_receipt(self):
        self._ready_job("capacity", table="a")
        self.glue.capacity_error = True

        self.assertEqual(dispatch_job("capacity", self.settings, self.s3, self.glue), "waiting")

        self.assertEqual(self.store.get_status("capacity").status.phase, "READY_FOR_MUTATION")
        self.assertFalse(any(key.startswith("prefix/table-locks/") for key in self.s3.items))

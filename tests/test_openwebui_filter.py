import asyncio
import base64
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


FILTER_PATH = (
    Path(__file__).parents[1]
    / "openwebui-local"
    / "functions"
    / "agentcore_file_context.py"
)
INSIGHTS_FILTER_PATH = (
    Path(__file__).parents[1]
    / "openwebui-insights"
    / "functions"
    / "agentcore_file_context.py"
)
USER_ID = "user-1"
CHAT_ID = "chat-1"


class FakeChats:
    def __init__(self, chat):
        self.chat = chat
        self.calls = []

    async def get_chat_by_id_and_user_id(self, chat_id, user_id):
        self.calls.append((chat_id, user_id))
        return self.chat


class FakeFiles:
    def __init__(self, records):
        self.records = records
        self.calls = []

    async def get_file_by_id_and_user_id(self, file_id, user_id):
        self.calls.append((file_id, user_id))
        record = self.records.get(file_id)
        if record and record.user_id == user_id:
            return record
        return None

    async def get_file_by_id(self, file_id):
        return self.records.get(file_id)

    async def insert_new_file(self, user_id, form):
        record = SimpleNamespace(
            id=form.id,
            user_id=user_id,
            filename=form.filename,
            path=form.path,
            meta=form.meta,
        )
        self.records[form.id] = record
        return record


class FakeFileForm:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def load_filter_module(path=FILTER_PATH):
    spec = importlib.util.spec_from_file_location("agentcore_file_context_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class OpenWebUIFilterTests(unittest.TestCase):
    def setUp(self):
        self.chat = SimpleNamespace(
            chat={
                "history": {
                    "messages": {
                        "message-1": {"files": [{"id": "file-1"}]},
                        "message-2": {
                            "files": [
                                {"id": "file-2"},
                                {"id": "file-1"},
                            ]
                        },
                    }
                }
            }
        )
        self.records = {
            "file-1": SimpleNamespace(
                id="file-1",
                user_id=USER_ID,
                filename="costs.csv",
                path="s3://test/openwebui-test/file-1_costs.csv",
                meta={"content_type": "text/csv", "size": 10},
            ),
            "file-2": SimpleNamespace(
                id="file-2",
                user_id=USER_ID,
                filename="notes.txt",
                path="s3://test/openwebui-test/file-2_notes.txt",
                meta={"content_type": "text/plain", "size": 20},
            ),
        }

    def install_fake_openwebui_modules(self, chats, files):
        chats_module = types.ModuleType("open_webui.models.chats")
        chats_module.Chats = chats
        files_module = types.ModuleType("open_webui.models.files")
        files_module.Files = files
        files_module.FileForm = FakeFileForm
        return {
            "open_webui": types.ModuleType("open_webui"),
            "open_webui.models": types.ModuleType("open_webui.models"),
            "open_webui.models.chats": chats_module,
            "open_webui.models.files": files_module,
        }

    def test_agentcore_request_gets_chat_wide_owned_manifest(self):
        module = load_filter_module()
        chats = FakeChats(self.chat)
        files = FakeFiles(self.records)
        body = {
            "model": "agentcore.harness",
            "files": [{"id": "file-1"}],
            "metadata": {"files": [{"id": "file-1"}]},
            "messages": [{"role": "user", "content": "Analyze them"}],
        }

        with patch.dict(
            sys.modules,
            self.install_fake_openwebui_modules(chats, files),
        ):
            result = asyncio.run(
                module.Filter().inlet(
                    body,
                    __user__={"id": USER_ID},
                    __metadata__=body["metadata"],
                    __chat_id__=CHAT_ID,
                )
            )

        self.assertEqual(
            [item["file_id"] for item in result["agentcore_files"]],
            ["file-1", "file-2"],
        )
        self.assertEqual(result["agentcore_request_context"], {"kind": "chat"})
        self.assertNotIn("files", result)
        self.assertNotIn("files", result["metadata"])
        self.assertEqual(chats.calls, [(CHAT_ID, USER_ID)])
        self.assertEqual(files.calls, [("file-1", USER_ID), ("file-2", USER_ID)])

    def test_background_task_is_isolated_and_does_not_forward_chat_files(self):
        module = load_filter_module()
        chats = FakeChats(self.chat)
        files = FakeFiles(self.records)
        body = {
            "model": "agentcore.harness",
            "files": [{"id": "file-1"}],
            "metadata": {
                "task": "follow_up_generation",
                "files": [{"id": "file-1"}],
            },
            "messages": [{"role": "user", "content": "Generate follow-ups"}],
        }

        with patch.dict(
            sys.modules,
            self.install_fake_openwebui_modules(chats, files),
        ):
            result = asyncio.run(
                module.Filter().inlet(
                    body,
                    __user__={"id": USER_ID},
                    __metadata__=body["metadata"],
                    __chat_id__=CHAT_ID,
                )
            )

        self.assertEqual(
            result["agentcore_request_context"],
            {"kind": "background", "task": "follow_up_generation"},
        )
        self.assertEqual(result["agentcore_files"], [])
        self.assertNotIn("files", result)
        self.assertNotIn("files", result["metadata"])
        self.assertEqual(chats.calls, [(CHAT_ID, USER_ID)])
        self.assertEqual(files.calls, [])

    def test_non_agentcore_model_is_unchanged(self):
        module = load_filter_module()
        chats = FakeChats(self.chat)
        files = FakeFiles(self.records)
        body = {
            "model": "gpt-5",
            "files": [{"id": "file-1"}],
            "metadata": {"files": [{"id": "file-1"}]},
        }
        original = {
            "model": body["model"],
            "files": list(body["files"]),
            "metadata": {"files": list(body["metadata"]["files"])},
        }

        with patch.dict(
            sys.modules,
            self.install_fake_openwebui_modules(chats, files),
        ):
            result = asyncio.run(
                module.Filter().inlet(
                    body,
                    __user__={"id": USER_ID},
                    __metadata__=body["metadata"],
                    __chat_id__=CHAT_ID,
                )
            )

        self.assertEqual(result, original)
        self.assertEqual(chats.calls, [])
        self.assertEqual(files.calls, [])

    def test_insights_filter_is_scoped_to_the_insights_model(self):
        module = load_filter_module(INSIGHTS_FILTER_PATH)
        chats = FakeChats(self.chat)
        files = FakeFiles(self.records)
        body = {
            "model": "agentcore.insights",
            "messages": [{"role": "user", "content": "Analyze them"}],
        }

        with patch.dict(
            sys.modules,
            self.install_fake_openwebui_modules(chats, files),
        ):
            result = asyncio.run(
                module.Filter().inlet(
                    body,
                    __user__={"id": USER_ID},
                    __metadata__={},
                    __chat_id__=CHAT_ID,
                )
            )

        self.assertEqual(result["agentcore_request_context"], {"kind": "chat"})
        self.assertEqual(
            [item["file_id"] for item in result["agentcore_files"]],
            ["file-1", "file-2"],
        )

    def test_office_model_uses_the_same_owned_file_manifest(self):
        module = load_filter_module(INSIGHTS_FILTER_PATH)
        chats = FakeChats(self.chat)
        files = FakeFiles(self.records)
        body = {
            "model": "agentcore-office.insights-office",
            "messages": [{"role": "user", "content": "Create a workbook"}],
        }

        with patch.dict(
            sys.modules,
            self.install_fake_openwebui_modules(chats, files),
        ):
            result = asyncio.run(
                module.Filter().inlet(
                    body,
                    __user__={"id": USER_ID},
                    __metadata__={},
                    __chat_id__=CHAT_ID,
                )
            )

        self.assertEqual(result["agentcore_request_context"], {"kind": "chat"})
        self.assertEqual(
            [item["file_id"] for item in result["agentcore_files"]],
            ["file-1", "file-2"],
        )

    def test_all_configured_runtime_models_use_the_owned_file_manifest(self):
        module = load_filter_module(INSIGHTS_FILTER_PATH)
        for model in (
            "agentcore-strands.strands",
            "agentcore-office.insights-office",
            "agentcore-gmio.gmio-pcr-dev",
        ):
            with self.subTest(model=model):
                chats = FakeChats(self.chat)
                files = FakeFiles(dict(self.records))
                body = {
                    "model": model,
                    "messages": [{"role": "user", "content": "Analyze them"}],
                }
                with patch.dict(
                    sys.modules,
                    self.install_fake_openwebui_modules(chats, files),
                ):
                    result = asyncio.run(
                        module.Filter().inlet(
                            body,
                            __user__={"id": USER_ID},
                            __metadata__={},
                            __chat_id__=CHAT_ID,
                        )
                    )
                self.assertEqual(
                    [item["file_id"] for item in result["agentcore_files"]],
                    ["file-1", "file-2"],
                )

    def test_office_status_marker_becomes_a_native_status_event(self):
        module = load_filter_module(INSIGHTS_FILTER_PATH)
        emitted = []

        async def emit(event):
            emitted.append(event)

        event = {
            "choices": [
                {
                    "delta": {
                        "content": (
                            '<!--agentcore-status:{"description":"Running Code '
                            'Interpreter","done":false}-->'
                        )
                    }
                }
            ]
        }
        result = asyncio.run(
            module.Filter().stream(
                event,
                __event_emitter__=emit,
                __body__={"model": "agentcore-office.insights-office"},
            )
        )
        self.assertEqual(result["choices"][0]["delta"]["content"], "")
        self.assertEqual(emitted[0]["type"], "status")
        self.assertEqual(
            emitted[0]["data"]["description"], "Running Code Interpreter"
        )

    def test_embedded_status_markers_are_removed_without_losing_prose(self):
        module = load_filter_module(INSIGHTS_FILTER_PATH)
        emitted = []

        async def emit(event):
            emitted.append(event)

        event = {
            "choices": [
                {
                    "delta": {
                        "content": (
                            "Preparing dashboard."
                            '<!--agentcore-status:{"description":"Starting tool: execute code","done":false}-->'
                            "Dashboard created."
                            '<!--agentcore-status:{"description":"Completed tool: execute code","done":true}-->'
                        )
                    }
                }
            ]
        }
        result = asyncio.run(
            module.Filter().stream(
                event,
                __event_emitter__=emit,
                __body__={"model": "agentcore-strands.strands"},
            )
        )

        content = result["choices"][0]["delta"]["content"]
        self.assertEqual(content, "Preparing dashboard.Dashboard created.")
        self.assertNotIn("agentcore-status", content)
        self.assertEqual(
            [item["data"]["description"] for item in emitted],
            ["Starting tool: execute code", "Completed tool: execute code"],
        )

    def test_status_markers_are_handled_for_every_runtime_model(self):
        module = load_filter_module(INSIGHTS_FILTER_PATH)

        async def run(model):
            emitted = []

            async def emit(event):
                emitted.append(event)

            event = {
                "choices": [{
                    "delta": {"content": '<!--agentcore-status:{"description":"Starting tool: SQL query","done":false}-->'}
                }]
            }
            result = await module.Filter().stream(
                event,
                __event_emitter__=emit,
                __body__={"model": model},
            )
            return result, emitted

        for model in (
            "agentcore-strands.strands",
            "agentcore-office.insights-office",
            "agentcore-gmio.gmio-pcr-dev",
        ):
            with self.subTest(model=model):
                result, emitted = asyncio.run(run(model))
                self.assertEqual(result["choices"][0]["delta"]["content"], "")
                self.assertEqual(
                    emitted[0]["data"]["description"], "Starting tool: SQL query"
                )

    def test_office_stream_artifact_marker_becomes_owned_download_link(self):
        module = load_filter_module(INSIGHTS_FILTER_PATH)
        files = FakeFiles(self.records)
        artifact = {
            "s3_uri": "s3://agentcore-openwebui-insights-964340114883/"
            "openwebui-insights/outputs/user-1/chat-1/report.xlsx",
            "filename": "report.xlsx",
            "mime_type": "application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet",
            "size": 120,
        }
        event = {
            "choices": [
                {
                    "delta": {
                        "content": "<!--agentcore-artifacts:%s-->"
                        % base64.urlsafe_b64encode(
                            __import__("json")
                            .dumps([artifact], separators=(",", ":"))
                            .encode("utf-8")
                        ).decode("ascii")
                    },
                    "finish_reason": None,
                }
            ]
        }
        with (
            patch.dict(sys.modules, self.install_fake_openwebui_modules(FakeChats(self.chat), files)),
            patch.object(module, "_validate_artifacts_with_proxy", return_value=[artifact]) as validate,
        ):
            result = asyncio.run(
                module.Filter().stream(
                    event,
                    __body__={"model": "agentcore-office.insights-office"},
                    __user__={"id": USER_ID},
                    __metadata__={"chat_id": CHAT_ID},
                )
            )

        content = result["choices"][0]["delta"]["content"]
        self.assertIn("[Download report.xlsx]", content)
        self.assertIn("/api/v1/files/", content)
        self.assertNotIn("s3://", content)
        self.assertEqual(len(files.records), 3)
        self.assertEqual(validate.call_args.args[3], "insights-office")

    def test_html_download_link_is_always_an_attachment(self):
        module = load_filter_module(INSIGHTS_FILTER_PATH)
        files = FakeFiles(self.records)
        artifact = {
            "s3_uri": "s3://agentcore-openwebui-insights-964340114883/"
            "openwebui-insights/outputs/user-1/chat-1/dashboard.html",
            "filename": "dashboard.html",
            "mime_type": "text/html",
            "size": 120,
        }
        marker = base64.urlsafe_b64encode(
            __import__("json").dumps([artifact], separators=(",", ":")).encode()
        ).decode()
        event = {"choices": [{"delta": {"content": f"<!--agentcore-artifacts:{marker}-->"}}]}
        with (
            patch.dict(sys.modules, self.install_fake_openwebui_modules(FakeChats(self.chat), files)),
            patch.object(module, "_validate_artifacts_with_proxy", return_value=[artifact]),
        ):
            result = asyncio.run(
                module.Filter().stream(
                    event,
                    __body__={"model": "agentcore-strands.strands"},
                    __user__={"id": USER_ID},
                    __metadata__={"chat_id": CHAT_ID},
                )
            )
        self.assertIn("?attachment=true", result["choices"][0]["delta"]["content"])

    def test_office_final_chunk_closes_native_status(self):
        module = load_filter_module(INSIGHTS_FILTER_PATH)
        emitted = []

        async def emit(event):
            emitted.append(event)

        event = {"choices": [{"delta": {}, "finish_reason": "stop"}]}
        result = asyncio.run(
            module.Filter().stream(
                event,
                __event_emitter__=emit,
                __body__={"model": "agentcore-office.insights-office"},
            )
        )
        self.assertIs(result, event)
        self.assertEqual(emitted, [{"type": "status", "data": {
            "description": "", "done": True, "hidden": True,
        }}])

    def test_unowned_chat_file_is_rejected(self):
        module = load_filter_module()
        chats = FakeChats(self.chat)
        files = FakeFiles(
            {
                **self.records,
                "file-2": SimpleNamespace(
                    **{
                        **self.records["file-2"].__dict__,
                        "user_id": "another-user",
                    }
                ),
            }
        )
        body = {
            "model": "harness",
            "metadata": {"files": [{"id": "file-1"}]},
        }

        with patch.dict(
            sys.modules,
            self.install_fake_openwebui_modules(chats, files),
        ):
            with self.assertRaisesRegex(PermissionError, "not accessible"):
                asyncio.run(
                    module.Filter().inlet(
                        body,
                        __user__={"id": USER_ID},
                        __metadata__=body["metadata"],
                        __chat_id__=CHAT_ID,
                    )
                )


if __name__ == "__main__":
    unittest.main()

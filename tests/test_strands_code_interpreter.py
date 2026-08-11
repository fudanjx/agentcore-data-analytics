import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path


def _load_code_interpreter(monkeypatch):
    strands = types.ModuleType("strands")

    def fake_tool(**_kwargs):
        return lambda function: function

    strands.tool = fake_tool
    monkeypatch.setitem(sys.modules, "strands", strands)
    module_path = Path(__file__).parents[1] / "Strands-runtime" / "code_interpreter.py"
    spec = importlib.util.spec_from_file_location(
        "strands_runtime_code_interpreter", module_path
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_stages_only_resolved_skill_resource_in_active_session(monkeypatch):
    module = _load_code_interpreter(monkeypatch)
    invocations = []

    async def fake_invoke(session_id, name, arguments):
        invocations.append((session_id, name, arguments))
        return json.dumps([{"isError": False, "structuredContent": {"exitCode": 0}}])

    monkeypatch.setattr(module, "_invoke_tool", fake_invoke)
    tools = module.build_tools(
        "ci-session",
        skill_resource_uri=lambda skill, resource: (
            f"s3://skill-bucket/skills/{skill}/{resource}"
        ),
    )

    result = asyncio.run(tools[2]("example", "assets/report template.xlsx"))

    assert result.startswith("Skill resource staged at /tmp/skill-resource-")
    assert result.endswith("-report_template.xlsx")
    assert invocations[0][0:2] == ("ci-session", "executeCommand")
    command = invocations[0][2]["command"]
    assert command.startswith("aws s3 cp --only-show-errors ")
    assert "s3://skill-bucket/skills/example/assets/report template.xlsx" in command
    assert "/tmp/skill-resource-" in command


def test_stage_skill_resource_reports_transfer_failure(monkeypatch):
    module = _load_code_interpreter(monkeypatch)

    async def fake_invoke(*_args, **_kwargs):
        return json.dumps([{"isError": True, "content": [{"text": "denied"}]}])

    monkeypatch.setattr(module, "_invoke_tool", fake_invoke)
    tools = module.build_tools(
        "ci-session",
        skill_resource_uri=lambda *_args: "s3://skill-bucket/skills/example/file.bin",
    )

    result = asyncio.run(tools[2]("example", "assets/file.bin"))

    assert result.startswith("Unable to stage skill resource")

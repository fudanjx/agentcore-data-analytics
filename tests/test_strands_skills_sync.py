import importlib.util
import sys
import types
from pathlib import Path


def _load_skills_sync(monkeypatch):
    strands = types.ModuleType("strands")

    def fake_tool(**_kwargs):
        return lambda function: function

    strands.tool = fake_tool
    monkeypatch.setitem(sys.modules, "strands", strands)
    module_path = Path(__file__).parents[1] / "Strands-runtime" / "skills_sync.py"
    spec = importlib.util.spec_from_file_location(
        "strands_runtime_skills_sync", module_path
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_reads_bounded_utf8_text_inside_skill_root(tmp_path, monkeypatch):
    module = _load_skills_sync(monkeypatch)
    skill_root = tmp_path / "skills"
    reference = skill_root / "example" / "references" / "metric.md"
    reference.parent.mkdir(parents=True)
    reference.write_text("123456789", encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    script = skill_root / "example" / "scripts" / "run.py"
    script.parent.mkdir(parents=True)
    script.write_text("print('yes')", encoding="utf-8")
    monkeypatch.setattr(module, "LOCAL_DIR", skill_root)
    monkeypatch.setattr(module, "MAX_RESOURCE_CHARS", 5)

    result = module.read_skill_resource("example", "references/metric.md")

    assert result == "12345\n\n[skill resource truncated]"
    assert "must be relative" in module.read_skill_resource("example", str(outside))
    assert module.read_skill_resource("example", "scripts/run.py").startswith("print")
    assert "must stay inside" in module.read_skill_resource("example", "../outside.md")


def test_sync_downloads_complete_skill_packages(tmp_path, monkeypatch):
    module = _load_skills_sync(monkeypatch)
    objects = {
        "skills/example/SKILL.md": b"---\nname: example\ndescription: Test\n---\n",
        "skills/example/references/schema.json": b'{"field":"value"}',
        "skills/example/scripts/process.py": b"print('ok')",
        "skills/example/assets/template.bin": b"\x00\x01\x02",
        "skills/example/assets/oversized.bin": b"x" * 101,
        "skills/../escape.txt": b"unsafe",
    }

    class FakeS3:
        def list_objects_v2(self, **request):
            assert request["Bucket"] == "bucket"
            if "ContinuationToken" not in request:
                keys = list(objects)[:3]
                return {
                    "Contents": [
                        {"Key": key, "Size": len(objects[key])} for key in keys
                    ],
                    "IsTruncated": True,
                    "NextContinuationToken": "page-2",
                }
            assert request["ContinuationToken"] == "page-2"
            keys = list(objects)[3:]
            return {
                "Contents": [{"Key": key, "Size": len(objects[key])} for key in keys],
                "IsTruncated": False,
            }

        def download_file(self, _bucket, key, destination):
            Path(destination).write_bytes(objects[key])

    skill_root = tmp_path / "skills"
    monkeypatch.setattr(module, "BUCKET", "bucket")
    monkeypatch.setattr(module, "PREFIX", "skills/")
    monkeypatch.setattr(module, "LOCAL_DIR", skill_root)
    monkeypatch.setattr(module, "MAX_OBJECT_BYTES", 100)
    monkeypatch.setattr(module, "MAX_SYNC_BYTES", 10_000)
    monkeypatch.setattr(module.boto3, "client", lambda *_args, **_kwargs: FakeS3())

    downloaded = module.sync_skills()

    assert len(downloaded) == 4
    assert (skill_root / "example" / "SKILL.md").is_file()
    assert (skill_root / "example" / "references" / "schema.json").is_file()
    assert (skill_root / "example" / "scripts" / "process.py").is_file()
    assert (
        skill_root / "example" / "assets" / "template.bin"
    ).read_bytes() == b"\x00\x01\x02"
    assert not (skill_root / "example" / "assets" / "oversized.bin").exists()
    assert not (tmp_path / "escape.txt").exists()


def test_prompt_context_bulk_injection_was_removed(monkeypatch):
    module = _load_skills_sync(monkeypatch)

    assert not hasattr(module, "prompt_context")

"""Tests for versioned Agent Skill ZIPs at Runtime startup."""

from __future__ import annotations

import asyncio
import io
import sys
import types
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


RUNTIME_DIR = Path(__file__).resolve().parents[1]
if str(RUNTIME_DIR) not in sys.path:
    sys.path.insert(0, str(RUNTIME_DIR))

fake_strands = types.ModuleType("strands")
fake_strands.tool = lambda **_kwargs: lambda function: function
sys.modules.setdefault("strands", fake_strands)

import code_interpreter  # noqa: E402
import skills_sync  # noqa: E402


SKILL = b"---\nname: old-name\ndescription: Apply the selected skill.\n---\n# Skill\n"


def skill_zip(folder: str = "", reference: bytes = b"new", extra: dict[str, bytes] | None = None) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        prefix = f"{folder}/" if folder else ""
        archive.writestr(f"{prefix}SKILL.md", SKILL)
        archive.writestr(f"{prefix}references/data.md", reference)
        for path, content in (extra or {}).items():
            archive.writestr(path, content)
    return output.getvalue()


class FakeS3:
    def __init__(self, objects: dict[str, bytes], modified: dict[str, datetime] | None = None):
        self.objects = objects
        self.modified = modified or {}
        self.downloaded: list[str] = []

    def list_objects_v2(self, Bucket: str, Prefix: str, ContinuationToken: str | None = None):
        keys = sorted(key for key in self.objects if key.startswith(Prefix))
        start = int(ContinuationToken or "0")
        page = keys[start:start + 2]
        return {
            "Contents": [{"Key": key, "Size": len(self.objects[key]), "LastModified": self.modified.get(key)} for key in page],
            "IsTruncated": start + 2 < len(keys),
            "NextContinuationToken": str(start + 2) if start + 2 < len(keys) else None,
        }

    def download_file(self, Bucket: str, Key: str, Filename: str):
        self.downloaded.append(Key)
        Path(Filename).write_bytes(self.objects[Key])


class SkillsSyncTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.local_dir = Path(self.temporary.name) / "skills"
        self.patches = [
            patch.object(skills_sync, "BUCKET", "test-skill-bucket"),
            patch.object(skills_sync, "PREFIX", "skills/"),
            patch.object(skills_sync, "LOCAL_DIR", self.local_dir),
        ]
        for active in self.patches:
            active.start()
            self.addCleanup(active.stop)
        skills_sync.ZIP_RESOURCE_LOCATIONS.clear()

    def test_newest_zip_replaces_loose_files_and_preserves_other_skills(self):
        old_key = "skills/domain-specialist/20260914T010000000Z-aaaaaaaa.zip"
        new_key = "skills/domain-specialist/20260915T010000000Z-bbbbbbbb.zip"
        objects = {
            old_key: skill_zip(reference=b"old"),
            new_key: skill_zip(folder="exported-skill", reference=b"new"),
            "skills/domain-specialist/SKILL.md": SKILL,
            "skills/domain-specialist/references/stale.md": b"stale",
            "skills/other-skill/SKILL.md": SKILL,
            "skills/other-skill/assets/archive.zip": b"nested ZIP asset",
        }
        self.local_dir.mkdir()
        stale = self.local_dir / "domain-specialist" / "previous-version.md"
        stale.parent.mkdir()
        stale.write_text("old local file")
        client = FakeS3(objects)
        with patch.object(skills_sync.boto3, "client", return_value=client):
            loaded = skills_sync.sync_skills()
        self.assertIn(str(self.local_dir / "domain-specialist" / "SKILL.md"), loaded)
        self.assertIn(b"name: domain-specialist", (self.local_dir / "domain-specialist" / "SKILL.md").read_bytes())
        self.assertEqual((self.local_dir / "domain-specialist" / "references" / "data.md").read_bytes(), b"new")
        self.assertFalse(stale.exists())
        self.assertFalse((self.local_dir / "domain-specialist" / "references" / "stale.md").exists())
        self.assertEqual((self.local_dir / "other-skill" / "assets" / "archive.zip").read_bytes(), b"nested ZIP asset")
        self.assertEqual(client.downloaded.count(new_key), 1)
        self.assertNotIn(old_key, client.downloaded)
        self.assertEqual(skills_sync.read_skill_resource("domain-specialist", "references/data.md"), "new")
        self.assertEqual(
            skills_sync.skill_resource_s3_location("domain-specialist", "references/data.md"),
            (f"s3://test-skill-bucket/{new_key}", "exported-skill/references/data.md"),
        )
        self.assertEqual(
            skills_sync.skill_resource_s3_location("other-skill", "SKILL.md"),
            ("s3://test-skill-bucket/skills/other-skill/SKILL.md", None),
        )

    def test_invalid_newest_zip_falls_back_to_previous_snapshot(self):
        old_key = "skills/domain-specialist/20260914T010000000Z-aaaaaaaa.zip"
        new_key = "skills/domain-specialist/20260915T010000000Z-bbbbbbbb.zip"
        client = FakeS3({old_key: skill_zip(reference=b"previous"), new_key: skill_zip(extra={"../outside.md": b"bad"})})
        with patch.object(skills_sync.boto3, "client", return_value=client):
            skills_sync.sync_skills()
        self.assertEqual((self.local_dir / "domain-specialist" / "references" / "data.md").read_bytes(), b"previous")
        self.assertEqual(client.downloaded[:2], [new_key, old_key])
        self.assertFalse((self.local_dir.parent / "outside.md").exists())

    def test_legacy_zip_names_use_s3_modified_time(self):
        older = "skills/domain-specialist/old.zip"
        newer = "skills/domain-specialist/new.zip"
        modified = {older: datetime(2026, 9, 14, tzinfo=timezone.utc), newer: datetime(2026, 9, 15, tzinfo=timezone.utc)}
        client = FakeS3({older: skill_zip(reference=b"old"), newer: skill_zip(reference=b"new")}, modified)
        with patch.object(skills_sync.boto3, "client", return_value=client):
            skills_sync.sync_skills()
        self.assertEqual(client.downloaded, [newer])

    def test_s3_object_time_takes_priority_over_filename_timestamp(self):
        newer_name = "skills/domain-specialist/20260915T010000000Z-bbbbbbbb.zip"
        older_name = "skills/domain-specialist/20260914T010000000Z-aaaaaaaa.zip"
        modified = {
            newer_name: datetime(2026, 9, 14, tzinfo=timezone.utc),
            older_name: datetime(2026, 9, 15, tzinfo=timezone.utc),
        }
        client = FakeS3({newer_name: skill_zip(reference=b"older upload"), older_name: skill_zip(reference=b"latest upload")}, modified)
        with patch.object(skills_sync.boto3, "client", return_value=client):
            skills_sync.sync_skills()
        self.assertEqual(client.downloaded, [older_name])
        self.assertEqual((self.local_dir / "domain-specialist" / "references" / "data.md").read_bytes(), b"latest upload")

    def test_loose_files_load_when_no_snapshot_is_valid(self):
        broken = "skills/domain-specialist/20260915T010000000Z-bbbbbbbb.zip"
        loose = "skills/domain-specialist/SKILL.md"
        client = FakeS3({broken: b"not a ZIP", loose: SKILL})
        with patch.object(skills_sync.boto3, "client", return_value=client):
            skills_sync.sync_skills()
        self.assertEqual((self.local_dir / "domain-specialist" / "SKILL.md").read_bytes(), SKILL)
        self.assertEqual(skills_sync.skill_resource_s3_uri("domain-specialist", "SKILL.md"), f"s3://test-skill-bucket/{loose}")
        self.assertNotIn("domain-specialist", skills_sync.ZIP_RESOURCE_LOCATIONS)

    def test_zip_resource_staging_downloads_snapshot_then_extracts_member(self):
        calls = []

        async def invoke(_session_id, name, arguments):
            calls.append((name, arguments))
            return "ok"

        with patch.object(code_interpreter, "_invoke_tool", side_effect=invoke), patch.object(code_interpreter, "_tool_result_is_error", return_value=False):
            tools = code_interpreter.build_tools("session", lambda _skill, _path: ("s3://bucket/skills/skill/version.zip", "wrapper/references/data.md"))
            staged = next(item for item in tools if item.__name__ == "stage_skill_resource")
            result = asyncio.run(staged("skill", "references/data.md"))
        self.assertIn("Skill resource staged", result)
        self.assertEqual([name for name, _ in calls], ["executeCommand", "executeCode"])
        self.assertIn("version.zip", calls[0][1]["command"])
        self.assertIn("wrapper/references/data.md", calls[1][1]["code"])

        archive_path = Path(self.temporary.name) / "member.zip"
        destination = Path(self.temporary.name) / "extracted.md"
        archive_path.write_bytes(skill_zip(folder="wrapper", reference=b"staged"))
        exec(code_interpreter._zip_member_extract_code(str(archive_path), "wrapper/references/data.md", str(destination)))
        self.assertEqual(destination.read_bytes(), b"staged")
        self.assertFalse(archive_path.exists())

        calls.clear()
        with patch.object(code_interpreter, "_invoke_tool", side_effect=invoke), patch.object(code_interpreter, "_tool_result_is_error", return_value=False):
            tools = code_interpreter.build_tools("session", lambda _skill, _path: "s3://bucket/skills/skill/references/data.md")
            staged = next(item for item in tools if item.__name__ == "stage_skill_resource")
            asyncio.run(staged("skill", "references/data.md"))
        self.assertEqual([name for name, _ in calls], ["executeCommand"])


if __name__ == "__main__":
    unittest.main()

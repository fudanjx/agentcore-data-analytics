import unittest
from io import BytesIO
from zipfile import ZipFile
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from s3tables_delta_pilot import skill_bundle


TABLE_BUCKET_ARN = "arn:aws:s3tables:ap-southeast-1:123456789012:bucket/ah-soc-delta-pilot"
SKILL = b"---\nname: old-name\ndescription: Analyse the selected tables.\n---\n# Skill\n"


class SkillBundleTests(unittest.TestCase):
    @staticmethod
    def skill_zip(**members):
        output = BytesIO()
        with ZipFile(output, "w") as archive:
            for path, content in members.items():
                archive.writestr(path, content)
        return output.getvalue()

    def test_zip_snapshot_requires_complete_safe_skill(self):
        valid = self.skill_zip(**{"SKILL.md": SKILL, "references/data.md": b"facts"})
        skill_bundle.validate_version_zip(TABLE_BUCKET_ARN, "skill.zip", valid)
        wrapped = self.skill_zip(**{"my-skill/SKILL.md": SKILL, "my-skill/references/data.md": b"facts"})
        skill_bundle.validate_version_zip(TABLE_BUCKET_ARN, "wrapped.zip", wrapped)
        for filename, content in [
            ("skill.md", valid), ("skill.zip", b"not a zip"),
            ("skill.zip", self.skill_zip(**{"references/data.md": b"facts"})),
            ("skill.zip", self.skill_zip(**{"SKILL.md": SKILL, "../outside": b"bad"})),
            ("skill.zip", self.skill_zip(**{"my-skill/SKILL.md": SKILL, "other/data.md": b"bad"})),
            ("skill.zip", self.skill_zip(**{"my-skill/SKILL.md": SKILL, "other/SKILL.md": SKILL})),
        ]:
            with self.subTest(filename=filename, content=content[:10]), self.assertRaises(skill_bundle.SkillBundleError):
                skill_bundle.validate_version_zip(TABLE_BUCKET_ARN, filename, content)

    def test_upload_creates_timestamped_immutable_zip_object(self):
        content = self.skill_zip(**{"SKILL.md": SKILL})
        with patch.object(skill_bundle.s3, "put_object") as put:
            result = skill_bundle.publish_version(TABLE_BUCKET_ARN, "local-editor", "my-skill.zip", content)
        key = put.call_args.kwargs["Key"]
        filename = key.rsplit("/", 1)[-1]
        self.assertRegex(filename, skill_bundle._VERSION_RE)
        self.assertEqual(filename, result["filename"])
        self.assertEqual(content, put.call_args.kwargs["Body"])
        self.assertEqual("*", put.call_args.kwargs["IfNoneMatch"])

    def test_zip_listing_includes_existing_versions_and_uses_filename_timestamp(self):
        paginator = Mock()
        paginator.paginate.return_value = [{"Contents": [
            {"Key": "skills/ah-soc-delta-pilot/20260915T101112123Z-a1b2c3d4.zip", "Size": 100},
            {"Key": "skills/ah-soc-delta-pilot/older.zip", "Size": 50, "LastModified": datetime(2026, 9, 14, tzinfo=timezone.utc)},
            {"Key": "skills/ah-soc-delta-pilot/20260913112233.zip", "Size": 40},
            {"Key": "skills/ah-soc-delta-pilot/SKILL.md", "Size": 10},
            {"Key": "skills/ah-soc-delta-pilot/nested/hidden.zip", "Size": 10},
        ]}]
        with patch.object(skill_bundle.s3, "get_paginator", return_value=paginator):
            versions = skill_bundle.list_skill_versions(TABLE_BUCKET_ARN)["versions"]
        self.assertEqual(["20260915T101112123Z-a1b2c3d4.zip", "older.zip", "20260913112233.zip"], [item["filename"] for item in versions])
        self.assertEqual("2026-09-15T10:11:12.123000+00:00", versions[0]["uploaded_at"])
        self.assertEqual("2026-09-13T11:22:33+00:00", versions[2]["uploaded_at"])

    def test_validation_requires_root_skill_and_normalises_name(self):
        name, files = skill_bundle.validate_bundle(TABLE_BUCKET_ARN, [("SKILL.md", SKILL), ("references/data.md", b"facts")])
        self.assertEqual("ah-soc-delta-pilot", name)
        self.assertIn(b"name: ah-soc-delta-pilot", files[0].content)
        with self.assertRaises(skill_bundle.SkillBundleError):
            skill_bundle.validate_bundle(TABLE_BUCKET_ARN, [("references/data.md", b"facts")])
        with self.assertRaises(skill_bundle.SkillBundleError):
            skill_bundle.validate_bundle(TABLE_BUCKET_ARN, [("../SKILL.md", SKILL)])

    def test_incremental_publish_uploads_resources_before_skill_and_keeps_stale_objects(self):
        paginator = Mock()
        paginator.paginate.return_value = [{"Contents": [{"Key": "skills/ah-soc-delta-pilot/old.txt"}]}]
        with patch.object(skill_bundle.s3, "get_paginator", return_value=paginator), patch.object(skill_bundle.s3, "put_object") as put, patch.object(skill_bundle.s3, "delete_object") as delete:
            result = skill_bundle.publish_files(TABLE_BUCKET_ARN, "local-editor", [("SKILL.md", SKILL), ("references/data.md", b"facts")])
        self.assertEqual("references/data.md", put.call_args_list[0].kwargs["Key"].removeprefix("skills/ah-soc-delta-pilot/"))
        self.assertEqual("SKILL.md", put.call_args_list[1].kwargs["Key"].removeprefix("skills/ah-soc-delta-pilot/"))
        delete.assert_not_called()
        self.assertEqual(["references/data.md", "SKILL.md"], result["created_paths"])
        self.assertEqual([], result["overwritten_paths"])

    def test_incremental_publish_reports_overwrites(self):
        paginator = Mock()
        paginator.paginate.return_value = [{"Contents": [{"Key": "skills/ah-soc-delta-pilot/references/data.md"}]}]
        with patch.object(skill_bundle.s3, "get_paginator", return_value=paginator), patch.object(skill_bundle.s3, "put_object"):
            result = skill_bundle.publish_files(TABLE_BUCKET_ARN, "local-editor", [("references/data.md", b"new facts")])
        self.assertEqual([], result["created_paths"])
        self.assertEqual(["references/data.md"], result["overwritten_paths"])

    def test_list_skill_files_exposes_safe_relative_metadata_only(self):
        paginator = Mock()
        paginator.paginate.return_value = [{"Contents": [
            {"Key": "skills/ah-soc-delta-pilot/SKILL.md", "Size": 123, "LastModified": datetime(2026, 9, 5, tzinfo=timezone.utc)},
            {"Key": "skills/ah-soc-delta-pilot/references/", "Size": 0},
            {"Key": "skills/ah-soc-delta-pilot/../outside.txt", "Size": 1},
        ]}]
        with patch.object(skill_bundle.s3, "get_paginator", return_value=paginator):
            result = skill_bundle.list_skill_files(TABLE_BUCKET_ARN)
        self.assertEqual("s3://agentcore-harness-dev/skills/ah-soc-delta-pilot/", result["destination_uri"])
        self.assertEqual([{"path": "SKILL.md", "size": 123, "last_modified": "2026-09-05T00:00:00+00:00"}], result["files"])

    def test_skill_file_location_cannot_escape_the_bucket_prefix(self):
        bucket, key, path = skill_bundle.skill_file_location(TABLE_BUCKET_ARN, "scripts/map.py")
        self.assertEqual("agentcore-harness-dev", bucket)
        self.assertEqual("skills/ah-soc-delta-pilot/scripts/map.py", key)
        self.assertEqual("scripts/map.py", path)
        with self.assertRaises(skill_bundle.SkillBundleError):
            skill_bundle.skill_file_location(TABLE_BUCKET_ARN, "../outside.txt")


if __name__ == "__main__":
    unittest.main()

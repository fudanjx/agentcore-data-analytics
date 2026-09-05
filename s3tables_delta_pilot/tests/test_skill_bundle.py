import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from s3tables_delta_pilot import skill_bundle


TABLE_BUCKET_ARN = "arn:aws:s3tables:ap-southeast-1:123456789012:bucket/ah-soc-delta-pilot"
SKILL = b"---\nname: old-name\ndescription: Analyse the selected tables.\n---\n# Skill\n"


class SkillBundleTests(unittest.TestCase):
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

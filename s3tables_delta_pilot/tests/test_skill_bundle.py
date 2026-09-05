import unittest
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

    def test_publish_uploads_resources_before_skill_and_deletes_stale_objects(self):
        paginator = Mock()
        paginator.paginate.return_value = [{"Contents": [{"Key": "skills/ah-soc-delta-pilot/old.txt"}]}]
        with patch.object(skill_bundle.s3, "get_paginator", return_value=paginator), patch.object(skill_bundle.s3, "put_object") as put, patch.object(skill_bundle.s3, "delete_object") as delete:
            result = skill_bundle.publish_bundle(TABLE_BUCKET_ARN, "local-editor", [("SKILL.md", SKILL), ("references/data.md", b"facts")])
        self.assertEqual("references/data.md", put.call_args_list[0].kwargs["Key"].removeprefix("skills/ah-soc-delta-pilot/"))
        self.assertEqual("SKILL.md", put.call_args_list[1].kwargs["Key"].removeprefix("skills/ah-soc-delta-pilot/"))
        delete.assert_called_once_with(Bucket="agentcore-harness-dev", Key="skills/ah-soc-delta-pilot/old.txt")
        self.assertEqual(["old.txt"], result["deleted_paths"])


if __name__ == "__main__":
    unittest.main()

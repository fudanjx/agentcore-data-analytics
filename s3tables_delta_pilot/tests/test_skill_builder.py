import unittest
from io import BytesIO
from unittest.mock import Mock, patch

from s3tables_delta_pilot import skill_builder


TABLE_BUCKET_ARN = (
    "arn:aws:s3tables:ap-southeast-1:123456789012:bucket/ah-soc-delta-pilot"
)
GENERATED_SKILL = """---
name: generated-name
description: Analyse the selected S3 Tables data.
---

# Instructions

Use the connected tools.
"""


class SkillBuilderTests(unittest.TestCase):
    def test_dify_uses_blocking_mode_and_unchanged_instruction(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "answer": "s3://ah-dify/harness_dev/user/run/SKILL.md"
        }
        with patch.dict(
            "os.environ",
            {
                "SKILL_BUILD_DIFY_API_KEY": "secret-value",
                "SKILL_BUILD_DIFY_URL": "https://dify-eks.bot-alex.com/v1/chat-messages",
            },
            clear=False,
        ), patch("s3tables_delta_pilot.skill_builder.httpx.post", return_value=response) as post:
            answer = skill_builder._call_dify("Exact user instruction", "local-editor")

        self.assertEqual("s3://ah-dify/harness_dev/user/run/SKILL.md", answer)
        request = post.call_args.kwargs
        self.assertEqual("Bearer secret-value", request["headers"]["Authorization"])
        self.assertEqual("Exact user instruction", request["json"]["query"])
        self.assertEqual("blocking", request["json"]["response_mode"])
        self.assertEqual("", request["json"]["conversation_id"])
        self.assertEqual([], request["json"]["files"])

    def test_generated_skill_name_is_replaced_with_table_bucket_name(self):
        normalized = skill_builder.normalize_skill_name(
            GENERATED_SKILL, "ah-soc-delta-pilot"
        )
        self.assertIn("\nname: ah-soc-delta-pilot\n", normalized)
        self.assertNotIn("generated-name", normalized)
        self.assertIn("# Instructions", normalized)

    def test_missing_name_is_inserted_but_description_is_required(self):
        normalized = skill_builder.normalize_skill_name(
            "---\ndescription: Valid skill\n---\nBody\n", "example-bucket"
        )
        self.assertTrue(normalized.startswith("---\nname: example-bucket\n"))
        with self.assertRaises(skill_builder.SkillBuildError):
            skill_builder.normalize_skill_name(
                "---\nname: old-name\n---\nBody\n", "example-bucket"
            )

    def test_build_draft_downloads_returned_uri_and_calculates_destination(self):
        uri = "s3://ah-dify/harness_dev/user/run/SKILL.md"
        with patch(
            "s3tables_delta_pilot.skill_builder._call_dify",
            return_value=f"Completed: {uri}",
        ) as call_dify, patch(
            "s3tables_delta_pilot.skill_builder._download_skill",
            return_value=GENERATED_SKILL,
        ) as download, patch.dict("os.environ", {}, clear=True):
            result = skill_builder.build_skill_draft(
                "Build the requested skill", "local-editor", TABLE_BUCKET_ARN
            )

        call_dify.assert_called_once_with("Build the requested skill", "local-editor")
        download.assert_called_once_with(uri)
        self.assertEqual("ah-soc-delta-pilot", result["skill_name"])
        self.assertEqual(
            "s3://agentcore-harness-dev/skills/ah-soc-delta-pilot/SKILL.md",
            result["destination_uri"],
        )
        self.assertIn("name: ah-soc-delta-pilot", result["content"])

    def test_uri_extraction_requires_one_skill_document(self):
        self.assertEqual(
            "s3://ah-dify/path/SKILL.md",
            skill_builder.extract_skill_uri("Ready: `s3://ah-dify/path/SKILL.md`."),
        )
        with self.assertRaises(skill_builder.SkillBuildError):
            skill_builder.extract_skill_uri("No object was generated")
        with self.assertRaises(skill_builder.SkillBuildError):
            skill_builder.extract_skill_uri(
                "s3://one/path/SKILL.md s3://two/path/SKILL.md"
            )

    def test_download_is_bounded_and_decodes_utf8(self):
        body = BytesIO(GENERATED_SKILL.encode("utf-8"))
        with patch.object(
            skill_builder.s3, "get_object", return_value={"Body": body}
        ) as get_object, patch.dict("os.environ", {}, clear=True):
            content = skill_builder._download_skill(
                "s3://ah-dify/harness_dev/user/run/SKILL.md"
            )
        get_object.assert_called_once_with(
            Bucket="ah-dify", Key="harness_dev/user/run/SKILL.md"
        )
        self.assertEqual(GENERATED_SKILL, content)

    def test_publish_reenforces_name_and_uses_bucket_scoped_destination(self):
        edited = GENERATED_SKILL.replace("Use the connected tools.", "Use reviewed rules.")
        with patch.object(
            skill_builder.s3,
            "put_object",
            return_value={"ETag": '"abc123"', "VersionId": "version-1"},
        ) as put_object, patch.dict("os.environ", {}, clear=True):
            result = skill_builder.publish_skill(
                edited, "local-editor", TABLE_BUCKET_ARN
            )

        request = put_object.call_args.kwargs
        self.assertEqual("agentcore-harness-dev", request["Bucket"])
        self.assertEqual("skills/ah-soc-delta-pilot/SKILL.md", request["Key"])
        self.assertIn(b"name: ah-soc-delta-pilot", request["Body"])
        self.assertIn(b"Use reviewed rules.", request["Body"])
        self.assertEqual("AES256", request["ServerSideEncryption"])
        self.assertEqual("abc123", result["etag"])
        self.assertEqual("version-1", result["version_id"])


if __name__ == "__main__":
    unittest.main()

"""Tests for request-scoped Bedrock image analysis."""

from __future__ import annotations

import asyncio
import importlib
import io
import json
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


RUNTIME_DIR = Path(__file__).resolve().parents[1]
if str(RUNTIME_DIR) not in sys.path:
    sys.path.insert(0, str(RUNTIME_DIR))


def _tool_decorator(**_kwargs):
    def decorate(function):
        return function

    return decorate


fake_strands = types.ModuleType("strands")
fake_strands.tool = _tool_decorator
sys.modules.setdefault("strands", fake_strands)

import image_analysis  # noqa: E402


class _S3Client:
    def __init__(self, raw: bytes) -> None:
        self.raw = raw

    def get_object(self, **_kwargs):
        return {"Body": io.BytesIO(self.raw), "ContentLength": len(self.raw)}


class _BedrockClient:
    def __init__(self) -> None:
        self.request = None

    def converse(self, **kwargs):
        self.request = kwargs
        return {
            "output": {
                "message": {"content": [{"text": "A visible dashboard chart."}]}
            }
        }


class ImageAnalysisTests(unittest.TestCase):
    def setUp(self) -> None:
        with patch.dict(os.environ, {"VISION_MODEL_ID": "vision-model"}):
            self.module = importlib.reload(image_analysis)

    def test_tool_downloads_request_bound_image_and_calls_vision(self) -> None:
        raw = b"\xff\xd8\xff" + b"image"
        bedrock = _BedrockClient()
        self.module.get_s3_client = lambda: _S3Client(raw)
        self.module.get_bedrock_client = lambda: bedrock
        tools = self.module.build_tools(
            [
                {
                    "stored_name": "clara-image.jpg",
                    "source_url": "s3://private/image.jpg",
                }
            ]
        )

        rendered = asyncio.run(
            tools[0]("clara-image.jpg", "Read the dashboard")
        )
        payload = json.loads(rendered)

        self.assertTrue(payload["ok"])
        self.assertFalse(payload["encrypted_input"])
        self.assertIn("dashboard", payload["analysis"])
        self.assertEqual(bedrock.request["modelId"], "vision-model")
        self.assertEqual(
            bedrock.request["messages"][0]["content"][0]["image"]["format"],
            "jpeg",
        )

    def test_tool_decrypts_matching_image_before_vision(self) -> None:
        encrypted = b"encrypted"
        plaintext = b"\x89PNG\r\n\x1a\n" + b"image"
        bedrock = _BedrockClient()
        self.module.get_s3_client = lambda: _S3Client(encrypted)
        self.module.get_bedrock_client = lambda: bedrock
        tools = self.module.build_tools(
            [
                {
                    "stored_name": "clara-image.png.claraimage",
                    "source_url": "s3://private/image.png.claraimage",
                }
            ],
            encrypted_documents=[
                {
                    "stored_name": "clara-image.png.claraimage",
                    "original_name": "image.png",
                    "wrapped_key": "wrapped",
                }
            ],
        )

        with (
            patch.object(
                self.module.document_encryption,
                "unwrap_key",
                return_value=b"k" * 32,
            ),
            patch.object(
                self.module.document_encryption,
                "decrypt_envelope",
                return_value=plaintext,
            ),
        ):
            payload = json.loads(
                asyncio.run(tools[0]("clara-image.png.claraimage"))
            )

        self.assertTrue(payload["ok"])
        self.assertTrue(payload["encrypted_input"])
        supplied = bedrock.request["messages"][0]["content"][0]["image"]
        self.assertEqual(supplied["source"]["bytes"], plaintext)

    def test_tool_rejects_an_unlisted_filename(self) -> None:
        tools = self.module.build_tools(
            [
                {
                    "stored_name": "allowed.jpg",
                    "source_url": "s3://private/allowed.jpg",
                }
            ]
        )
        payload = json.loads(asyncio.run(tools[0]("other.jpg")))
        self.assertFalse(payload["ok"])

    def test_claraimage_requires_matching_encryption_metadata(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "missing matching"):
            self.module.build_tools(
                [
                    {
                        "stored_name": "clara-image.png.claraimage",
                        "source_url": "s3://private/image.png.claraimage",
                    }
                ]
            )

    def test_vision_model_falls_back_to_primary_model_id(self) -> None:
        with patch.dict(
            os.environ,
            {
                "VISION_MODEL_ID": "",
                "MODEL_ARN": "",
                "MODEL_ID": "global.openai.gpt-5.6-luna",
            },
        ):
            module = importlib.reload(image_analysis)

        self.assertEqual(module.VISION_MODEL_ID, "global.openai.gpt-5.6-luna")


if __name__ == "__main__":
    unittest.main()

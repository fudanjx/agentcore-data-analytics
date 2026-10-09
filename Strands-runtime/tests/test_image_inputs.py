"""Tests for request-scoped image input tags."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


RUNTIME_DIR = Path(__file__).resolve().parents[1]
if str(RUNTIME_DIR) not in sys.path:
    sys.path.insert(0, str(RUNTIME_DIR))

import image_inputs  # noqa: E402


class ImageInputTests(unittest.TestCase):
    def test_extracts_uppercase_image_tag(self) -> None:
        images = image_inputs.extract_from_messages(
            [
                {
                    "role": "system",
                    "content": (
                        "<IMAGE_INPUT>\n"
                        "Name: clara-example.jpg, "
                        "S3_URI: s3://private-bucket/path/example.jpg\n"
                        "</IMAGE_INPUT>"
                    ),
                }
            ]
        )

        self.assertEqual(
            images,
            [
                {
                    "stored_name": "clara-example.jpg",
                    "source_url": "s3://private-bucket/path/example.jpg",
                }
            ],
        )

    def test_rejects_duplicate_names(self) -> None:
        tag = (
            "<IMAGE_INPUT>Name: same.jpg, "
            "S3_URI: s3://private-bucket/path/same.jpg</IMAGE_INPUT>"
        )
        with self.assertRaisesRegex(ValueError, "unique"):
            image_inputs.extract_from_messages(
                [{"role": "system", "content": tag + tag}]
            )

    def test_extracts_multiple_images_from_one_tag(self) -> None:
        content = """<IMAGE_INPUT>
Name: clara-first.png.claraimage, S3_URI: s3://private/first.claraimage
Name: clara-second.png.claraimage, S3_URI: s3://private/second.claraimage
Name: clara-third.png.claraimage, S3_URI: s3://private/third.claraimage
</IMAGE_INPUT>"""

        images = image_inputs.extract_from_messages(
            [{"role": "system", "content": content}]
        )

        self.assertEqual(len(images), 3)
        self.assertEqual(
            [image["stored_name"] for image in images],
            [
                "clara-first.png.claraimage",
                "clara-second.png.claraimage",
                "clara-third.png.claraimage",
            ],
        )


if __name__ == "__main__":
    unittest.main()

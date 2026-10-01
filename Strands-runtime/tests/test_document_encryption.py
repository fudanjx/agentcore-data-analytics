"""Tests for CLARA encrypted-document request metadata."""

from __future__ import annotations

import base64
import json
import sys
import unittest
from pathlib import Path


RUNTIME_DIR = Path(__file__).resolve().parents[1]
if str(RUNTIME_DIR) not in sys.path:
    sys.path.insert(0, str(RUNTIME_DIR))

import document_encryption  # noqa: E402


def _encoded_context(files: list[dict]) -> str:
    raw = json.dumps({"version": 1, "files": files}).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _metadata(stored_name: str = "clara-123-contract.pdf") -> dict:
    return {
        "content_encryption": "AES-256-GCM",
        "key_wrap": "RSA-OAEP-256",
        "envelope": "CLARAENC1",
        "stored_name": stored_name,
        "original_name": "contract.pdf",
        "wrapped_key": "not-a-plaintext-key",
        "key_id": "test-key",
    }


class DocumentEncryptionContextTests(unittest.TestCase):
    def test_extracts_context_and_removes_it_before_model_input(self) -> None:
        encoded = _encoded_context([_metadata()])
        messages = [
            {
                "role": "system",
                "content": (
                    "instructions\n<encrypted_document_context>"
                    + encoded
                    + "</encrypted_document_context>"
                ),
            },
            {"role": "user", "content": "Review the contract"},
        ]

        cleaned, metadata = document_encryption.extract_from_messages(messages)

        self.assertEqual(metadata[0]["original_name"], "contract.pdf")
        self.assertNotIn("wrapped_key", cleaned[0]["content"])
        self.assertNotIn("encrypted_document_context", cleaned[0]["content"])
        self.assertEqual(cleaned[1]["content"], "Review the contract")

    def test_rejects_unsupported_algorithm(self) -> None:
        item = _metadata()
        item["content_encryption"] = "AES-256-CBC"

        with self.assertRaisesRegex(ValueError, "metadata is invalid"):
            document_encryption.decode_context(_encoded_context([item]))

    def test_removes_empty_optional_carrier(self) -> None:
        cleaned, metadata = document_encryption.extract_from_messages(
            [
                {
                    "role": "system",
                    "content": "before<encrypted_document_context> </encrypted_document_context>after",
                }
            ]
        )

        self.assertEqual(cleaned[0]["content"], "beforeafter")
        self.assertEqual(metadata, [])

    def test_rejects_duplicate_stored_names_across_carriers(self) -> None:
        encoded = _encoded_context([_metadata()])
        carrier = (
            "<encrypted_document_context>"
            + encoded
            + "</encrypted_document_context>"
        )

        with self.assertRaisesRegex(ValueError, "duplicate stored names"):
            document_encryption.extract_from_messages(
                [
                    {"role": "system", "content": carrier},
                    {"role": "user", "content": carrier + "question"},
                ]
            )


if __name__ == "__main__":
    unittest.main()

"""Parse request-scoped image references from caller prompt tags."""

from __future__ import annotations

import re
from urllib.parse import urlparse


_IMAGE_INPUT_BLOCK_RE = re.compile(
    r"<IMAGE_INPUT>(?P<body>.*?)</IMAGE_INPUT>",
    re.IGNORECASE | re.DOTALL,
)
_IMAGE_INPUT_ENTRY_RE = re.compile(
    r"Name:\s*(?P<stored_name>[^\r\n]+?),\s*"
    r"S3_URI:\s*(?P<source_url>s3://[^\s<]+)",
    re.IGNORECASE,
)


def extract_from_messages(messages: list[dict]) -> list[dict[str, str]]:
    """Return validated image references without modifying caller messages."""
    images: list[dict[str, str]] = []
    seen: set[str] = set()
    for message in messages:
        content = message.get("content")
        if not isinstance(content, str):
            continue
        for block in _IMAGE_INPUT_BLOCK_RE.finditer(content):
            entries = list(_IMAGE_INPUT_ENTRY_RE.finditer(block.group("body")))
            if not entries:
                raise ValueError("Image input metadata is invalid")
            for match in entries:
                stored_name = match.group("stored_name").strip()
                source_url = match.group("source_url").strip()
                parsed = urlparse(source_url)
                if (
                    not stored_name
                    or len(stored_name) > 512
                    or parsed.scheme != "s3"
                    or not parsed.netloc
                    or not parsed.path.lstrip("/")
                ):
                    raise ValueError("Image input metadata is invalid")
                if stored_name in seen:
                    raise ValueError("Image input stored names must be unique")
                seen.add(stored_name)
                images.append(
                    {"stored_name": stored_name, "source_url": source_url}
                )
                if len(images) > 20:
                    raise ValueError("A request may contain at most 20 image inputs")
    return images

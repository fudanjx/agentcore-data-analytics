"""Time helpers.

Timestamps written to durable records use ISO-8601 in UTC so ordering and
comparison are lexicographic and unambiguous.
"""

from __future__ import annotations

from datetime import datetime, timezone


def utc_now() -> datetime:
    """Return an aware UTC ``datetime`` for the current instant."""
    return datetime.now(timezone.utc)


def now_iso() -> str:
    """Return the current UTC instant formatted as ISO-8601."""
    return utc_now().isoformat()

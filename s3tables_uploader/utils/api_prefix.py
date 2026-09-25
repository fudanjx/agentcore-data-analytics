"""Router prefix helper.

Ported verbatim from the shared reference utility. Used by every router:

    router = APIRouter(prefix=get_api_prefix(Path(__file__), "api"))

For ``.../api/v3/skills.py`` with ``stop_folder_name="api"`` this returns
``/api/v3/skills`` — the folder path itself becomes the URL prefix.
"""

from __future__ import annotations

from pathlib import Path


def get_api_prefix(path: Path, stop_folder_name: str) -> str:
    """Return a URL prefix built from the folder path up to ``stop_folder_name``.

    Args:
        path: Pass ``Path(__file__)`` from the caller module.
        stop_folder_name: The parent folder to include (typically ``"api"``).

    Underscores in filenames are converted to hyphens so Python module names
    (``upload_sessions.py``) become idiomatic REST URLs
    (``/api/v3/upload-sessions``).

    Example:
        ``/a/b/upload_sessions.py`` with ``stop_folder_name="a"`` -> ``/a/b/upload-sessions``.
    """
    path_parts = path.parts
    index = path_parts.index(stop_folder_name)
    new_path = Path("/", *path_parts[index:])
    return new_path.with_suffix("").as_posix().replace("_", "-")

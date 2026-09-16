"""Sync complete Agent Skills from S3 and expose their local text resources."""

import logging
import os
import re
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import boto3
from strands import tool


logger = logging.getLogger(__name__)
BUCKET = os.environ.get("SKILLS_BUCKET", "").strip()
PREFIX = os.environ.get("SKILLS_PREFIX", "").strip()
if PREFIX and not PREFIX.endswith("/"):
    PREFIX += "/"
LOCAL_DIR = Path(os.environ.get("SKILLS_LOCAL_DIR", "/tmp/strands-agent-skills"))
MAX_RESOURCE_CHARS = max(
    1_000, int(os.environ.get("SKILLS_MAX_RESOURCE_CHARS", "100000"))
)
MAX_OBJECT_BYTES = max(
    1_000, int(os.environ.get("SKILLS_MAX_OBJECT_BYTES", "52428800"))
)
MAX_SYNC_BYTES = max(
    MAX_OBJECT_BYTES, int(os.environ.get("SKILLS_MAX_SYNC_BYTES", "262144000"))
)
MAX_SKILL_FILES = 500
_SNAPSHOT_RE = re.compile(r"^(?P<stamp>\d{8}T\d{9}Z)-[0-9a-f]{8}\.zip$")
_SKILL_NAME_RE = re.compile(r"(?m)^name[ \t]*:.*$")
_DESCRIPTION_RE = re.compile(r"(?m)^description[ \t]*:[ \t]*(.*)$")
ZIP_RESOURCE_LOCATIONS: dict[str, dict[str, tuple[str, str]]] = {}
ACTIVATION_GUIDANCE = """

---

## Agent Skills

- The available-skills list contains only skill names and descriptions. When a skill matches the request, activate it with the skills tool before using the related MCP Gateway or Code Interpreter tools.
- After activation, follow the complete skill instructions. Read every required UTF-8 text reference with read_skill_resource before constructing a query or analysis.
- Skills provide operational guidance; MCP Gateway and Code Interpreter remain the tools that retrieve data and perform work.
"""


def skills_enabled() -> bool:
    """Return whether an S3 skills bucket is configured."""
    return bool(BUCKET)


def _local_path_for_key(key: str) -> Path:
    """Map an S3 object key safely beneath the configured local skill root."""
    if not key.startswith(PREFIX):
        raise ValueError("object key is outside the configured skills prefix")
    relative = key.removeprefix(PREFIX)
    parts = relative.split("/")
    if not relative or any(
        part in {"", ".", ".."} or "\\" in part or ":" in part or "\x00" in part
        for part in parts
    ):
        raise ValueError("object key contains an unsafe path")
    root = LOCAL_DIR.resolve()
    local = root.joinpath(*parts).resolve()
    if root not in local.parents:
        raise ValueError("object key resolves outside the configured skills directory")
    return local


def _safe_member_parts(path: str) -> list[str]:
    if not path or path.startswith("/"):
        raise ValueError("ZIP member path is not relative")
    parts = path.split("/")
    if any(part in {"", ".", ".."} or "\\" in part or ":" in part or "\x00" in part for part in parts):
        raise ValueError("ZIP member path is unsafe")
    return parts


def _snapshot_time(filename: str, last_modified: object) -> float:
    """Use S3 object time first; parse the filename only if metadata is missing."""
    if isinstance(last_modified, datetime):
        return last_modified.replace(tzinfo=last_modified.tzinfo or timezone.utc).timestamp()
    match = _SNAPSHOT_RE.fullmatch(filename)
    if match:
        try:
            return datetime.strptime(match.group("stamp"), "%Y%m%dT%H%M%S%fZ").replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            pass
    stamp = filename[:-4]
    if stamp.isdigit():
        try:
            if len(stamp) == 14:
                return datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc).timestamp()
            if len(stamp) in {10, 13}:
                return int(stamp) / (1000 if len(stamp) == 13 else 1)
        except (OverflowError, ValueError):
            pass
    return 0.0


def _normalise_skill_name(raw: bytes, skill_name: str) -> bytes:
    try:
        content = raw.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError("SKILL.md must be UTF-8") from error
    match = re.match(r"\A---[ \t]*\r?\n(?P<header>.*?)\r?\n---[ \t]*(?P<rest>\r?\n.*|\Z)", content, re.DOTALL)
    if not match:
        raise ValueError("SKILL.md needs YAML frontmatter")
    header = match.group("header")
    descriptions = list(_DESCRIPTION_RE.finditer(header))
    if len(descriptions) != 1 or descriptions[0].group(1).strip() in {"", "''", '""'}:
        raise ValueError("SKILL.md needs one non-empty description")
    names = list(_SKILL_NAME_RE.finditer(header))
    if len(names) > 1:
        raise ValueError("SKILL.md has more than one name")
    if names:
        header = _SKILL_NAME_RE.sub(f"name: {skill_name}", header, count=1)
    else:
        header = f"name: {skill_name}\n{header}"
    return f"---\n{header}\n---{match.group('rest')}".encode("utf-8")


def _extract_snapshot(archive_path: Path, skill_name: str, staging_dir: Path, remaining_bytes: int) -> tuple[int, dict[str, str]]:
    """Validate and unpack one snapshot into a fresh local skill directory."""
    with zipfile.ZipFile(archive_path) as archive:
        members = archive.infolist()
        files = [member for member in members if not member.is_dir()]
        if not files or len(files) > MAX_SKILL_FILES or len(members) > MAX_SKILL_FILES * 2:
            raise ValueError("ZIP contains too many or no skill files")
        names: set[str] = set()
        declared_total = 0
        for member in members:
            name = member.filename[:-1] if member.is_dir() else member.filename
            _safe_member_parts(name)
            file_type = (member.external_attr >> 16) & 0o170000
            if member.flag_bits & 1 or file_type == 0o120000:
                raise ValueError("ZIP contains an encrypted or linked entry")
            if member.is_dir():
                continue
            if name in names:
                raise ValueError("ZIP contains a duplicate member")
            names.add(name)
            declared_total += member.file_size
            if member.file_size > MAX_OBJECT_BYTES or declared_total > remaining_bytes:
                raise ValueError("ZIP expands beyond the skill sync size limit")
        skill_paths = [name for name in names if name == "SKILL.md" or (name.count("/") == 1 and name.endswith("/SKILL.md"))]
        if len(skill_paths) != 1:
            raise ValueError("ZIP needs one SKILL.md at its root or inside one enclosing folder")
        wrapper = skill_paths[0].split("/", 1)[0] if skill_paths[0] != "SKILL.md" else ""
        if wrapper and any(not name.startswith(f"{wrapper}/") for name in names):
            raise ValueError("ZIP files do not share one enclosing folder")
        staging_dir.mkdir(parents=True)
        locations: dict[str, str] = {}
        extracted_bytes = 0
        for member in files:
            relative = member.filename[len(wrapper) + 1:] if wrapper else member.filename
            parts = _safe_member_parts(relative)
            target = staging_dir.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            member_bytes = 0
            with archive.open(member) as source, target.open("wb") as destination:
                while chunk := source.read(1024 * 1024):
                    extracted_bytes += len(chunk)
                    member_bytes += len(chunk)
                    if extracted_bytes > remaining_bytes or member_bytes > MAX_OBJECT_BYTES:
                        raise ValueError("ZIP expands beyond the skill sync size limit")
                    destination.write(chunk)
            locations[relative] = member.filename
        skill_file = staging_dir / "SKILL.md"
        original_size = skill_file.stat().st_size
        normalised = _normalise_skill_name(skill_file.read_bytes(), skill_name)
        if len(normalised) > MAX_OBJECT_BYTES:
            raise ValueError("normalised SKILL.md exceeds the object size limit")
        skill_file.write_bytes(normalised)
        extracted_bytes += len(normalised) - original_size
        if extracted_bytes > remaining_bytes:
            raise ValueError("ZIP expands beyond the skill sync size limit")
        return extracted_bytes, locations


def sync_skills() -> list[str]:
    """Sync newest valid ZIP snapshot per skill, with loose-file fallback."""
    if not skills_enabled():
        return []
    LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    ZIP_RESOURCE_LOCATIONS.clear()
    client = boto3.client(
        "s3", region_name=os.environ.get("AWS_DEFAULT_REGION", "ap-southeast-1")
    )
    paths: list[str] = []
    synced_bytes = 0
    token = None
    try:
        objects: list[dict] = []
        while True:
            request = {"Bucket": BUCKET, "Prefix": PREFIX}
            if token:
                request["ContinuationToken"] = token
            response = client.list_objects_v2(**request)
            objects.extend(response.get("Contents", []))
            if not response.get("IsTruncated"):
                break
            token = response.get("NextContinuationToken")
            if not token:
                logger.warning("Skills sync stopped: S3 listing omitted a continuation token")
                break

        snapshots: dict[str, list[dict]] = {}
        for item in objects:
            key = str(item.get("Key") or "")
            if not key.startswith(PREFIX):
                continue
            parts = key.removeprefix(PREFIX).split("/")
            if len(parts) != 2 or not parts[1].lower().endswith(".zip"):
                continue
            try:
                _local_path_for_key(f"{PREFIX}{parts[0]}/SKILL.md")
                _safe_member_parts(parts[1])
            except ValueError:
                logger.warning("Skipping unsafe ZIP snapshot key %s", key)
                continue
            snapshots.setdefault(parts[0], []).append(item)

        versioned_skills: set[str] = set()
        for skill_name, candidates in snapshots.items():
            candidates.sort(key=lambda item: (_snapshot_time(str(item["Key"]).rsplit("/", 1)[-1], item.get("LastModified")), str(item["Key"])), reverse=True)
            for item in candidates:
                key = str(item.get("Key") or "")
                try:
                    size = int(item.get("Size", 0))
                    if size < 0 or size > MAX_OBJECT_BYTES:
                        raise ValueError("compressed ZIP exceeds the object size limit")
                    with tempfile.TemporaryDirectory(prefix="strands-skill-snapshot-") as temporary:
                        archive_path = Path(temporary) / "archive.zip"
                        staging_dir = Path(temporary) / "unpacked" / skill_name
                        client.download_file(BUCKET, key, str(archive_path))
                        if archive_path.stat().st_size > MAX_OBJECT_BYTES:
                            raise ValueError("downloaded ZIP exceeds the object size limit")
                        used, locations = _extract_snapshot(archive_path, skill_name, staging_dir, MAX_SYNC_BYTES - synced_bytes)
                        target = LOCAL_DIR.resolve() / skill_name
                        if target.is_symlink():
                            raise ValueError("local skill directory is a symlink")
                        if target.exists():
                            shutil.rmtree(target)
                        shutil.move(str(staging_dir), str(target))
                    uri = f"s3://{BUCKET}/{key}"
                    ZIP_RESOURCE_LOCATIONS[skill_name] = {relative: (uri, member) for relative, member in locations.items()}
                    paths.extend(str(target / relative) for relative in locations)
                    synced_bytes += used
                    versioned_skills.add(skill_name)
                    logger.info("Loaded ZIP skill snapshot %s (%d files)", key, len(locations))
                    break
                except Exception as error:
                    logger.warning("Skipping ZIP skill snapshot %s: %s", key, error)

        for item in objects:
            key = str(item.get("Key") or "")
            if key.endswith("/"):
                continue
            relative_parts = key.removeprefix(PREFIX).split("/") if key.startswith(PREFIX) else []
            if len(relative_parts) == 2 and relative_parts[1].lower().endswith(".zip"):
                continue
            if relative_parts and relative_parts[0] in versioned_skills:
                continue
            try:
                size = int(item.get("Size", 0))
                if size < 0:
                    raise ValueError("object size is negative")
                local = _local_path_for_key(key)
            except (TypeError, ValueError) as error:
                logger.warning("Skipping unsafe skill object %s: %s", key, error)
                continue
            if size > MAX_OBJECT_BYTES:
                logger.warning("Skipping oversized skill object %s (%d > %d bytes)", key, size, MAX_OBJECT_BYTES)
                continue
            if synced_bytes + size > MAX_SYNC_BYTES:
                logger.warning("Skipping skill object %s because the sync size limit would be exceeded", key)
                continue
            local.parent.mkdir(parents=True, exist_ok=True)
            try:
                client.download_file(BUCKET, key, str(local))
                actual_size = local.stat().st_size
            except Exception as error:
                logger.warning("Unable to download skill object %s: %s", key, error)
                continue
            if actual_size > MAX_OBJECT_BYTES or synced_bytes + actual_size > MAX_SYNC_BYTES:
                local.unlink(missing_ok=True)
                logger.warning("Discarded oversized downloaded skill object %s", key)
                continue
            synced_bytes += actual_size
            paths.append(str(local))
    except Exception as error:
        logger.warning("Skills sync from s3://%s/%s failed: %s", BUCKET, PREFIX, error)
    logger.info(
        "Skills sync complete: %d files, %d bytes", len(paths), synced_bytes
    )
    return paths


def _resolve_resource(skill_name: str, resource_path: str) -> Path:
    """Resolve one resource while preventing access outside the skill root."""
    requested = Path(resource_path)
    root = LOCAL_DIR.resolve()
    if not skill_name or Path(skill_name).name != skill_name:
        raise ValueError("Skill name must be one directory name")
    if requested.is_absolute():
        raise ValueError("Skill resource path must be relative to its skill directory")
    skill_root = (root / skill_name).resolve()
    if skill_root.parent != root:
        raise ValueError(
            "Skill directory must stay inside the configured skills directory"
        )
    candidate = (skill_root / requested).resolve()
    if candidate == skill_root or skill_root not in candidate.parents:
        raise ValueError(
            "Skill resource path must stay inside its activated skill directory"
        )
    if not candidate.is_file():
        raise FileNotFoundError(f"Skill resource does not exist: {resource_path}")
    return candidate


def skill_resource_s3_location(skill_name: str, resource_path: str) -> tuple[str, str | None]:
    """Return an S3 object URI and optional ZIP member for a synced resource."""
    if not skills_enabled():
        raise ValueError("SKILLS_BUCKET must be configured to stage skill resources")
    local = _resolve_resource(skill_name, resource_path)
    relative = local.relative_to(LOCAL_DIR.resolve()).as_posix()
    member = ZIP_RESOURCE_LOCATIONS.get(skill_name, {}).get(local.relative_to(LOCAL_DIR.resolve() / skill_name).as_posix())
    if member is not None:
        return member
    return f"s3://{BUCKET}/{PREFIX}{relative}", None


def skill_resource_s3_uri(skill_name: str, resource_path: str) -> str:
    """Return a loose resource URI; ZIP members require location metadata."""
    uri, member = skill_resource_s3_location(skill_name, resource_path)
    if member is not None:
        raise ValueError("ZIP skill resources need their member path to be staged")
    return uri


@tool(
    name="read_skill_resource",
    description=(
        "Read a UTF-8 text resource belonging to an activated Agent Skill, including "
        "Markdown, JSON, CSV, SQL, or source code. Pass a path relative to that "
        "skill's directory, such as skill_name="
        "'hospital-data-analyst-nuh' and resource_path='references/emd.md'. Use this "
        "when an activated skill requires a text resource before using an operational "
        "tool. Binary resources need a compatible binary or file-processing tool."
    ),
)
def read_skill_resource(skill_name: str, resource_path: str) -> str:
    """Return a bounded UTF-8 text resource from the local skill cache."""
    try:
        path = _resolve_resource(skill_name, resource_path)
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError, ValueError) as error:
        return f"Unable to read skill resource: {error}"
    if len(content) > MAX_RESOURCE_CHARS:
        return content[:MAX_RESOURCE_CHARS] + "\n\n[skill resource truncated]"
    return content

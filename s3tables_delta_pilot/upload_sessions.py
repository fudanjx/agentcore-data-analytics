"""Private, node-local upload-session state for the uploader v2 API."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


SESSION_TTL_MINUTES = 60


@dataclass
class SessionFile:
    name: str
    path: str
    sha256: str
    size_bytes: int


@dataclass
class UploadSession:
    session_id: str
    owner_user_id: str
    mode: str
    table_bucket_arn: str
    namespace: str
    table: str
    expires_at: str
    files: list[SessionFile]
    phase: str = "RECEIVED"
    progress_message: str = "Files received locally; waiting to profile them."
    error: dict[str, str] | None = None
    preflight: dict[str, Any] | None = None
    key_impact: dict[str, Any] | None = None
    ingestion: dict[str, Any] | None = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    phase_started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def safe_dict(self) -> dict[str, Any]:
        # Original filenames are intentionally exposed only to their owner,
        # never logged or written to S3. They are needed by the local UI.
        value = asdict(self)
        for file in value["files"]:
            file.pop("path", None)
        return value


class UploadSessionStore:
    def __init__(self, root: Path | None = None, ttl_minutes: int = SESSION_TTL_MINUTES):
        self.root = root or Path(os.environ.get("PILOT_UPLOAD_SESSION_ROOT", Path(tempfile.gettempdir()) / "agentcore-s3tables-upload-sessions"))
        self.ttl_minutes = ttl_minutes
        self._sessions: dict[str, UploadSession] = {}
        self._lock = threading.RLock()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            self.root.chmod(0o700)
        except OSError:
            pass

    def create(self, *, owner_user_id: str, mode: str, table_bucket_arn: str, namespace: str,
               table: str, files: Iterable[tuple[str, Any]]) -> UploadSession:
        session_id = uuid.uuid4().hex
        directory = self.root / session_id
        directory.mkdir(mode=0o700)
        session_files: list[SessionFile] = []
        try:
            for number, (name, source) in enumerate(files):
                suffix = ".parquet" if name.lower().endswith((".parquet", ".parquet.gzip")) else Path(name).suffix
                path = directory / f"{number:02d}{suffix}"
                digest = hashlib.sha256()
                size_bytes = 0
                with path.open("wb") as destination:
                    while chunk := source.read(8 * 1024 * 1024):
                        digest.update(chunk)
                        destination.write(chunk)
                        size_bytes += len(chunk)
                path.chmod(0o600)
                session_files.append(SessionFile(name=name, path=str(path), sha256=digest.hexdigest(), size_bytes=size_bytes))
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=self.ttl_minutes)
        session = UploadSession(
            session_id=session_id, owner_user_id=owner_user_id, mode=mode,
            table_bucket_arn=table_bucket_arn, namespace=namespace, table=table,
            expires_at=expires_at.isoformat(), files=session_files,
        )
        with self._lock:
            self._sessions[session_id] = session
            self._write_metadata_locked(session)
        return session

    def get(self, session_id: str, owner_user_id: str) -> UploadSession:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                # Recover metadata after a service restart. Upload bytes remain
                # in the private session directory and are never put in this
                # JSON file.
                metadata = self.root / session_id / "session.json"
                try:
                    value = json.loads(metadata.read_text(encoding="utf-8"))
                    session = self._from_dict(value)
                except (FileNotFoundError, OSError, ValueError, TypeError, KeyError):
                    session = None
                if session is not None:
                    self._sessions[session_id] = session
            if not session or session.owner_user_id != owner_user_id:
                raise KeyError(session_id)
            if datetime.fromisoformat(session.expires_at) <= datetime.now(timezone.utc):
                self._remove_locked(session_id)
                raise KeyError(session_id)
            return session

    def update(self, session_id: str, owner_user_id: str, *, phase: str, progress_message: str,
               error: dict[str, str] | None = None, **values: Any) -> UploadSession:
        with self._lock:
            session = self.get(session_id, owner_user_id)
            now = datetime.now(timezone.utc).isoformat()
            if session.phase != phase:
                session.phase_started_at = now
            session.phase = phase
            session.progress_message = progress_message
            session.error = error
            for key, value in values.items():
                setattr(session, key, value)
            session.updated_at = now
            self._write_metadata_locked(session)
            return session

    def delete(self, session_id: str, owner_user_id: str) -> None:
        with self._lock:
            self.get(session_id, owner_user_id)
            self._remove_locked(session_id)

    def cleanup_expired(self) -> int:
        with self._lock:
            known = dict(self._sessions)
            # Include sessions created by a previous process so a restart does
            # not leave their private upload bytes behind indefinitely.
            for metadata in self.root.glob("*/session.json"):
                session_id = metadata.parent.name
                if session_id in known:
                    continue
                try:
                    known[session_id] = self._from_dict(json.loads(metadata.read_text(encoding="utf-8")))
                except (FileNotFoundError, OSError, ValueError, TypeError, KeyError):
                    continue
            expired = [session_id for session_id, session in known.items()
                       if datetime.fromisoformat(session.expires_at) <= datetime.now(timezone.utc)]
            for session_id in expired:
                self._remove_locked(session_id)
            return len(expired)

    def _remove_locked(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        directory = Path(session.files[0].path).parent if session and session.files else self.root / session_id
        shutil.rmtree(directory, ignore_errors=True)

    @staticmethod
    def _from_dict(value: dict[str, Any]) -> UploadSession:
        files = [SessionFile(**item) for item in value.pop("files", [])]
        return UploadSession(files=files, **value)

    def _write_metadata_locked(self, session: UploadSession) -> None:
        path = self.root / session.session_id / "session.json"
        temporary = path.with_suffix(".json.tmp")
        value = asdict(session)
        temporary.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(path)

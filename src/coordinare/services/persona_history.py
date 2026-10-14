"""Persona version history (spec 130).

Every persona text in effect is kept as a numbered version, so a run can name the
version it was given and the text stays readable after the persona is edited.

One JSON Lines file per role lives at ``<state directory>/persona_history/<role>.jsonl``.
It sits beside the state snapshot, which is writable in a Kubernetes deployment, unlike
the config. Versions are appended and fsynced, so they survive a restart. The cap
(``PERSONA_HISTORY_MAX_VERSIONS`` per role) prunes the oldest by an atomic rewrite. A
pruned version is no longer served by the API, but every run header that used it still
carries the full text and its hash.

The stored text is the **effective** persona text: what ``get_effective_instructions``
returns (the default when the configured text is empty). A version therefore matches
the persona the dispatch used, without the language directive the dispatch appends.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict, ValidationError

from coordinare.lib.transcript_redaction import redact_text
from coordinare.services.persona_service import DEFAULT_INSTRUCTIONS

logger = structlog.get_logger(__name__)

#: Versions kept per persona role. The oldest is pruned once the cap is exceeded.
PERSONA_HISTORY_MAX_VERSIONS = 200
PERSONA_HISTORY_DIRNAME = "persona_history"

#: Who changed a persona when no dashboard session identifies them.
LOCAL_ACTOR = "local"
#: Who changed a persona with the dashboard token, when no signed-in session names them.
#: The token is shared and maps to no user, so the history says so rather than guess.
BEARER_ACTOR = "token"
#: Who changed a persona when the change was not made through the dashboard.
UNKNOWN_ACTOR = "unknown"

SOURCE_DASHBOARD = "dashboard"
SOURCE_DASHBOARD_RESET = "dashboard_reset"
SOURCE_OUTSIDE_DASHBOARD = "outside_dashboard"
SOURCE_FIRST_SEEN = "first_seen"

_ROLE_PATTERN = re.compile(r"[a-z][a-z_]{0,63}")
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


class PersonaVersion(BaseModel):
    """One retained version of one persona role."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    role: str
    version: int
    sha256: str
    text: str
    changed_at: str
    changed_by: str
    source: str


def stored_text(text: str) -> str:
    """The text the history keeps: the persona text with credential patterns redacted.

    Persona text is operator-written, and an operator can paste a secret into it. The
    history is served by the API for as long as the version is retained, so a secret that
    was later removed from the persona must not stay readable there. Only the redacted text
    is stored, and only its hash is compared or served.
    """
    return redact_text(text)


def persona_text_sha256(text: str) -> str:
    """The content hash of a persona text as it is stored (SHA-256, hex)."""
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def effective_persona_text(role: str, configured: str) -> str:
    """The text a persona has in effect: the configured text, or the built-in default when empty.

    Mirrors ``persona_service.get_effective_instructions`` for a text already in hand.
    """
    return configured if configured.strip() else DEFAULT_INSTRUCTIONS[role]


def persona_history_root(config: object) -> Path | None:
    """Where the history lives, or None when there is no daemon configuration."""
    state_path = getattr(config, "state_file_path", None)
    if state_path is None:
        return None
    return Path(state_path).resolve().parent / PERSONA_HISTORY_DIRNAME


class PersonaHistory:
    """Append-only, capped, per-role version store. Thread-safe within one process."""

    def __init__(self, root: Path, *, max_versions: int = PERSONA_HISTORY_MAX_VERSIONS) -> None:
        self._root = root
        self._max_versions = max_versions
        self._cache: dict[str, list[PersonaVersion]] = {}
        self._lock = threading.Lock()

    @property
    def root(self) -> Path:
        return self._root

    def record(
        self,
        role: str,
        text: str,
        *,
        changed_by: str,
        source: str,
    ) -> PersonaVersion | None:
        """Append the redacted ``text`` as a new version, unless it is the latest already.

        Returns the new version, or None when nothing changed. A write that fails raises
        ``OSError`` (after a log line), so the caller can say so: the save it follows has
        already happened and is not undone. Raises ValueError for a role name that could
        not be a file.
        """
        path = self._path(role)
        stored = stored_text(text)
        with self._lock:
            try:
                versions = self._load(role)
                if versions and versions[-1].sha256 == persona_text_sha256(stored):
                    return None
                next_id = versions[-1].version + 1 if versions else 1
                version = PersonaVersion(
                    role=role,
                    version=next_id,
                    sha256=persona_text_sha256(stored),
                    text=stored,
                    changed_at=datetime.now(UTC).isoformat(),
                    changed_by=changed_by,
                    source=source,
                )
                kept = [*versions, version][-self._max_versions:]
                if len(versions) + 1 > self._max_versions:
                    self._rewrite(path, kept)
                else:
                    self._append(path, version)
                self._cache[role] = kept
            except OSError as exc:
                logger.warning(
                    "persona_history.record_failed",
                    role=role,
                    exc_type=type(exc).__name__,
                )
                raise
        logger.info("persona_history.recorded", role=role, version=version.version, source=source)
        return version

    def versions(self, role: str) -> list[PersonaVersion]:
        """The retained versions of ``role``, oldest first. Empty when unreadable."""
        self._path(role)
        with self._lock:
            try:
                return list(self._load(role))
            except OSError:
                return []

    def latest(self, role: str) -> PersonaVersion | None:
        versions = self.versions(role)
        return versions[-1] if versions else None

    def get(self, role: str, version: int) -> PersonaVersion | None:
        return next((v for v in self.versions(role) if v.version == version), None)

    def _path(self, role: str) -> Path:
        if not _ROLE_PATTERN.fullmatch(role):
            message = f"persona role is not a valid name: {role!r}"
            raise ValueError(message)
        return self._root / f"{role}.jsonl"

    def _load(self, role: str) -> list[PersonaVersion]:
        """The retained versions, read once per role and kept in memory. Caller holds the lock."""
        cached = self._cache.get(role)
        if cached is not None:
            return cached
        path = self._path(role)
        loaded: list[PersonaVersion] = []
        if path.exists():
            # Read bytes and judge each line on its own: one corrupt byte costs one line,
            # never the whole history (a UnicodeDecodeError here would hide every version).
            for raw_line in path.read_bytes().split(b"\n"):
                parsed = _parse_version(raw_line)
                if parsed is not None:
                    loaded.append(parsed)
        self._cache[role] = loaded
        return loaded

    def _append(self, path: Path, version: PersonaVersion) -> None:
        """Append one version and fsync. A torn last line is closed off first."""
        self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
        line = version.model_dump_json() + "\n"
        fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT | _NOFOLLOW, 0o600)
        try:
            size = os.fstat(fd).st_size
            if size and os.pread(fd, 1, size - 1) != b"\n":
                line = "\n" + line
            _write_all(fd, line.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)

    def _rewrite(self, path: Path, versions: list[PersonaVersion]) -> None:
        """Replace the file with ``versions`` atomically (used only when pruning)."""
        self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
        temp = path.with_suffix(".jsonl.tmp")
        body = "".join(v.model_dump_json() + "\n" for v in versions).encode("utf-8")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _NOFOLLOW, 0o600)
        try:
            _write_all(fd, body)
            os.fsync(fd)
        finally:
            os.close(fd)
        temp.replace(path)


def _parse_version(raw: bytes) -> PersonaVersion | None:
    """One stored line, or None for a torn, corrupt or foreign line (skipped, never fatal)."""
    if not raw.strip():
        return None
    try:
        data: Any = json.loads(raw.decode("utf-8"))
        return PersonaVersion.model_validate(data)
    except (ValueError, ValidationError):
        return None


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        view = view[written:]


_registry: dict[str, PersonaHistory] = {}
_registry_lock = threading.Lock()


def history_for(root: Path) -> PersonaHistory:
    """The one store for ``root`` in this process, so every writer shares its cache and lock."""
    key = str(root.resolve())
    with _registry_lock:
        store = _registry.get(key)
        if store is None:
            store = PersonaHistory(root)
            _registry[key] = store
        return store


def reference_for_dispatch(root: Path | None, role: str, effective: str) -> dict[str, Any]:
    """The persona version a dispatch runs with. Never raises.

    A text that differs from the latest version was not saved through the dashboard
    (a hand edit of the config file, or a change in the built-in default). It is
    recorded as a version named ``unknown``, so the history never misses a change.
    """
    if root is None:
        return {
            "version": None,
            "reason": "no daemon configuration: persona history is unavailable",
        }
    stored_hash = persona_text_sha256(stored_text(effective))
    try:
        store = history_for(root)
        latest = store.latest(role)
        if latest is None:
            store.record(role, effective, changed_by=UNKNOWN_ACTOR, source=SOURCE_FIRST_SEEN)
        elif latest.sha256 != stored_hash:
            store.record(role, effective, changed_by=UNKNOWN_ACTOR, source=SOURCE_OUTSIDE_DASHBOARD)
        latest = store.latest(role)
    except (OSError, ValueError) as exc:
        return {
            "version": None,
            "reason": f"persona history is unavailable: {type(exc).__name__}",
        }
    if latest is None or latest.sha256 != stored_hash:
        return {"version": None, "reason": "persona history could not record this text"}
    return {
        "version": latest.version,
        "version_sha256": latest.sha256,
        "changed_at": latest.changed_at,
        "changed_by": latest.changed_by,
        "source": latest.source,
        "text": latest.text,
    }

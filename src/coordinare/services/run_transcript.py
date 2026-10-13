"""Run transcripts: the record schema, the per-run JSONL writer, and retention (spec 129).

Every performer run gets one JSONL file under the transcripts root::

    <transcripts root>/<symphony>/<card>/<run_id>.jsonl

The default root is ``<state directory>/transcripts``. It is deliberately NOT
under ``performer_log_dir``: that directory is bind-mounted read-write into
every ephemeral performer container, and a transcript root there would let one
container read and rewrite another run's transcripts. :func:`make_collector`
refuses a root that sits inside any directory mounted into a performer.

The performer keeps the records in a seq-numbered buffer for the life of the
job and the daemon pulls them incrementally while it polls status, so the file
outlives the pod. This module is the single home of the record schema, the
writer and the retention sweep.

Guarantees the writer provides:

* ``seq`` is the dedupe key. A record at or below the run's cursor is skipped,
  so a re-delivered batch never writes a duplicate. A record is accepted only
  when ``cursor < seq <= cursor + batch size`` (and ``seq <= last_seq`` reported
  by the performer), so one bogus seq cannot push the cursor past real records.
* The cursor is recovered from the file itself, so a restarted daemon resumes the
  same file without a second bookkeeping store.
* Every record passes through ``redact_value`` (credential-named keys, embedded
  JSON, token shapes, exact secret values) and the serialized line through
  ``redact_text``, before it touches disk.
* The run file and every directory between the root and it are never followed
  through a symlink. A planted link makes the writer refuse, not write elsewhere.
* Size is bounded per record and per batch. Capture is best effort: a writer
  failure marks the run's transcript failed and is logged once. It never raises.
* The per-run byte cap writes exactly one ``truncated`` marker and then counts
  (not writes) any further content records.
* A torn final line left by a crash mid-write is trimmed before appending.

The performer-side mirror of this schema is ``performer.transcript``. The two
packages do not import each other; the field names are the contract.
"""
from __future__ import annotations

import json
import os
import re
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, get_args

import structlog
from pydantic import BaseModel, ConfigDict, ValidationError

from coordinare.lib.transcript_redaction import redact_text, redact_value, secret_values_from_env

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

logger = structlog.get_logger(__name__)

TranscriptType = Literal[
    "system",
    "user",
    "assistant_text",
    "thinking",
    "tool_call",
    "tool_result",
    "usage",
    "result",
    "error",
    "capability",
    "truncated",
]

TranscriptScope = Literal["message", "run"]

TRANSCRIPT_TYPES: frozenset[str] = frozenset(get_args(TranscriptType))

# Folder name used when a run's symphony or card is unknown (for example a
# status poll made without a transcript target). The file is still written.
UNATTRIBUTED = "unattributed"

# Bounds on what the daemon accepts from one delivery. A record over the
# per-record limit becomes a truncation marker. A batch over the batch limit is
# cut short, and the rest is requested again on the next poll (never skipped).
MAX_RECORD_BYTES = 16 * 1024 * 1024
MAX_BATCH_BYTES = 32 * 1024 * 1024

_SEGMENT_INVALID = re.compile(r"[^A-Za-z0-9._-]+")
_SEGMENT_MAX_LEN = 120
_TRANSCRIPT_SUFFIX = ".jsonl"
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


class TranscriptUsage(BaseModel):
    """Token accounting for one model turn or one run total (see ``scope``)."""

    model_config = ConfigDict(extra="ignore")

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read: int | None = None
    cache_write: int | None = None
    model: str | None = None


class TranscriptRecord(BaseModel):
    """One transcript line. ``text`` carries prose; ``input``/``output`` carry payloads."""

    model_config = ConfigDict(extra="ignore")

    ts: str
    run_id: str = ""
    seq: int
    type: TranscriptType
    text: str | None = None
    tool_name: str | None = None
    call_id: str | None = None
    input: Any = None
    output: Any = None
    is_error: bool | None = None
    usage: TranscriptUsage | None = None
    scope: TranscriptScope | None = None


class _RefusedError(Exception):
    """The writer declined a path (a symlink, a non-directory, a non-regular file)."""


def sanitize_segment(value: str) -> str:
    """Make a symphony, card or run id safe as one path segment.

    Anything outside ``[A-Za-z0-9._-]`` becomes ``_``, leading and trailing dots
    and underscores are stripped (so ``..`` cannot survive), and the result is
    length-capped. An empty result becomes ``UNATTRIBUTED``.
    """
    cleaned = _SEGMENT_INVALID.sub("_", value.strip())[:_SEGMENT_MAX_LEN].strip("._")
    return cleaned or UNATTRIBUTED


def transcript_path(root: Path, *, symphony: str, card_id: str, run_id: str) -> Path:
    """The JSONL path for one run, joined lexically.

    Each segment is sanitized to a single safe name, so the result cannot leave
    ``root``. The path is deliberately not resolved: a symlink planted at any
    component must be seen by the writer, which refuses it.
    """
    return (
        root
        / sanitize_segment(symphony)
        / sanitize_segment(card_id)
        / f"{sanitize_segment(run_id)}{_TRANSCRIPT_SUFFIX}"
    )


def resolve_transcripts_root(
    transcripts_dir: Path | None,
    *,
    config_dir: Path,
    state_dir: Path,
) -> Path:
    """Where transcripts live. Pure: touches nothing on disk.

    ``transcripts_dir`` (``transcripts.dir``) overrides the default. A relative
    override is taken from the config directory. The default is
    ``<state directory>/transcripts``, a daemon-only area beside the state store.
    """
    if transcripts_dir is None:
        return state_dir / "transcripts"
    base = Path(transcripts_dir)
    return base if base.is_absolute() else config_dir / base


def transcript_root_conflicts(root: Path, mounts: Iterable[Path]) -> list[Path]:
    """Mounted host directories that would expose ``root`` to a performer.

    A conflict is a mount that contains the root, or that lies inside it.
    Both directions expose transcripts: the first puts the root inside a mount,
    the second puts a mount (and so a container) inside the root.
    """
    resolved_root = root.resolve()
    conflicts: list[Path] = []
    for mount in mounts:
        resolved = mount.resolve()
        if resolved_root.is_relative_to(resolved) or resolved.is_relative_to(resolved_root):
            conflicts.append(resolved)
    return conflicts


def _seq_of(raw: object) -> int | None:
    if not isinstance(raw, dict):
        return None
    seq = raw.get("seq")
    if isinstance(seq, int) and not isinstance(seq, bool):
        return seq
    return None


def _to_line(record: dict[str, Any], secrets: tuple[str, ...]) -> bytes:
    text = json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str)
    return (redact_text(text, secrets) + "\n").encode()


def _check_chain(root: Path, parts: Sequence[str], *, create: bool) -> Path:
    """Walk ``root/parts`` one component at a time. Refuse any symlink or non-directory.

    ``root`` itself is trusted (the collector resolved it once). Every component
    below it is checked with lstat, so a link planted there is refused.
    """
    current = root
    for part in parts:
        candidate = current / part
        try:
            info = os.lstat(candidate)
        except FileNotFoundError:
            if not create:
                raise
            candidate.mkdir(mode=0o700)
            info = os.lstat(candidate)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            message = f"transcript path component is not a plain directory: {candidate}"
            raise _RefusedError(message)
        current = candidate
    return current


class RunTranscriptWriter:
    """Appends one run's records to its JSONL file. Not thread-safe; one per run.

    Never raises from :meth:`append_batch` or the constructor. A failure marks
    the writer failed, logs ``run_transcript.run_failed`` once, and every later
    call returns 0 without touching the disk.
    """

    def __init__(
        self,
        root: Path,
        *,
        parts: tuple[str, str],
        run_id: str,
        max_bytes: int,
    ) -> None:
        """``parts`` is the (symphony, card) folder pair, already sanitized."""
        self._root = root
        self._parts = parts
        self._file_name = f"{sanitize_segment(run_id)}{_TRANSCRIPT_SUFFIX}"
        self._run_id = run_id
        self._max_bytes = max_bytes
        self._cursor = 0
        self._bytes = 0
        self._truncated = False
        self._failed = False
        self._dropped_records = 0
        self._dropped_bytes = 0
        self._secrets: tuple[str, ...] = ()
        try:
            self._load_existing()
        except Exception as exc:  # best effort: a broken file fails this run only
            self._fail(exc)

    @property
    def path(self) -> Path:
        return self._root.joinpath(*self._parts, self._file_name)

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def cursor(self) -> int:
        """Highest seq written. The next delivery asks the performer for records after it."""
        return self._cursor

    @property
    def truncated(self) -> bool:
        return self._truncated

    @property
    def failed(self) -> bool:
        return self._failed

    @property
    def dropped_records(self) -> int:
        return self._dropped_records

    @property
    def dropped_bytes(self) -> int:
        return self._dropped_bytes

    def add_secret_values(self, values: Iterable[str]) -> None:
        """Exact values to redact from this run's records (job secrets, refreshed tokens)."""
        merged = set(self._secrets)
        merged.update(v for v in values if isinstance(v, str))
        self._secrets = tuple(sorted(merged))

    def mark_failed(self, exc: BaseException) -> None:
        """Record that capture for this run failed. The caller has already contained it."""
        self._fail(exc)

    def _fail(self, exc: BaseException) -> None:
        if self._failed:
            return
        self._failed = True
        logger.warning(
            "run_transcript.run_failed",
            run_id=self._run_id,
            path=str(self.path),
            exc_type=type(exc).__name__,
            error=str(exc),
        )

    def _existing_fd(self) -> int | None:
        """Open the existing run file for reading and truncation, or None if absent."""
        directory = _check_chain(self._root, self._parts, create=False)
        try:
            return os.open(directory / self._file_name, os.O_RDWR | _NOFOLLOW)
        except FileNotFoundError:
            return None

    def _load_existing(self) -> None:
        try:
            fd = self._existing_fd()
        except FileNotFoundError:
            return
        if fd is None:
            return
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                message = f"transcript file is not a regular file: {self.path}"
                raise _RefusedError(message)
            with os.fdopen(os.dup(fd), "rb") as handle:
                data = handle.read()
            complete = data.rfind(b"\n") + 1
            if complete != len(data):
                os.ftruncate(fd, complete)
                data = data[:complete]
                logger.warning("run_transcript.torn_line_trimmed", run_id=self._run_id)
        finally:
            os.close(fd)
        for raw_line in data.splitlines():
            try:
                obj = json.loads(raw_line)
            except ValueError:
                continue
            seq = _seq_of(obj)
            if seq is not None and seq > self._cursor:
                self._cursor = seq
            if isinstance(obj, dict) and obj.get("type") == "truncated":
                self._truncated = True
        self._bytes = len(data)

    def _prepare(self, raw: dict[str, Any]) -> dict[str, Any] | None:
        try:
            parsed = TranscriptRecord.model_validate(raw)
        except ValidationError:
            logger.warning(
                "run_transcript.record_rejected",
                run_id=self._run_id,
                seq=_seq_of(raw),
            )
            return None
        data = parsed.model_dump(exclude_none=True)
        data["run_id"] = self._run_id
        redacted: dict[str, Any] = redact_value(data, self._secrets)
        return redacted

    def _marker(self, seq: int, reason: str) -> dict[str, Any]:
        return {
            "ts": datetime.now(UTC).isoformat(),
            "run_id": self._run_id,
            "seq": seq,
            "type": "truncated",
            "text": f"{reason}; later content records were dropped",
            "input": {"max_bytes": self._max_bytes, "written_bytes": self._bytes},
        }

    def append_batch(self, records: list[dict[str, Any]], *, last_seq: int | None = None) -> int:
        """Write the records that pass validation, in order. Returns lines written.

        A record is accepted only if its seq is above the cursor, inside the
        window ``cursor < seq <= cursor + len(records)``, at most ``last_seq``,
        and strictly above the previous accepted seq in this batch. Anything else
        is logged and skipped without moving the cursor past it.
        """
        if self._failed:
            return 0
        try:
            return self._append(records, last_seq)
        except Exception as exc:  # capture is best effort; never raise into a poll
            self._fail(exc)
            return 0

    def _append(self, records: list[dict[str, Any]], last_seq: int | None) -> int:
        base = self._cursor
        window_end = base + len(records)
        lines: list[bytes] = []
        batch_bytes = 0
        previous_in_batch: int | None = None
        for raw in records:
            seq = _seq_of(raw)
            if seq is None:
                logger.warning("run_transcript.seq_rejected", run_id=self._run_id, reason="no_seq")
                continue
            if seq <= self._cursor:
                continue  # already on disk: a re-delivered record
            out_of_window = not (base < seq <= window_end)
            if out_of_window or (last_seq is not None and seq > last_seq):
                logger.warning(
                    "run_transcript.seq_rejected",
                    run_id=self._run_id,
                    seq=seq,
                    cursor=self._cursor,
                    batch=len(records),
                    last_seq=last_seq,
                    reason="out_of_window",
                )
                continue
            if previous_in_batch is not None and seq <= previous_in_batch:
                logger.warning(
                    "run_transcript.seq_rejected",
                    run_id=self._run_id,
                    seq=seq,
                    reason="not_increasing",
                )
                continue
            record = self._prepare(raw)
            if record is None:
                continue
            line = _to_line(record, self._secrets)
            if lines and batch_bytes + len(line) > MAX_BATCH_BYTES:
                break  # the rest is re-requested on the next poll
            out = self._admit(record, seq, line)
            previous_in_batch = seq
            self._cursor = seq
            if out is not None:
                lines.append(out)
                batch_bytes += len(out)
                self._bytes += len(out)
        if lines:
            self._write(b"".join(lines))
        return len(lines)

    def _admit(self, record: dict[str, Any], seq: int, line: bytes) -> bytes | None:
        """Decide what one accepted record becomes on disk: its line, a marker, or nothing."""
        if record["type"] == "truncated":
            if self._truncated:
                return None
            self._truncated = True
            return line
        if self._truncated:
            self._dropped_records += 1
            self._dropped_bytes += len(line)
            return None
        if len(line) > MAX_RECORD_BYTES:
            return self._cap_marker(seq, len(line), "record exceeded the per-record limit")
        if self._bytes + len(line) > self._max_bytes:
            return self._cap_marker(seq, len(line), f"transcript byte cap reached ({self._max_bytes} bytes)")
        return line

    def _cap_marker(self, seq: int, dropped_len: int, reason: str) -> bytes:
        self._truncated = True
        self._dropped_records += 1
        self._dropped_bytes += dropped_len
        return _to_line(self._marker(seq, reason), self._secrets)

    def _write(self, payload: bytes) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        directory = _check_chain(self._root, self._parts, create=True)
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | _NOFOLLOW
        fd = os.open(directory / self._file_name, flags, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                message = f"transcript file is not a regular file: {self.path}"
                raise _RefusedError(message)
            os.write(fd, payload)
        finally:
            os.close(fd)


class TranscriptCollector:
    """Owns the transcripts root and one writer per in-flight run.

    Constructing a collector touches nothing on disk. Directories and files are
    created only when a run's first batch is written.
    """

    def __init__(
        self,
        root: Path,
        *,
        enabled: bool,
        max_bytes_per_run: int,
        retention_days: int = 0,
        ambient_secrets: Mapping[str, str] | None = None,
    ) -> None:
        self._root = root.resolve()
        self._enabled = enabled
        self._max_bytes_per_run = max_bytes_per_run
        self._retention_days = retention_days
        self._ambient_secrets = secret_values_from_env(ambient_secrets or {})
        self._writers: dict[str, RunTranscriptWriter] = {}

    @property
    def root(self) -> Path:
        return self._root

    @property
    def retention_days(self) -> int:
        return self._retention_days

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def max_bytes_per_run(self) -> int:
        return self._max_bytes_per_run

    def writer_for(self, *, run_id: str, symphony: str, card_id: str) -> RunTranscriptWriter:
        """The writer for ``run_id``, created on first use. The file path is fixed then."""
        existing = self._writers.get(run_id)
        if existing is not None:
            return existing
        writer = RunTranscriptWriter(
            self._root,
            parts=(sanitize_segment(symphony), sanitize_segment(card_id)),
            run_id=run_id,
            max_bytes=self._max_bytes_per_run,
        )
        writer.add_secret_values(self._ambient_secrets)
        self._writers[run_id] = writer
        return writer

    def add_ambient_secrets(self, environ: Mapping[str, str]) -> None:
        """Exact values of credential-named environment variables, for future writers."""
        self._ambient_secrets = secret_values_from_env(environ)

    def release(self, run_id: str) -> None:
        """Forget a finished run's writer. The file stays on disk."""
        writer = self._writers.pop(run_id, None)
        if writer is not None and writer.truncated:
            logger.info(
                "run_transcript.run_truncated",
                run_id=run_id,
                dropped_records=writer.dropped_records,
                dropped_bytes=writer.dropped_bytes,
                max_bytes=self._max_bytes_per_run,
            )


def make_collector(
    *,
    enabled: bool,
    root: Path,
    max_bytes_per_run: int,
    retention_days: int,
    performer_mounts: Sequence[Path],
) -> TranscriptCollector:
    """Build the collector, refusing a root a performer can reach.

    When the root sits inside (or contains) a directory mounted into a performer,
    transcription is switched off for this process and an error is logged. The
    daemon keeps running: transcripts are diagnostics, and refusing to start
    would take the orchestrator down over an optional setting. The error is
    logged at error level, so the condition is visible rather than silent.
    """
    conflicts = transcript_root_conflicts(root, performer_mounts) if enabled else []
    if conflicts:
        logger.error(
            "run_transcript.root_mounted_into_performer",
            root=str(root),
            mounts=[str(c) for c in conflicts],
            action="transcripts disabled for this process; set transcripts.dir outside every performer mount",
        )
        enabled = False
    return TranscriptCollector(
        root,
        enabled=enabled,
        max_bytes_per_run=max_bytes_per_run,
        retention_days=retention_days,
    )


def prune_expired(root: Path, *, retention_days: int, now: datetime | None = None) -> int:
    """Delete transcript files whose mtime is older than ``retention_days``.

    ``retention_days == 0`` keeps everything. Only regular ``.jsonl`` files
    inside ``root`` are candidates; symlinks are never followed or removed.
    Directories left empty by the sweep are removed, but ``root`` itself is not.
    Returns the number of files deleted.
    """
    if retention_days <= 0 or not root.is_dir():
        return 0
    base = root.resolve()
    cutoff = ((now or datetime.now(UTC)) - timedelta(days=retention_days)).timestamp()
    removed = 0
    for dirpath, _dirnames, filenames in os.walk(base, followlinks=False):
        for name in filenames:
            if not name.endswith(_TRANSCRIPT_SUFFIX):
                continue
            candidate = Path(dirpath) / name
            if candidate.is_symlink() or not candidate.is_file():
                continue
            if not candidate.resolve().is_relative_to(base):
                continue
            if candidate.stat().st_mtime < cutoff:
                candidate.unlink()
                removed += 1
    for dirpath, _dirnames, _filenames in os.walk(base, topdown=False, followlinks=False):
        folder = Path(dirpath)
        if folder == base:
            continue
        if not any(folder.iterdir()):
            folder.rmdir()
    logger.info("run_transcript.retention_pruned", removed=removed, retention_days=retention_days)
    return removed


__all__ = [
    "MAX_BATCH_BYTES",
    "MAX_RECORD_BYTES",
    "TRANSCRIPT_TYPES",
    "UNATTRIBUTED",
    "RunTranscriptWriter",
    "TranscriptCollector",
    "TranscriptRecord",
    "TranscriptScope",
    "TranscriptType",
    "TranscriptUsage",
    "make_collector",
    "prune_expired",
    "resolve_transcripts_root",
    "sanitize_segment",
    "transcript_path",
    "transcript_root_conflicts",
]

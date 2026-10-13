"""Run transcripts: the record schema, the per-run JSONL writer, and retention (spec 129).

Every performer run gets one JSONL file under the transcripts root::

    <performer_log_dir>/transcripts/<symphony>/<card>/<run_id>.jsonl

The performer keeps the records in a seq-numbered buffer for the life of the
job and the daemon pulls them incrementally while it polls status, so the file
outlives the pod. This module is the single home of the record schema, the
writer and the retention sweep. Later transcript features (run header, conductor
calls, the dashboard viewer, token accounting) reuse these types.

Guarantees the writer provides:

* ``seq`` is the dedupe key. A record whose seq is at or below the run's cursor
  is skipped, so a re-delivered batch never writes a duplicate.
* The cursor is recovered from the file itself, so a restarted daemon resumes the
  same file without a second bookkeeping store.
* Every record is passed through ``redact_value`` and then ``redact_secrets``
  on the serialized line before it touches disk.
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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, get_args

import structlog
from pydantic import BaseModel, ConfigDict, ValidationError

from coordinare.attempt_log import resolve_writable_log_dir
from coordinare.lib.redaction import redact_secrets, redact_value

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

TRANSCRIPT_TYPES: frozenset[str] = frozenset(get_args(TranscriptType))

# Folder name used when a run's symphony or card is unknown (for example a
# status poll made without a transcript target). The file is still written.
UNATTRIBUTED = "unattributed"

_SEGMENT_INVALID = re.compile(r"[^A-Za-z0-9._-]+")
_SEGMENT_MAX_LEN = 120
_TRANSCRIPT_SUFFIX = ".jsonl"


class TranscriptUsage(BaseModel):
    """Token accounting for one model turn, or a run total when ``text`` says so."""

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


def sanitize_segment(value: str) -> str:
    """Make a symphony, card or run id safe as one path segment.

    Anything outside ``[A-Za-z0-9._-]`` becomes ``_``, leading and trailing dots
    and underscores are stripped (so ``..`` cannot survive), and the result is
    length-capped. An empty result becomes ``UNATTRIBUTED``.
    """
    cleaned = _SEGMENT_INVALID.sub("_", value.strip())[:_SEGMENT_MAX_LEN].strip("._")
    return cleaned or UNATTRIBUTED


def transcript_path(root: Path, *, symphony: str, card_id: str, run_id: str) -> Path:
    """The JSONL path for one run. Raises ``ValueError`` if it would leave ``root``."""
    base = root.resolve()
    path = (
        base
        / sanitize_segment(symphony)
        / sanitize_segment(card_id)
        / f"{sanitize_segment(run_id)}{_TRANSCRIPT_SUFFIX}"
    ).resolve()
    if not path.is_relative_to(base):
        message = f"transcript path escapes the transcripts root: {path}"
        raise ValueError(message)
    return path


def resolve_transcripts_root(
    performer_log_dir: Path | None,
    *,
    attempt_base: Path,
    state_base: Path,
) -> Path:
    """Where transcripts live. Defaults to ``<attempts parent>/logs/transcripts``.

    With ``performer_log_dir`` set, the root is ``<performer_log_dir>/transcripts``.
    With it unset, the root sits beside the attempt log (``<config dir>/logs``),
    the same base the attempt log already uses. An unwritable candidate falls
    back to the state directory, as the attempt log does (issue 493).
    """
    if performer_log_dir is not None:
        base = Path(performer_log_dir)
        if not base.is_absolute():
            base = attempt_base / base
    else:
        base = attempt_base / "logs"
    return resolve_writable_log_dir(
        base / "transcripts",
        state_base / "logs" / "transcripts",
    )


def _seq_of(raw: object) -> int | None:
    if not isinstance(raw, dict):
        return None
    seq = raw.get("seq")
    if isinstance(seq, int) and not isinstance(seq, bool):
        return seq
    return None


def _to_line(record: dict[str, Any]) -> bytes:
    text = json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str)
    return (redact_secrets(text) + "\n").encode()


class RunTranscriptWriter:
    """Appends one run's records to its JSONL file. Not thread-safe; one per run."""

    def __init__(self, path: Path, *, run_id: str, max_bytes: int) -> None:
        self._path = path
        self._run_id = run_id
        self._max_bytes = max_bytes
        self._cursor = 0
        self._bytes = 0
        self._truncated = False
        self._dropped_records = 0
        self._dropped_bytes = 0
        self._load_existing()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def cursor(self) -> int:
        """Highest seq written. The next delivery asks the performer for records after it."""
        return self._cursor

    @property
    def truncated(self) -> bool:
        return self._truncated

    @property
    def dropped_records(self) -> int:
        return self._dropped_records

    @property
    def dropped_bytes(self) -> int:
        return self._dropped_bytes

    def _load_existing(self) -> None:
        if not self._path.exists():
            return
        data = self._path.read_bytes()
        complete = data.rfind(b"\n") + 1
        if complete != len(data):
            data = data[:complete]
            with self._path.open("r+b") as handle:
                handle.truncate(complete)
            logger.warning("run_transcript.torn_line_trimmed", run_id=self._run_id)
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
        redacted: dict[str, Any] = redact_value(data)
        return redacted

    def _marker(self, seq: int) -> dict[str, Any]:
        return {
            "ts": datetime.now(UTC).isoformat(),
            "run_id": self._run_id,
            "seq": seq,
            "type": "truncated",
            "text": (
                f"transcript byte cap reached ({self._max_bytes} bytes); "
                "later content records were dropped"
            ),
            "input": {"max_bytes": self._max_bytes, "written_bytes": self._bytes},
        }

    def append_batch(self, records: list[dict[str, Any]]) -> int:
        """Write the records whose seq is above the cursor, in order.

        Returns how many lines were written. Records at or below the cursor are
        silently skipped, which makes a re-delivered batch harmless.
        """
        lines: list[bytes] = []
        for raw in records:
            seq = _seq_of(raw)
            if seq is None or seq <= self._cursor:
                continue
            self._cursor = seq
            record = self._prepare(raw)
            if record is None:
                continue
            if record["type"] == "truncated":
                if self._truncated:
                    continue
                self._truncated = True
                line = _to_line(record)
            elif self._truncated:
                self._dropped_records += 1
                continue
            else:
                line = _to_line(record)
                if self._bytes + len(line) > self._max_bytes:
                    marker = _to_line(self._marker(seq))
                    lines.append(marker)
                    self._bytes += len(marker)
                    self._truncated = True
                    self._dropped_records += 1
                    self._dropped_bytes += len(line)
                    continue
            lines.append(line)
            self._bytes += len(line)
        if lines:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("ab") as handle:
                handle.write(b"".join(lines))
        return len(lines)


class TranscriptCollector:
    """Owns the transcripts root and one writer per in-flight run."""

    def __init__(
        self,
        root: Path,
        *,
        enabled: bool,
        max_bytes_per_run: int,
        retention_days: int = 0,
    ) -> None:
        self._root = root
        self._enabled = enabled
        self._max_bytes_per_run = max_bytes_per_run
        self._retention_days = retention_days
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
        path = transcript_path(self._root, symphony=symphony, card_id=card_id, run_id=run_id)
        writer = RunTranscriptWriter(path, run_id=run_id, max_bytes=self._max_bytes_per_run)
        self._writers[run_id] = writer
        return writer

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
    "TRANSCRIPT_TYPES",
    "UNATTRIBUTED",
    "RunTranscriptWriter",
    "TranscriptCollector",
    "TranscriptRecord",
    "TranscriptType",
    "TranscriptUsage",
    "prune_expired",
    "resolve_transcripts_root",
    "sanitize_segment",
    "transcript_path",
]

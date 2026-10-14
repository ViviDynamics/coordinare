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
* A ``usage`` record carries a ``stage`` key when the daemon knows the run's
  performer stage (spec 134). It is the only key the daemon adds to a performer
  record, and readers that ignore unknown keys are unaffected.
* A torn final line left by a crash mid-write is trimmed before appending.

The performer-side mirror of this schema is ``performer.transcript``. The two
packages do not import each other; the field names are the contract.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import stat
import threading
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, get_args

import structlog
from pydantic import BaseModel, ConfigDict, ValidationError

from coordinare.lib.path_segments import (
    CONDUCTOR_FOLDER,
    UNATTRIBUTED,
    is_conductor_folder,
    sanitize_segment,
)
from coordinare.lib.transcript_redaction import redact_value, secret_values_from_env
from coordinare.services.token_usage import TokenLedger

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

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

# Bounds on what the daemon accepts from one delivery. A record over the
# per-record limit becomes a truncation marker. A batch over the batch limit is
# cut short, and the rest is requested again on the next poll (never skipped).
MAX_RECORD_BYTES = 16 * 1024 * 1024
MAX_BATCH_BYTES = 32 * 1024 * 1024

_TRANSCRIPT_SUFFIX = ".jsonl"
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
# A FIFO at the run path must not hang a worker thread (review item 3a).
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)


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

    ``transcripts.dir`` overrides the default. ``~`` is expanded, and a relative
    value is taken from the config directory. The default is
    ``<state directory>/transcripts``.
    """
    if transcripts_dir is None:
        return state_dir / "transcripts"
    base = Path(transcripts_dir).expanduser()
    return base if base.is_absolute() else config_dir / base


@dataclass(frozen=True)
class RootGuard:
    """What the transcripts root must not overlap (129 review, item 3).

    ``performer_mounts``: host directories a performer container can see (the
    performer log dir, endpoint volume host paths, env-cache roots). The root may
    not lie inside one of them, nor contain one.

    ``protected``: daemon-owned directories (state, config, attempt log). The root
    may not equal one of them or contain one. Lying inside one is allowed, because
    the default root is inside the state directory.
    """

    performer_mounts: tuple[Path, ...] = ()
    protected: tuple[Path, ...] = ()


def _case_insensitive(path: Path) -> bool:
    """Probe, not guess: is the filesystem that holds ``path`` case-insensitive?

    The nearest existing ancestor is tested by swapping its name's case and asking the
    filesystem whether that name is the same file. Fails closed: when the probe cannot tell
    (the filesystem root, or a name with no letters to swap, or an error), the answer is True,
    so the comparison folds case rather than trusting a guess (219 final check).
    """
    probe = path
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    name = probe.name
    if not name:
        return True
    swapped = name.swapcase()
    if swapped == name:
        return True
    alternate = probe.parent / swapped
    try:
        return alternate.exists() and alternate.samefile(probe)
    except OSError:
        return True


def comparable_part(part: str, *, insensitive: bool) -> str:
    """One path component in the form two spellings are compared in (219 final check).

    NFC first, because APFS treats NFC and NFD spellings of a name as one name, then casefold
    where the filesystem folds case. Every path comparison that guards a mount uses this.
    """
    normalised = unicodedata.normalize("NFC", part)
    return normalised.casefold() if insensitive else normalised


def _within(child: Path, parent: Path) -> bool:
    """True when ``child`` equals ``parent`` or lies inside it (compared case-insensitively
    where the filesystem is case-insensitive)."""
    insensitive = _case_insensitive(parent) or _case_insensitive(child)
    child_parts = tuple(comparable_part(p, insensitive=insensitive) for p in child.resolve().parts)
    parent_parts = tuple(comparable_part(p, insensitive=insensitive) for p in parent.resolve().parts)
    return child_parts[: len(parent_parts)] == parent_parts


def transcript_root_conflicts(root: Path, guard: RootGuard) -> list[Path]:
    """The guarded directories that conflict with ``root``. Empty means the root is safe."""
    conflicts = [
        mount for mount in guard.performer_mounts if _within(root, mount) or _within(mount, root)
    ]
    conflicts.extend(directory for directory in guard.protected if _within(directory, root))
    return [path.resolve() for path in conflicts]


def _seq_key(raw: dict[str, Any]) -> int:
    seq = _seq_of(raw)
    return seq if seq is not None else 0


def _withheld_record(run_id: str, seq: int) -> dict[str, Any]:
    """The marker written in place of a record that could not be recorded (item 1)."""
    return {
        "ts": datetime.now(UTC).isoformat(),
        "run_id": run_id,
        "seq": seq,
        "type": "error",
        "text": f"transcript record {seq} withheld: could not be recorded",
    }


def _reseal_persona_hash(header: dict[str, Any]) -> None:
    """Hash the persona text the header carries after every redaction (spec 130, review item 3).

    The builder hashes the text after pattern redaction. Exact job secrets are removed
    here, so the hash is recomputed over what reaches the file. Anyone can then reproduce
    ``sent_text_sha256`` from the transcript line alone.
    """
    persona = header.get("persona")
    if isinstance(persona, dict) and isinstance(persona.get("sent_text"), str):
        sent = persona["sent_text"]
        persona["sent_text_sha256"] = hashlib.sha256(sent.encode("utf-8", "surrogatepass")).hexdigest()


def _seq_of(raw: object) -> int | None:
    if not isinstance(raw, dict):
        return None
    seq = raw.get("seq")
    if isinstance(seq, int) and not isinstance(seq, bool):
        return seq
    return None


def _to_line(record: dict[str, Any]) -> bytes:
    """One JSON line, ASCII only.

    Values were redacted before this point. The line itself is not rewritten, so it
    always parses and keeps its seq (review item 2). ``ensure_ascii`` escapes lone
    surrogates, so every line encodes.
    """
    text = json.dumps(record, ensure_ascii=True, separators=(",", ":"), default=str)
    return (text + "\n").encode("ascii")


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
            with contextlib.suppress(FileExistsError):
                # Created by a concurrent writer: the lstat below decides.
                candidate.mkdir(mode=0o700)
            info = os.lstat(candidate)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            message = f"transcript path component is not a plain directory: {candidate}"
            raise _RefusedError(message)
        current = candidate
    return current


_TAIL_CHUNK = 64 * 1024


def _write_all(fd: int, payload: bytes) -> None:
    """Write every byte. ``os.write`` may return a short count."""
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _refuse_unless_private_regular(info: os.stat_result, path: Path) -> None:
    """Refuse anything but a regular file with exactly one name (no hard links).

    A second hard link would make the file reachable from another location, so an
    append could land in a file the root does not own (129 review, item 4).
    """
    if not stat.S_ISREG(info.st_mode):
        message = f"transcript file is not a regular file: {path}"
        raise _RefusedError(message)
    if info.st_nlink != 1:
        message = f"transcript file has {info.st_nlink} hard links: {path}"
        raise _RefusedError(message)


def _rfind_newline(fd: int, end: int) -> int:
    """Index of the last newline before ``end`` in the open file, or -1. Reads backward in chunks."""
    pos = end
    while pos > 0:
        start = max(0, pos - _TAIL_CHUNK)
        chunk = os.pread(fd, pos - start, start)
        index = chunk.rfind(b"\n")
        if index != -1:
            return start + index
        pos = start
    return -1


def _parse_line(data: bytes) -> object | None:
    try:
        parsed: object = json.loads(data)
    except (ValueError, RecursionError):
        return None
    return parsed


_MAX_TAIL_LINES = 1000


def _last_parsable_record(fd: int, end: int) -> object | None:
    """The last line, scanning back from ``end`` (its newline index), that parses as JSON.

    A torn or garbled last line must not reset the cursor to 0: that would re-pull
    the whole run and write duplicates (review item 2). The scan is bounded.
    """
    for _ in range(_MAX_TAIL_LINES):
        if end < 0:
            return None
        start = _rfind_newline(fd, end) + 1
        obj = _parse_line(os.pread(fd, end - start, start))
        if isinstance(obj, dict) and _seq_of(obj) is not None:
            return obj
        end = start - 1
    return None


class RunTranscriptWriter:
    """Appends one run's records to its JSONL file.

    Construction does no I/O. The cursor is read from the file's tail on the first
    append, and every append runs on a worker thread (the daemon calls it through
    ``asyncio.to_thread``), so neither the file nor redaction ever blocks the event
    loop. A lock serialises appends for one run.

    Never raises from :meth:`append_batch`. A failure marks the writer failed,
    logs ``run_transcript.run_failed`` once, and every later call returns 0.
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
        self._loaded = False
        self._dropped_records = 0
        self._dropped_bytes = 0
        self._secrets: tuple[str, ...] = ()
        # The highest seq the performer has reported. A tail beyond the cursor is known
        # to exist only when this is larger (review round 4, item 4).
        self._performer_last: int | None = None
        # 130: the run header, held until it is written as the file's first line.
        self._pending_header: dict[str, Any] | None = None
        self._lock = threading.Lock()
        # 134: the stage the daemon stamps on usage records, and the accepted usage
        # records the ledger has not taken yet.
        self._stage = ""
        self._usage_out: list[dict[str, Any]] = []

    @property
    def path(self) -> Path:
        return self._root.joinpath(*self._parts, self._file_name)

    @property
    def parts(self) -> tuple[str, str]:
        """The (symphony, card) folder pair, sanitized as the file path uses them."""
        return self._parts

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def stage(self) -> str:
        return self._stage

    def set_stage(self, stage: str) -> None:
        """The performer stage of this run. Written as ``stage`` on usage records only (134)."""
        self._stage = stage

    def drain_usage(self) -> list[dict[str, Any]]:
        """The usage records accepted since the last call, redacted, each once (134).

        Records past the byte cap are included: the file drops them, the count does not.
        """
        with self._lock:
            taken, self._usage_out = self._usage_out, []
        return taken

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

    def observe_last_seq(self, value: int | None) -> None:
        """Remember the highest seq the performer has reported (memory only)."""
        if value is None:
            return
        if self._performer_last is None or value > self._performer_last:
            self._performer_last = value

    def record_tail_lost(self, reason: str) -> None:
        """Write one ``truncated`` marker for a tail the daemon could not fetch.

        The marker takes the next seq, so the file says where the transcript stops
        (review round 4, item 4). Only one marker is written, and only when records
        were actually missed. Blocking: run it on a worker thread.
        """
        if self._failed or self._performer_last is None:
            return
        self.ensure_loaded()
        with self._lock:
            self._flush_pending_header_locked()  # 130: the header is always the first line
            if self._failed or self._truncated:
                return
            seq = self._cursor + 1
            if seq > self._performer_last:
                return
            self._truncated = True
            self._cursor = seq
            marker_text = f"records after seq {seq - 1} were not retrieved: {reason}"
            try:
                self._write(_to_line(self._marker(seq, marker_text)))
            except Exception as exc:  # best effort: a failed marker fails the run quietly
                self._fail(exc)

    def stage_run_header(self, header: dict[str, Any]) -> None:
        """Hold the run's header until it is written as the file's first line (spec 130).

        Memory only, so it never raises and never waits on the disk. Every content write
        flushes a staged header first, so the header stays first even when the caller's
        flush timed out and the write is still on its way to disk.
        """
        self._pending_header = header

    def flush_run_header(self) -> bool:
        """Write the staged header as the first line. Blocking. Never raises.

        Returns True when the header is on disk. A file that already has content gets
        no header. A failure is logged, and the run carries on without one: the header
        is diagnostics, and records must still be captured.
        """
        self.ensure_loaded()
        with self._lock:
            return self._flush_pending_header_locked()

    def _flush_pending_header_locked(self) -> bool:
        """Write the staged header, if any. The caller holds ``self._lock``."""
        header = self._pending_header
        if header is None or self._failed:
            return False
        self._pending_header = None
        if self._cursor or self._bytes:
            logger.warning(
                "run_transcript.header_skipped",
                run_id=self._run_id,
                reason="file already has records",
            )
            return False
        try:
            redacted: dict[str, Any] = redact_value(header, self._secrets)
            _reseal_persona_hash(redacted)
            line = _to_line(redacted)
            self._write(line)
            self._bytes += len(line)
        except Exception as exc:  # best effort: the run's records still follow
            logger.warning(
                "run_transcript.header_failed",
                run_id=self._run_id,
                exc_type=type(exc).__name__,
            )
            return False
        return True

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
        """Open the existing run file read-write, refusing links, or None if absent."""
        directory = _check_chain(self._root, self._parts, create=False)
        try:
            return os.open(directory / self._file_name, os.O_RDWR | _NONBLOCK | _NOFOLLOW)
        except FileNotFoundError:
            return None

    def _load_tail(self) -> None:
        """Read the cursor and the cap state from the file's last line only.

        Records are written in seq order, so the last complete line carries the
        highest seq. A cap marker is always the last line too: after it nothing
        content-bearing is written. The file is never read whole.
        """
        try:
            fd = self._existing_fd()
        except FileNotFoundError:
            return  # no directory yet: a first write creates it
        if fd is None:
            return
        try:
            info = os.fstat(fd)
            _refuse_unless_private_regular(info, self.path)
            size = info.st_size
            newline = _rfind_newline(fd, size)
            complete = newline + 1
            if complete != size:
                os.ftruncate(fd, complete)
                size = complete
                logger.warning("run_transcript.torn_line_trimmed", run_id=self._run_id)
            if complete:
                obj = _last_parsable_record(fd, newline)
                seq = _seq_of(obj)
                if seq is not None:
                    self._cursor = seq
                else:
                    # No parsable record in the scanned tail. Resetting the cursor would
                    # re-pull the whole run, so the run's transcript fails instead (round 4).
                    self._fail(ValueError("no parsable record in the transcript tail"))
                if isinstance(obj, dict) and obj.get("type") == "truncated":
                    self._truncated = True
            self._bytes = size
        finally:
            os.close(fd)

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
        if data.get("type") == "usage" and self._stage:
            # 134: the stage is not on the wire; the daemon knows it and the ledger
            # rebuilds per-stage totals from it after a restart.
            data["stage"] = self._stage
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

    @property
    def loaded(self) -> bool:
        """True once the cursor has been read from the file (or the file is known absent)."""
        return self._loaded

    def ensure_loaded(self) -> None:
        """Read the cursor and cap state from the file's tail, once.

        Blocking: the daemon calls it through ``asyncio.to_thread`` before it first
        needs ``cursor``. Never raises: a failure marks the writer failed.
        """
        with self._lock:
            if self._loaded or self._failed:
                return
            try:
                self._load_tail()
            except Exception as exc:  # best effort: a broken file fails this run only
                self._fail(exc)
            finally:
                # Set only after the cursor is read: a poll that sees ``loaded`` must
                # never see the placeholder cursor of 0 (it would re-pull the run).
                self._loaded = True

    def append_batch(self, records: list[dict[str, Any]], *, last_seq: int | None = None) -> int:
        """Write the records that pass validation, in order. Returns lines written.

        A record is accepted only if its seq is above the cursor, inside the
        window ``cursor < seq <= cursor + len(records)``, at most ``last_seq``,
        and strictly above the previous accepted seq in this batch. Anything else
        is logged and skipped without moving the cursor past it.

        Blocking: call it from a worker thread (``asyncio.to_thread``).
        """
        if self._failed:
            return 0
        self.ensure_loaded()
        if self._failed:
            return 0
        with self._lock:
            try:
                return self._append(records, last_seq)
            except Exception as exc:  # capture is best effort; never raise into a poll
                self._fail(exc)
                return 0

    def _append(self, records: list[dict[str, Any]], last_seq: int | None) -> int:
        self._flush_pending_header_locked()  # 130: a staged header precedes every record
        base = self._cursor
        window_end = base + len(records)
        lines: list[bytes] = []
        batch_bytes = 0
        previous_in_batch: int | None = None
        # Records are written in seq order. A batch that arrives out of order is
        # sorted, so [3, 2, 1] is written as 1, 2, 3 rather than partially (item 8).
        seqd = [raw for raw in records if _seq_of(raw) is not None]
        if len(seqd) != len(records):
            logger.warning("run_transcript.seq_rejected", run_id=self._run_id, reason="no_seq")
        for raw in sorted(seqd, key=_seq_key):
            seq = _seq_of(raw)
            if seq is None:
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
            try:
                record = self._prepare(raw)
                if record is None:
                    continue
                line = _to_line(record)
            except Exception as exc:  # one bad record: a marker in its place, then go on
                logger.warning(
                    "run_transcript.record_withheld",
                    run_id=self._run_id,
                    seq=seq,
                    exc_type=type(exc).__name__,
                )
                record = _withheld_record(self._run_id, seq)
                line = _to_line(record)
            if lines and batch_bytes + len(line) > MAX_BATCH_BYTES:
                break  # the rest is re-requested on the next poll
            if record.get("type") == "usage":
                # 134: counted here, before the byte cap, so a capped run keeps its totals.
                self._usage_out.append(record)
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
            return self._cap_marker(
                seq,
                len(line),
                f"transcript byte cap reached ({self._max_bytes} bytes)",
            )
        return line

    def _cap_marker(self, seq: int, dropped_len: int, reason: str) -> bytes:
        self._truncated = True
        self._dropped_records += 1
        self._dropped_bytes += dropped_len
        return _to_line(self._marker(seq, reason))

    def _write(self, payload: bytes) -> None:
        """Append ``payload`` completely, or leave the file as it was (review item 3b)."""
        self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = self._open_for_append()
        try:
            start = os.fstat(fd).st_size
            try:
                _write_all(fd, payload)
            except OSError:
                # A short or failed write must not leave a torn line mid-file.
                os.ftruncate(fd, start)
                raise
        finally:
            os.close(fd)

    def _open_for_append(self) -> int:
        """Open the run file for append. A directory that vanished under the open is retried once."""
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | _NONBLOCK | _NOFOLLOW
        for attempt in range(2):
            directory = _check_chain(self._root, self._parts, create=True)
            try:
                fd = os.open(directory / self._file_name, flags, 0o600)
            except FileNotFoundError:
                if attempt:
                    raise
                continue
            try:
                _refuse_unless_private_regular(os.fstat(fd), self.path)
            except _RefusedError:
                os.close(fd)
                raise
            return fd
        message = f"could not open transcript file: {self.path}"
        raise OSError(message)


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
        # 134: the usage ledger the accepted usage records feed. It lives on the
        # collector because the collector is the one object the daemon shares with the
        # poll path, and it is empty whenever transcription is off.
        self.usage = TokenLedger()

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
        writer = self.new_writer(run_id=run_id, symphony=symphony, card_id=card_id)
        self._writers[run_id] = writer
        return writer

    def new_writer(self, *, run_id: str, symphony: str, card_id: str) -> RunTranscriptWriter:
        """A writer for one file that is not kept in the live set (spec 131: one-shot files).

        Built with the same path rules and ambient secrets as :meth:`writer_for`. The caller
        owns it, so it can be used on a worker thread without touching the live-writer map.
        A symphony name is always sanitised here, so it can never reach the conductor's folder.
        """
        return self._one_shot(run_id=run_id, folder=sanitize_segment(symphony), card_id=card_id)

    def conductor_writer(self, *, run_id: str, card_id: str) -> RunTranscriptWriter:
        """A one-shot writer for a conductor call, in the conductor's own folder (spec 131)."""
        return self._one_shot(run_id=run_id, folder=CONDUCTOR_FOLDER, card_id=card_id)

    def _one_shot(self, *, run_id: str, folder: str, card_id: str) -> RunTranscriptWriter:
        writer = RunTranscriptWriter(
            self._root,
            parts=(folder, sanitize_segment(card_id)),
            run_id=run_id,
            max_bytes=self._max_bytes_per_run,
        )
        writer.add_secret_values(self._ambient_secrets)
        return writer

    def add_ambient_secrets(self, environ: Mapping[str, str]) -> None:
        """Exact values of credential-named environment variables, for future writers."""
        self._ambient_secrets = secret_values_from_env(environ)

    def live_paths(self) -> frozenset[Path]:
        """Files of runs with a live writer. The retention sweep never removes these."""
        return frozenset(writer.path for writer in self._writers.values())

    def release(self, run_id: str) -> None:
        """Forget a finished run's writer. The file stays on disk."""
        writer = self._writers.pop(run_id, None)
        if writer is not None:
            # 134 review: usage the writer accepted but no poll has taken yet is counted
            # before the writer is dropped, so a run that ends mid-poll keeps its totals.
            taken = writer.drain_usage()
            if taken:
                symphony, card = writer.parts
                self.usage.observe(
                    run_id, symphony=symphony, card=card, stage=writer.stage, records=taken,
                )
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
    guard: RootGuard,
) -> TranscriptCollector:
    """Build the collector, refusing a root that overlaps a guarded directory.

    On conflict, transcription is switched off for this process and an error is
    logged. The daemon keeps running: transcripts are diagnostics, and refusing to
    start would take the orchestrator down over an optional setting. The error is
    logged at error level, so the condition is visible rather than silent.
    Construction touches nothing on disk.
    """
    conflicts = transcript_root_conflicts(root, guard) if enabled else []
    if conflicts:
        logger.error(
            "run_transcript.root_mounted_into_performer",
            root=str(root),
            conflicts=[str(c) for c in conflicts],
            action="transcripts disabled for this process; set transcripts.dir outside these paths",
        )
        enabled = False
    return TranscriptCollector(
        root,
        enabled=enabled,
        max_bytes_per_run=max_bytes_per_run,
        retention_days=retention_days,
    )


def _layout_dirs(parent: Path) -> list[Path]:
    """Immediate subdirectories whose names the writer could have created (the conductor's folder included)."""
    found: list[Path] = []
    for entry in parent.iterdir():
        if entry.is_symlink() or not entry.is_dir():
            continue
        if is_conductor_folder(entry.name) or sanitize_segment(entry.name) == entry.name:
            found.append(entry)
    return found


def _is_run_file(entry: Path) -> bool:
    """A file the writer could have named: ``<sanitized run id>.jsonl`` and nothing else."""
    if not entry.name.endswith(_TRANSCRIPT_SUFFIX):
        return False
    stem = entry.name[: -len(_TRANSCRIPT_SUFFIX)]
    return sanitize_segment(stem) == stem


#: Files modified more recently than this are never removed by the sweep. A run that
#: is still active after a daemon restart writes often, so its file is young (review
#: item 7). The live set covers the same case within this process.
MIN_RETENTION_AGE_SECONDS = 3600.0


def _remove_quietly(action: Callable[[], None], path: Path) -> bool:
    """Run one removal. A racing writer is normal, so ENOTEMPTY and ENOENT are ignored.

    Any other error is logged and skipped, so one bad entry never aborts the sweep.
    Returns True when the removal happened.
    """
    try:
        action()
    except OSError as exc:
        if exc.errno not in (errno.ENOTEMPTY, errno.ENOENT, errno.EEXIST):
            logger.warning(
                "run_transcript.retention_skipped",
                path=str(path),
                exc_type=type(exc).__name__,
            )
        return False
    return True


def _expired_run_file(entry: Path, keep_resolved: set[Path], older_than: float) -> bool:
    """Is ``entry`` one of the writer's own run files, not live, and old enough?

    Every filesystem call is inside the guard: an entry that cannot be read is
    skipped, never allowed to stop the sweep. One ``lstat`` answers "regular
    file, not a link" and the age together.
    """
    if not _is_run_file(entry):
        return False
    try:
        info = entry.lstat()
        if not stat.S_ISREG(info.st_mode) or entry.resolve() in keep_resolved:
            return False
    except OSError:
        return False
    return info.st_mtime < older_than


def prune_expired(
    root: Path,
    *,
    retention_days: int,
    keep: frozenset[Path] = frozenset(),
    now: datetime | None = None,
) -> int:
    """Delete run files older than ``retention_days``, within the writer's exact layout.

    Only ``<root>/<symphony>/<card>/<run>.jsonl`` is considered, at exactly that depth,
    and only names the writer could have produced (129 review, item 3). Symlinks are
    never followed or removed. A file in ``keep`` (a live writer's file) is never
    removed, and neither is a file modified within ``MIN_RETENTION_AGE_SECONDS``.
    Folders left empty are removed, but the root is not. A writer creating a folder
    at the same moment is not an error (review item 7). Returns the files deleted.
    """
    if retention_days <= 0 or not root.is_dir():
        return 0
    base = root.resolve()
    keep_resolved = {path.resolve() for path in keep}
    current = now or datetime.now(UTC)
    cutoff = (current - timedelta(days=retention_days)).timestamp()
    youngest_allowed = current.timestamp() - MIN_RETENTION_AGE_SECONDS
    removed = 0
    for symphony in _layout_dirs(base):
        for card in _layout_dirs(symphony):
            for entry in list(card.iterdir()):
                if _expired_run_file(entry, keep_resolved, min(cutoff, youngest_allowed)) and _remove_quietly(
                    entry.unlink, entry,
                ):
                    removed += 1
            if not any(card.iterdir()):
                _remove_quietly(card.rmdir, card)
        if symphony.exists() and not any(symphony.iterdir()):
            _remove_quietly(symphony.rmdir, symphony)
    logger.info("run_transcript.retention_pruned", removed=removed, retention_days=retention_days)
    return removed


__all__ = [
    "MAX_BATCH_BYTES",
    "MAX_RECORD_BYTES",
    "TRANSCRIPT_TYPES",
    "UNATTRIBUTED",
    "RootGuard",
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

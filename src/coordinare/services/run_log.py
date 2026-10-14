"""Full run logs: a performer run's complete stdout and stderr, kept after its container (spec 132).

Each containerized run gets one file under the run logs root::

    <run logs root>/<run_id>.log

The default root is ``<state directory>/run-logs``. It is NOT under
``performer_log_dir``: that directory is bind-mounted read-write into every
ephemeral performer, so a file the daemon writes there could be rewritten by the
container whose output it records. :func:`make_run_logs_collector` refuses a root
that overlaps a performer mount or a protected daemon directory, with the same rule
as the transcripts (129).

The service collects the log at teardown, before the container or Pod is removed,
by streaming the runtime's output into :class:`RunLogWriter` chunk by chunk. The
writer redacts, caps and commits the file. Collection is a side feature: the writer
never blocks its caller for longer than a write takes, and a collection cut short by
the time bound keeps what it wrote, marked as cut off.

Guarantees the writer provides:

* Bytes are decoded as UTF-8 with replacement. Text is redacted with the job's exact
  secret values and then the activity-feed redactor, before it is kept.
* A line too long to hold is cut only where no known secret straddles the cut, and
  the text after the cut is carried raw into the next window. A secret split by a
  cut is therefore redacted whole, never written in pieces.
* The file never exceeds ``max_bytes``. The first part and the last part of the log
  are kept, and one marker between them says how many bytes were dropped.
* The run path and the temporary path are created with ``O_EXCL`` and ``O_NOFOLLOW``.
  A symlink or hard link at either is refused, never written through, and an
  existing final file is never overwritten.
* Writes never raise into the caller. A failure marks the run's log failed, is logged
  once, and removes the partial file. ``abort`` never waits for a writer thread.
* Reads refuse links and anything that is not a single-name regular file, and pages
  never split a UTF-8 character.
"""

from __future__ import annotations

import codecs
import contextlib
import os
import stat
import threading
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from coordinare.lib.redaction import redact_command_text
from coordinare.lib.transcript_redaction import redact_text, secret_values_from_env
from coordinare.services.run_transcript import (
    _NOFOLLOW,
    _NONBLOCK,
    MIN_RETENTION_AGE_SECONDS,
    RootGuard,
    _refuse_unless_private_regular,
    _RefusedError,
    _write_all,
    sanitize_segment,
    transcript_root_conflicts,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping

__all__ = [
    "CUT_OFF_MARKER",
    "LOG_CHUNK_BYTES",
    "MARKER_RESERVE_BYTES",
    "MAX_LINE_CHARS",
    "SECRET_OVERLAP_CHARS",
    "RootGuard",
    "RunLogCollector",
    "RunLogRefusedError",
    "RunLogWindow",
    "RunLogWriter",
    "iter_run_log_file",
    "make_run_logs_collector",
    "open_run_log",
    "prune_run_logs",
    "read_run_log_window",
    "redact_log_text",
    "resolve_run_logs_root",
    "run_log_exists",
    "run_log_name",
    "run_log_path",
    "run_log_size",
]

logger = structlog.get_logger(__name__)

LOG_SUFFIX = ".log"
PARTIAL_SUFFIX = ".log.partial"
# Read and write granularity for the stream, so memory stays bounded.
LOG_CHUNK_BYTES = 64 * 1024
# A line longer than this is cut (see RunLogWriter._cut_long_pending), so a single huge
# line without a newline cannot grow the pending buffer without limit.
MAX_LINE_CHARS = 64 * 1024
# Room kept free for the truncation marker, so head, tail and markers stay within the cap.
MARKER_RESERVE_BYTES = 512
# The longest exact secret the writer keeps whole across a cut. A secret longer than
# this is not guaranteed to be redacted when a long line is cut (the job's secrets are
# tokens and keys, far shorter).
SECRET_OVERLAP_CHARS = 8 * 1024
# UTF-8 sequences are at most four bytes long. A continuation byte is 10xxxxxx; a lead
# byte says how long its character is (see _drop_partial_character).
UTF8_MAX_BYTES = 4
_CONTINUATION_MASK = 0xC0
_CONTINUATION = 0x80
_TWO_BYTE_LEAD = 0xE0
_THREE_BYTE_LEAD = 0xF0
CUT_OFF_MARKER = (
    "[collection cut off at the time bound; the rest of this log was not collected]\n"
)


def redact_log_text(text: str, secrets: Iterable[str] = ()) -> str:
    """One piece of log text, redacted: exact secret values, then the activity redactor.

    The same function redacts the full log and the live tail, so the two never disagree.
    """
    return redact_command_text(redact_text(text, tuple(secrets)))


def resolve_run_logs_root(run_logs_dir: Path | None, *, config_dir: Path, state_dir: Path) -> Path:
    """Where full run logs live. Pure: touches nothing on disk.

    ``run_logs.dir`` overrides the default. ``~`` is expanded, and a relative value is
    taken from the config directory. The default is ``<state directory>/run-logs``.
    """
    if run_logs_dir is None:
        return state_dir / "run-logs"
    base = Path(run_logs_dir).expanduser()
    return base if base.is_absolute() else config_dir / base


def run_log_name(run_id: str) -> str | None:
    """The file name for one run, or None when the id is not one safe path segment."""
    if not run_id or sanitize_segment(run_id) != run_id:
        return None
    return f"{run_id}{LOG_SUFFIX}"


def run_log_path(root: Path, run_id: str) -> Path | None:
    """The final path of one run's log, or None for an unsafe run id. Joined lexically."""
    name = run_log_name(run_id)
    return None if name is None else root / name


class RunLogRefusedError(Exception):
    """A run's log file is a link, a hard link, or not a plain regular file."""


@dataclass(frozen=True)
class RunLogWindow:
    """One page of a run's log. ``next_offset`` is None on the last page."""

    text: str
    size: int
    next_offset: int | None


def _marker(dropped: int, head: int, tail: int) -> bytes:
    text = (
        f"[truncated: {dropped} bytes dropped from the middle of this log; "
        f"kept the first {head} and the last {tail} bytes]\n"
    )
    return text.encode("utf-8")


class RunLogWriter:
    """Writes one run's log from a stream of byte chunks.

    Construction does no I/O. The file is created on the first write. Methods that
    touch the disk are blocking and are called on worker threads; ``abort`` and
    ``request_cut_off`` never block, so the event loop may call them at any time.
    Nothing here raises: a failure marks the writer failed and removes the partial.
    """

    def __init__(
        self,
        root: Path,
        *,
        run_id: str,
        max_bytes: int,
        secrets: Iterable[str] = (),
    ) -> None:
        self._root = root
        self._run_id = run_id
        self._valid_name = run_log_name(run_id) is not None
        stem = sanitize_segment(run_id)
        self._final = root / f"{stem}{LOG_SUFFIX}"
        self._partial = root / f"{stem}{PARTIAL_SUFFIX}"
        self._secrets: tuple[str, ...] = tuple(sorted({value for value in secrets if value}))
        self._head_budget = max(0, max_bytes - MARKER_RESERVE_BYTES) // 2
        self._tail_budget = max(0, max_bytes - MARKER_RESERVE_BYTES - self._head_budget)
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._pending = ""
        self._fd: int | None = None
        self._head = 0
        self._total = 0
        self._tail: deque[bytes] = deque()
        self._tail_bytes = 0
        self._in_tail = False
        self._dropped = 0
        self._failed = False
        self._finished = False
        self._aborted = False
        self._cut_off = False
        # Re-entrant: a failure inside feed or finish cleans up under the same lock.
        self._lock = threading.RLock()

    @property
    def path(self) -> Path:
        return self._final

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def failed(self) -> bool:
        return self._failed

    @property
    def cut_off(self) -> bool:
        return self._cut_off

    @property
    def truncated(self) -> bool:
        return self._dropped > 0

    @property
    def dropped_bytes(self) -> int:
        return self._dropped

    def feed(self, chunk: bytes) -> None:
        """Absorb one chunk of the runtime's output. Blocking; never raises."""
        with self._lock:
            if self._failed or self._finished or self._aborted or self._cut_off:
                self._reap()
                return
            try:
                self._absorb(self._decoder.decode(chunk))
            except Exception as exc:  # best effort: a failed write fails this run's log only
                self._fail(exc)
            self._reap()

    def finish(self) -> bool:
        """Flush, mark any cut, and commit the file. True when the final file now exists.

        Blocking; never raises.
        """
        with self._lock:
            return self._finish()

    def request_cut_off(self) -> None:
        """The time bound has passed: keep what was written and mark the log cut off.

        Non-blocking. The caller then runs ``finish`` on a worker thread; the marker is
        written at the end, after the content that was collected.
        """
        self._cut_off = True

    def abort(self) -> None:
        """Drop the partial file. Non-blocking, safe at any time and more than once.

        When a worker thread holds the writer (a write that has not returned), the abort
        is recorded and that thread removes the partial when it next releases the lock.
        Nothing here waits for the thread, so the event loop is never held by a disk.
        """
        self._aborted = True
        if self._lock.acquire(blocking=False):
            try:
                self._cleanup()
            finally:
                self._lock.release()

    def _finish(self) -> bool:
        if self._failed or self._finished or self._aborted:
            self._reap()
            return False
        try:
            self._absorb(self._decoder.decode(b"", final=True))
            if self._pending:
                self._emit(self._pending)
                self._pending = ""
            fd = self._ensure_open()
            kept_tail = self._tail_bytes
            dropped = self._total - self._head - kept_tail
            if dropped > 0:
                self._dropped = dropped
                _write_all(fd, _marker(dropped, self._head, kept_tail))
            for piece in self._tail:
                _write_all(fd, piece)
            self._tail.clear()
            self._tail_bytes = 0
            if self._cut_off:
                _write_all(fd, CUT_OFF_MARKER.encode("utf-8"))
            os.close(fd)
            self._fd = None
            self._partial.replace(self._final)
        except Exception as exc:  # best effort: see feed
            self._fail(exc)
            return False
        self._finished = True
        logger.info(
            "run_log.collected",
            run_id=self._run_id,
            total_bytes=self._total,
            dropped_bytes=self._dropped,
            cut_off=self._cut_off,
        )
        return True

    def _reap(self) -> None:
        """An abort that arrived while this thread held the writer takes effect now."""
        if self._aborted:
            self._cleanup()

    def _cleanup(self) -> None:
        """Close the descriptor and drop the partial. Caller holds the lock."""
        if self._fd is not None:
            with contextlib.suppress(OSError):
                os.close(self._fd)
            self._fd = None
        with contextlib.suppress(OSError):
            self._partial.unlink()

    def _fail(self, exc: BaseException) -> None:
        if self._failed:
            return
        self._failed = True
        logger.warning(
            "run_log.write_failed",
            run_id=self._run_id,
            path=str(self._final),
            exc_type=type(exc).__name__,
            error=str(exc),
        )
        self._cleanup()

    def _absorb(self, text: str) -> None:
        if not text:
            return
        parts = (self._pending + text).split("\n")
        self._pending = parts.pop()
        for part in parts:
            self._emit(part + "\n")
        if len(self._pending) > MAX_LINE_CHARS:
            self._cut_long_pending()

    def _cut_long_pending(self) -> None:
        """Emit the start of a line too long to hold, keeping the rest raw for later.

        The cut is never inside an exact secret, so each secret before it is complete and
        redacted whole. The text after the cut stays raw and is redacted with the next
        window, together with whatever arrives after it.
        """
        unfinished = self._pending
        cut = self._cut_point(unfinished)
        self._write_content(self._redact(unfinished[:cut]).encode("utf-8", errors="replace"))
        self._pending = unfinished[cut:]

    def _cut_point(self, unfinished: str) -> int:
        """A cut that no exact secret straddles, preferably at a whitespace boundary.

        Cuts at least ``SECRET_OVERLAP_CHARS`` from the end, so a secret still arriving
        starts after the cut. When no whitespace is near, the cut moves back to the start
        of any secret it would split.
        """
        length = len(unfinished)
        fallback = length - SECRET_OVERLAP_CHARS
        for cut in range(fallback, max(0, length - 2 * SECRET_OVERLAP_CHARS), -1):
            if unfinished[cut - 1].isspace() and self._straddle_start(unfinished, cut) is None:
                return cut
        cut = fallback
        while (start := self._straddle_start(unfinished, cut)) is not None:
            cut = start
        return cut

    def _straddle_start(self, unfinished: str, cut: int) -> int | None:
        """The earliest start of a known secret that begins before ``cut`` and ends after it."""
        for secret in self._secrets:
            first = max(0, cut - len(secret) + 1)
            for start in range(first, cut):
                if unfinished.startswith(secret, start) and start + len(secret) > cut:
                    return start
        return None

    def _redact(self, text: str) -> str:
        return redact_log_text(text, self._secrets)

    def _emit(self, line: str) -> None:
        self._write_content(self._redact(line).encode("utf-8", errors="replace"))

    def _write_content(self, data: bytes) -> None:
        """Keep the head as it comes; past the head budget, keep a bounded tail in memory."""
        self._total += len(data)
        if not self._in_tail and self._head + len(data) <= self._head_budget:
            _write_all(self._ensure_open(), data)
            self._head += len(data)
            return
        self._in_tail = True
        self._tail.append(data)
        self._tail_bytes += len(data)
        while self._tail and self._tail_bytes > self._tail_budget:
            self._tail_bytes -= len(self._tail.popleft())

    def _ensure_open(self) -> int:
        if self._fd is not None:
            return self._fd
        if not self._valid_name:
            message = f"run id is not a safe file name: {self._run_id!r}"
            raise _RefusedError(message)
        self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.path.lexists(self._final):
            # Never overwrite, and never open: the existing entry may be a link.
            message = f"run log already exists: {self._final}"
            raise _RefusedError(message)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _NONBLOCK
        self._fd = os.open(self._partial, flags, 0o600)
        return self._fd


class RunLogCollector:
    """Owns the run logs root and makes one writer per run.

    Constructing a collector touches nothing on disk.
    """

    def __init__(
        self,
        root: Path,
        *,
        enabled: bool,
        max_bytes: int,
        retention_days: int = 0,
    ) -> None:
        self._root = root.resolve()
        self._enabled = enabled
        self._max_bytes = max_bytes
        self._retention_days = retention_days
        self._ambient: tuple[str, ...] = ()

    @property
    def root(self) -> Path:
        return self._root

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def max_bytes(self) -> int:
        return self._max_bytes

    @property
    def retention_days(self) -> int:
        return self._retention_days

    @property
    def ambient_secrets(self) -> tuple[str, ...]:
        """Exact values of credential-named environment variables (the live tail uses them too)."""
        return self._ambient

    def add_ambient_secrets(self, environ: Mapping[str, str]) -> None:
        """Exact values of credential-named environment variables, for future writers."""
        self._ambient = tuple(secret_values_from_env(environ))

    def writer_for(self, *, run_id: str, secrets: Iterable[str] = ()) -> RunLogWriter:
        """A fresh writer for one run, redacting the job's secrets and the ambient ones."""
        return RunLogWriter(
            self._root,
            run_id=run_id,
            max_bytes=self._max_bytes,
            secrets=(*self._ambient, *secrets),
        )


def make_run_logs_collector(
    *,
    enabled: bool,
    root: Path,
    max_bytes: int,
    retention_days: int,
    guard: RootGuard,
) -> RunLogCollector:
    """Build the collector, refusing a root that overlaps a guarded directory.

    On conflict, run logs are switched off for this process and an error is logged.
    The daemon keeps running: run logs are diagnostics, and refusing to start would
    take the orchestrator down over an optional setting.
    """
    conflicts = transcript_root_conflicts(root, guard) if enabled else []
    if conflicts:
        logger.error(
            "run_log.root_mounted_into_performer",
            root=str(root),
            conflicts=[str(c) for c in conflicts],
            action="full run logs disabled for this process; set run_logs.dir outside these paths",
        )
        enabled = False
    return RunLogCollector(
        root,
        enabled=enabled,
        max_bytes=max_bytes,
        retention_days=retention_days,
    )


def open_run_log(root: Path, run_id: str) -> tuple[int, int] | None:
    """Open one run's log for reading: ``(fd, size)``, or None when it does not exist.

    Raises :class:`RunLogRefusedError` for a link, a hard link, or anything that is not
    a plain single-name regular file. The caller closes the descriptor.
    """
    path = run_log_path(root, run_id)
    if path is None:
        return None
    try:
        fd = os.open(path, os.O_RDONLY | _NOFOLLOW | _NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as exc:
        message = f"run log cannot be opened safely: {path}"
        raise RunLogRefusedError(message) from exc
    try:
        info = os.fstat(fd)
        _refuse_unless_private_regular(info, path)
    except _RefusedError as exc:
        os.close(fd)
        raise RunLogRefusedError(str(exc)) from exc
    return fd, info.st_size


def run_log_exists(root: Path, run_id: str) -> bool:
    """Whether a run's log has been committed: a plain, single-name regular file.

    Never opens the file, and never follows a link.
    """
    path = run_log_path(root, run_id)
    if path is None:
        return False
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and info.st_nlink == 1


def run_log_size(root: Path, run_id: str) -> int | None:
    """The size of a run's committed log, or None when there is none. Refuses links."""
    opened = open_run_log(root, run_id)
    if opened is None:
        return None
    fd, size = opened
    os.close(fd)
    return size


def _drop_partial_character(body: bytes) -> bytes:
    """Remove the bytes of a UTF-8 character that the page ends in the middle of."""
    for back in range(1, min(len(body), UTF8_MAX_BYTES) + 1):
        byte = body[-back]
        if byte & _CONTINUATION_MASK != _CONTINUATION:  # a lead byte (or ASCII)
            if byte < _CONTINUATION:
                need = 1
            elif byte < _TWO_BYTE_LEAD:
                need = 2
            elif byte < _THREE_BYTE_LEAD:
                need = 3
            else:
                need = 4
            return body[:-back] if need > back else body
    return body


def read_run_log_window(root: Path, run_id: str, *, offset: int, limit: int) -> RunLogWindow | None:
    """One page of a run's log, or None when there is no such log. Refuses links.

    A page never splits a character: a page that starts inside one begins after it, and
    a page that ends inside one stops before it. An offset at or past the end is an
    empty page.
    """
    opened = open_run_log(root, run_id)
    if opened is None:
        return None
    fd, size = opened
    try:
        if offset >= size:
            return RunLogWindow(text="", size=size, next_offset=None)
        raw = os.pread(fd, limit + UTF8_MAX_BYTES, offset)
    finally:
        os.close(fd)
    skip = 0
    if offset > 0:
        while skip < min(len(raw), UTF8_MAX_BYTES - 1) and raw[skip] & _CONTINUATION_MASK == _CONTINUATION:
            skip += 1
    body = raw[skip:skip + limit]
    end = offset + skip + len(body)
    if end < size:
        body = _drop_partial_character(body)
        end = offset + skip + len(body)
    return RunLogWindow(
        text=body.decode("utf-8", errors="replace"),
        size=size,
        next_offset=end if end < size else None,
    )


def iter_run_log_file(root: Path, run_id: str) -> Iterator[bytes]:
    """Stream a run's log in chunks. The file is opened only when iteration starts.

    A stream that is never started holds no descriptor, and one that is abandoned closes
    its descriptor when it is closed or collected.
    """
    opened = open_run_log(root, run_id)
    if opened is None:
        return
    fd, _size = opened
    try:
        while chunk := os.read(fd, LOG_CHUNK_BYTES):
            yield chunk
    finally:
        os.close(fd)


def _is_run_log_entry(name: str) -> bool:
    """A name the writer could have produced: ``<safe run id>.log`` or its partial."""
    if name.endswith(PARTIAL_SUFFIX):
        stem = name.removesuffix(PARTIAL_SUFFIX)
    elif name.endswith(LOG_SUFFIX):
        stem = name.removesuffix(LOG_SUFFIX)
    else:
        return False
    return bool(stem) and sanitize_segment(stem) == stem


def _expired(entry: Path, cutoff: float) -> bool:
    """Is ``entry`` a regular file (never a link) last modified before ``cutoff``?"""
    try:
        info = os.lstat(entry)
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and info.st_mtime < cutoff


def prune_run_logs(root: Path, *, retention_days: int, now: datetime | None = None) -> int:
    """Delete run logs older than ``retention_days``, direct children of the root only.

    Only names the writer could have produced (a log or its stale partial) are
    considered. Links are never followed or removed. A file modified within
    ``MIN_RETENTION_AGE_SECONDS`` is kept, so a run still writing is safe. Returns the
    number of files removed.
    """
    if retention_days <= 0 or not root.is_dir():
        return 0
    current = now or datetime.now(UTC)
    cutoff = min(
        (current - timedelta(days=retention_days)).timestamp(),
        current.timestamp() - MIN_RETENTION_AGE_SECONDS,
    )
    removed = 0
    for entry in list(root.iterdir()):
        if not _is_run_log_entry(entry.name) or not _expired(entry, cutoff):
            continue
        try:
            entry.unlink()
        except OSError:
            logger.warning("run_log.retention_skipped", path=str(entry))
            continue
        removed += 1
    logger.info("run_log.retention_pruned", removed=removed, retention_days=retention_days)
    return removed

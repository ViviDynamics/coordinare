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
writer decodes, redacts and caps it, then commits the file atomically.

Guarantees the writer provides:

* Bytes are decoded as UTF-8 with replacement, and each line is redacted with the
  job's exact secret values and then the activity-feed redactor, before it is kept.
* The file never exceeds ``max_bytes``. The first part and the last part of the log
  are kept, and one marker between them says how many bytes were dropped. Only the
  bytes in the middle are dropped, and only when some were.
* The run path and the temporary path are created with ``O_EXCL`` and ``O_NOFOLLOW``.
  A symlink or hard link at either is refused, never written through, and an
  existing final file is never overwritten.
* Writes never raise into the caller. A failure marks the run's log failed, is logged
  once, and removes the partial file.
* Reads refuse links and anything that is not a single-name regular file.
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
    "LOG_CHUNK_BYTES",
    "MARKER_RESERVE_BYTES",
    "MAX_LINE_CHARS",
    "RootGuard",
    "RunLogCollector",
    "RunLogRefusedError",
    "RunLogWindow",
    "RunLogWriter",
    "iter_run_log",
    "make_run_logs_collector",
    "open_run_log",
    "prune_run_logs",
    "read_run_log_window",
    "resolve_run_logs_root",
    "run_log_name",
    "run_log_path",
]

logger = structlog.get_logger(__name__)

LOG_SUFFIX = ".log"
PARTIAL_SUFFIX = ".log.partial"
# Read and write granularity for the stream, so memory stays bounded.
LOG_CHUNK_BYTES = 64 * 1024
# A line longer than this is cut, so a single huge line without a newline cannot
# grow the pending buffer without limit.
MAX_LINE_CHARS = 64 * 1024
# Room kept free for the truncation marker, so head, tail and marker stay within the cap.
MARKER_RESERVE_BYTES = 512


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

    Construction does no I/O. The file is created on the first write. Every method
    that touches the disk is blocking: the service calls them on a worker thread.
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
        # Re-entrant: a failure inside feed or finish aborts the partial under the same lock.
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
    def truncated(self) -> bool:
        return self._dropped > 0

    @property
    def dropped_bytes(self) -> int:
        return self._dropped

    def feed(self, chunk: bytes) -> None:
        """Absorb one chunk of the runtime's output. Blocking; never raises."""
        with self._lock:
            if self._failed or self._finished or self._aborted:
                return
            try:
                self._absorb(self._decoder.decode(chunk))
            except Exception as exc:  # best effort: a failed write fails this run's log only
                self._fail(exc)

    def finish(self) -> bool:
        """Flush, write the marker if content was dropped, and commit the file.

        Returns True when the final file now exists. Blocking; never raises.
        """
        with self._lock:
            return self._finish()

    def _finish(self) -> bool:
        if self._failed or self._finished or self._aborted:
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
        )
        return True

    def abort(self) -> None:
        """Drop the partial file. Safe to call at any time, and more than once.

        Once aborted, a late chunk from a worker thread is ignored, so it cannot
        recreate the partial file after the caller has removed it.
        """
        with self._lock:
            self._aborted = True
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
            path=str(self.path),
            exc_type=type(exc).__name__,
            error=str(exc),
        )
        self.abort()

    def _absorb(self, text: str) -> None:
        if not text:
            return
        parts = (self._pending + text).split("\n")
        self._pending = parts.pop()
        for part in parts:
            self._emit(part + "\n")
        if len(self._pending) > MAX_LINE_CHARS:
            self._emit(self._pending)
            self._pending = ""

    def _emit(self, line: str) -> None:
        redacted = redact_command_text(redact_text(line, self._secrets))
        self._write_content(redacted.encode("utf-8", errors="replace"))

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

    Raises :class:`RunLogRefusedError` for a link, a hard link, or anything that is not a
    plain single-name regular file. The caller closes the descriptor.
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


def read_run_log_window(root: Path, run_id: str, *, offset: int, limit: int) -> RunLogWindow | None:
    """One page of a run's log, or None when there is no such log. Refuses links."""
    opened = open_run_log(root, run_id)
    if opened is None:
        return None
    fd, size = opened
    try:
        data = os.pread(fd, limit, offset)
    finally:
        os.close(fd)
    end = offset + len(data)
    return RunLogWindow(
        text=data.decode("utf-8", errors="replace"),
        size=size,
        next_offset=end if end < size else None,
    )


def iter_run_log(fd: int) -> Iterator[bytes]:
    """Stream an open run log in chunks, then close the descriptor."""
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

    Only names the writer could have produced are considered (a log or its stale
    partial). Links are never followed or removed. A file modified within
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

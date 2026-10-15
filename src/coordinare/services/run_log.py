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
by streaming the runtime's output into :class:`RunLogWriter`. All run-log I/O runs on
:class:`RunLogIO`, a small bounded pool of its own, never on the default thread pool,
so a stalled read or write cannot starve the rest of the daemon. When every worker is
still stuck, new collections are skipped rather than queued.

Guarantees the writer provides:

* Text is redacted with the pattern set and the job's exact secret values before it
  is written. A line too long to hold is cut only where the redacted text of both
  halves equals the redaction of the whole, so no secret (pattern or exact) is split by
  a cut and written in pieces.
* The file never exceeds ``max_bytes``. The first part and the last part of the log
  are kept, and one marker between them says how many bytes were dropped. Only the
  head is redacted as it arrives; the tail is redacted once, when the log is finished.
* The run path and the temporary path are created with ``O_EXCL`` and ``O_NOFOLLOW``.
  A symlink or hard link at either is refused, never written through, and an
  existing final file is never overwritten.
* Writes never raise into the caller. A failure marks the run's log failed, is logged
  once, and removes the partial file. ``abort`` never waits for a worker thread, and
  an abandoned writer lets go of the job's secret values at once.
* Reads refuse links and anything that is not a single-name regular file, and pages
  never split a UTF-8 character.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import functools
import os
import stat
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

import structlog

from coordinare.lib.redaction import redact_client_forms, redact_secrets
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
    from collections.abc import Callable, Iterable, Iterator, Mapping
    from concurrent.futures import Future

_T = TypeVar("_T")

__all__ = [
    "CUT_OFF_MARKER",
    "LOG_CHUNK_BYTES",
    "MARKER_RESERVE_BYTES",
    "MAX_LINE_CHARS",
    "RUN_LOG_WORKERS",
    "SECRET_OVERLAP_CHARS",
    "RootGuard",
    "RunLogBusyError",
    "RunLogCollector",
    "RunLogIO",
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
# Read granularity for the stream, so memory stays bounded.
LOG_CHUNK_BYTES = 64 * 1024
# A line longer than this is considered for a cut (see RunLogWriter._cut_long_pending).
MAX_LINE_CHARS = 64 * 1024
# Above this, a line with no safe cut is redacted whole and emitted (the documented limit).
HARD_CAP_CHARS = 1024 * 1024
# How far back from the end the cut is searched, in steps of CUT_STEP_CHARS.
CUT_STEP_CHARS = 256
# Room kept free for the truncation marker, so head, tail and markers stay within the cap.
MARKER_RESERVE_BYTES = 512
# The longest secret, pattern or exact, that a cut is guaranteed not to split. Matches
# are bounded by this: a longer one is the documented limit of the cut.
SECRET_OVERLAP_CHARS = 8 * 1024
# UTF-8 sequences are at most four bytes long. A continuation byte is 10xxxxxx; a lead
# byte says how long its character is (see _drop_partial_character).
UTF8_MAX_BYTES = 4
_CONTINUATION_MASK = 0xC0
_CONTINUATION = 0x80
_TWO_BYTE_LEAD = 0xE0
_THREE_BYTE_LEAD = 0xF0
# The run-log I/O pool: a few threads, owned by the collector, never the default pool.
RUN_LOG_WORKERS = 4
# How many committed runs the collector remembers (the snapshot never asks the disk).
COMMITTED_MEMORY = 4096
CUT_OFF_MARKER = (
    "[collection cut off at the time bound; the rest of this log was not collected]\n"
)


def redact_log_text(text: str, secrets: Iterable[str] = ()) -> str:
    """One piece of log text, redacted: the pattern set and exact secret values.

    The same function redacts the full log and the live tail, so the two never disagree.
    """
    # The transcript redactor runs once, with the exact values; redact_command_text would run
    # it a second time over the same text, which costs a third of the time for no change.
    return redact_client_forms(redact_secrets(redact_text(text, tuple(secrets))))


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


class RunLogBusyError(Exception):
    """Every run-log I/O worker is still busy, so the operation is not queued."""


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


def _split_lines(text: str) -> list[str]:
    """``text`` as lines that keep their newline; a final unterminated line is kept too."""
    parts = text.split("\n")
    lines = [part + "\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


class RunLogIO:
    """A small bounded pool for run-log I/O only, owned by the collector.

    A submitted call that outlives its caller (a stalled disk, a silent socket) keeps its
    worker until it returns. Once every worker is in that state, ``submit`` refuses new
    work with :class:`RunLogBusyError` instead of queueing it, so run logs can never fill
    the daemon's threads. Nothing here blocks the event loop.
    """

    def __init__(self, workers: int = RUN_LOG_WORKERS) -> None:
        self._workers = workers
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="run-log")
        self._in_flight = 0
        self._lock = threading.Lock()
        self._saturation_logged = False

    @property
    def workers(self) -> int:
        return self._workers

    def submit(self, fn: Callable[..., _T], *args: Any) -> Future[_T]:
        """Run ``fn`` on a run-log worker. Raises :class:`RunLogBusyError` when saturated."""
        with self._lock:
            if self._in_flight >= self._workers:
                if not self._saturation_logged:
                    self._saturation_logged = True
                    logger.warning(
                        "run_log.io_saturated",
                        workers=self._workers,
                        detail="every run-log worker is stuck; new collections are skipped",
                    )
                message = "run-log workers are all busy"
                raise RunLogBusyError(message)
            self._in_flight += 1
        try:
            future = self._executor.submit(fn, *args)
        except RuntimeError as exc:  # the pool is shut down
            with self._lock:
                self._in_flight -= 1
            message = "run-log I/O is shut down"
            raise RunLogBusyError(message) from exc
        future.add_done_callback(self._returned)
        return future

    def _returned(self, _future: Future[Any]) -> None:
        with self._lock:
            self._in_flight -= 1
            if self._in_flight < self._workers:
                self._saturation_logged = False

    async def run(self, fn: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
        """Await ``fn(*args, **kwargs)`` on a run-log worker."""
        return await asyncio.wrap_future(self.submit(functools.partial(fn, *args, **kwargs)))


class RunLogWriter:
    """Writes one run's log from a stream of byte chunks.

    Construction does no I/O. The file is created on the first write. Methods that touch
    the disk are blocking and run on :class:`RunLogIO` workers; ``abort`` and
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
        self._pending = ""  # the line in progress, raw
        self._discarding = False  # tail mode: a runaway line is skipped to its newline
        self._fd: int | None = None
        self._head = 0  # redacted bytes written to the file
        self._head_raw = 0  # the raw bytes those head bytes stand for
        self._total = 0  # raw bytes received
        self._in_tail = False
        self._raw_tail: deque[bytes] = deque()  # raw lines kept for the tail, redacted at finish
        self._raw_tail_bytes = 0
        self._dropped = 0
        self._failed = False
        self._finished = False
        self._aborted = False
        self._cut_off = False
        self._cut_retry_at = 0
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

    @property
    def holds_secrets(self) -> bool:
        """Whether the writer still holds the job's secret values (it lets go of them when done)."""
        return bool(self._secrets)

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
        """Redact the tail, mark any cut, and commit the file. True when the final file exists.

        Blocking; never raises.
        """
        with self._lock:
            return self._finish()

    def request_cut_off(self) -> None:
        """The time bound has passed: keep what was written and mark the log cut off.

        Non-blocking. The caller then runs ``finish`` on a worker; the marker goes at the
        end, after the content that was collected.
        """
        self._cut_off = True

    def abort(self) -> None:
        """Drop the partial file and the secret values. Non-blocking, safe at any time.

        When a worker holds the writer (a write that has not returned), the abort is
        recorded and that worker removes the partial when it next releases the lock.
        """
        self._aborted = True
        self._secrets = ()
        if self._lock.acquire(blocking=False):
            try:
                self._reap()
            finally:
                self._lock.release()

    # --- ingestion -----------------------------------------------------------------------

    def _absorb(self, text: str) -> None:
        if not text:
            return
        self._total += len(text.encode("utf-8", errors="replace"))
        if self._in_tail:
            self._absorb_tail(text)
        else:
            self._absorb_head(text)

    def _absorb_head(self, text: str) -> None:
        parts = (self._pending + text).split("\n")
        self._pending = parts.pop()
        if parts:
            self._place_block("".join(part + "\n" for part in parts))
        if (
            not self._in_tail
            and len(self._pending) > MAX_LINE_CHARS
            and len(self._pending) >= self._cut_retry_at
        ):
            self._cut_long_pending()

    def _absorb_tail(self, text: str) -> None:
        if self._discarding:
            newline = text.find("\n")
            if newline == -1:
                return
            text = text[newline + 1:]
            self._discarding = False
        parts = (self._pending + text).split("\n")
        self._pending = parts.pop()
        for part in parts:
            self._keep_raw(part + "\n")
        if len(self._pending) > self._tail_budget:
            # A line that cannot be kept whole in the tail: skip it up to its newline.
            self._pending = ""
            self._discarding = True

    # --- placement -----------------------------------------------------------------------

    def _place_block(self, raw: str) -> None:
        """Write a block of complete lines as one redaction pass, if it fits the head."""
        redacted = self._redact(raw)
        if not self._in_tail and self._head + len(redacted.encode("utf-8", "replace")) <= self._head_budget:
            self._place(raw, redacted)
            return
        for line in _split_lines(raw):
            self._place(line, self._redact(line))

    def _place(self, raw: str, redacted: str) -> None:
        """Write redacted text to the head while it fits; past the head, keep the raw text."""
        data = redacted.encode("utf-8", errors="replace")
        if not self._in_tail and self._head + len(data) <= self._head_budget:
            self._write_head(data)
            self._head += len(data)
            self._head_raw += len(raw.encode("utf-8", errors="replace"))
            return
        self._in_tail = True
        self._keep_raw(raw)

    def _keep_raw(self, raw: str) -> None:
        """Keep raw text for the tail, dropping whole old pieces past the tail budget."""
        piece = raw.encode("utf-8", errors="replace")
        self._raw_tail.append(piece)
        self._raw_tail_bytes += len(piece)
        while self._raw_tail and self._raw_tail_bytes > self._tail_budget:
            self._raw_tail_bytes -= len(self._raw_tail.popleft())

    def _cut_long_pending(self) -> None:
        """Place the start of a line too long to hold; keep the rest raw for later.

        The window is redacted whole first. A cut is accepted only when redacting the two
        halves separately gives the same text as redacting the whole: then no secret, of
        any pattern, straddles the cut. A line with no such cut is kept until it has a
        cut or reaches the hard cap, where it is redacted whole and emitted.
        """
        window = self._pending
        whole = self._redact(window)
        found = self._safe_cut(window, whole)
        if found is None:
            if len(window) >= HARD_CAP_CHARS:
                self._place(window, whole)
                self._pending = ""
                self._cut_retry_at = 0
            else:
                self._cut_retry_at = len(window) + MAX_LINE_CHARS
            return
        cut, prefix = found
        self._place(window[:cut], prefix)
        self._pending = window[cut:]
        self._cut_retry_at = 0

    def _safe_cut(self, pending: str, whole: str) -> tuple[int, str] | None:
        """The cut nearest the end that no secret straddles, with the redacted prefix."""
        for keep in range(SECRET_OVERLAP_CHARS, 2 * SECRET_OVERLAP_CHARS, CUT_STEP_CHARS):
            cut = len(pending) - keep
            if cut <= 0:
                break
            prefix = self._redact(pending[:cut])
            if prefix + self._redact(pending[cut:]) == whole:
                return cut, prefix
        return None

    def _redact(self, text: str) -> str:
        return redact_log_text(text, self._secrets)

    # --- finishing -----------------------------------------------------------------------

    def _finish(self) -> bool:
        if self._failed or self._finished or self._aborted:
            self._reap()
            return False
        try:
            self._absorb(self._decoder.decode(b"", final=True))
            if self._pending:
                if self._in_tail:
                    if not self._discarding:
                        self._keep_raw(self._pending)
                else:
                    self._place_block(self._pending)
                self._pending = ""
            fd = self._ensure_open()
            if self._in_tail:
                self._write_tail(fd)
            if self._cut_off:
                _write_all(fd, CUT_OFF_MARKER.encode("utf-8"))
            os.close(fd)
            self._fd = None
            self._partial.replace(self._final)
        except Exception as exc:  # best effort: see feed
            self._fail(exc)
            return False
        self._finished = True
        self._secrets = ()
        logger.info(
            "run_log.collected",
            run_id=self._run_id,
            total_bytes=self._total,
            dropped_bytes=self._dropped,
            cut_off=self._cut_off,
        )
        return True

    def _write_tail(self, fd: int) -> None:
        """Redact the kept raw tail once, then write it with the marker for what was dropped.

        The kept tail is redacted as one block, so the cost is bounded by the tail budget.
        Redaction can lengthen a short secret, so whole redacted lines are trimmed from the
        front if the result is over budget; that trim is counted at its redacted size.
        """
        kept_raw = self._raw_tail_bytes
        block = b"".join(self._raw_tail).decode("utf-8", errors="replace")
        self._raw_tail.clear()
        self._raw_tail_bytes = 0
        data = self._redact(block).encode("utf-8", errors="replace")
        if len(data) > self._tail_budget:
            pieces = [line.encode("utf-8", errors="replace") for line in _split_lines(data.decode("utf-8"))]
            size = len(data)
            first = 0
            while first < len(pieces) and size > self._tail_budget:
                size -= len(pieces[first])
                kept_raw -= len(pieces[first])
                first += 1
            data = b"".join(pieces[first:])
        self._dropped = max(0, self._total - self._head_raw - kept_raw)
        if self._dropped > 0:
            _write_all(fd, _marker(self._dropped, self._head, len(data)))
        _write_all(fd, data)

    # --- files ---------------------------------------------------------------------------

    def _write_head(self, data: bytes) -> None:
        if self._aborted:
            return  # an abandoned writer never creates or extends a file
        _write_all(self._ensure_open(), data)

    def _reap(self) -> None:
        """An abort that arrived while this thread held the writer takes effect now."""
        if self._aborted:
            self._pending = ""
            self._raw_tail.clear()
            self._raw_tail_bytes = 0
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
        self._secrets = ()
        logger.warning(
            "run_log.write_failed",
            run_id=self._run_id,
            path=str(self._final),
            exc_type=type(exc).__name__,
            error=str(exc),
        )
        self._cleanup()

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
    """Owns the run logs root, the run-log I/O pool and the record of committed runs.

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
        self._io = RunLogIO()
        self._committed: dict[str, None] = {}
        self._committed_lock = threading.Lock()

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

    @property
    def io(self) -> RunLogIO:
        return self._io

    def add_ambient_secrets(self, environ: Mapping[str, str]) -> None:
        """Exact values of credential-named environment variables, for future writers."""
        self._ambient = tuple(secret_values_from_env(environ))

    def mark_committed(self, run_id: str) -> None:
        """Remember that a run's log is committed. Thread-safe; bounded."""
        with self._committed_lock:
            self._committed[run_id] = None
            while len(self._committed) > COMMITTED_MEMORY:
                del self._committed[next(iter(self._committed))]

    def is_committed(self, run_id: str) -> bool:
        """Whether this daemon committed the run's log. Never touches the disk."""
        with self._committed_lock:
            return run_id in self._committed

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
    """Whether a run's log is committed: a plain, single-name regular file. Never opens it."""
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
    """Stream a run's log in chunks, opening the file once when iteration starts.

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


def _expired_at(dir_fd: int, name: str, cutoff: float) -> bool:
    """Is *name* in *dir_fd* a regular file (never a link) last modified before *cutoff*?"""
    try:
        info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and info.st_mtime < cutoff


def prune_run_logs(root: Path, *, retention_days: int, now: datetime | None = None) -> int:
    """Delete run logs older than ``retention_days``, direct children of the root only.

    Only names the writer could have produced (a log or its stale partial) are
    considered. Links are never followed or removed, and a link at the root itself is refused
    (220): the root is opened with ``O_NOFOLLOW`` and every entry is checked and removed by name
    relative to that descriptor. A file modified within ``MIN_RETENTION_AGE_SECONDS`` is kept,
    so a run still writing is safe. Returns the number of files removed.
    """
    if retention_days <= 0:
        return 0
    try:
        dir_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        return 0
    current = now or datetime.now(UTC)
    cutoff = min(
        (current - timedelta(days=retention_days)).timestamp(),
        current.timestamp() - MIN_RETENTION_AGE_SECONDS,
    )
    removed = 0
    try:
        with os.scandir(dir_fd) as entries:
            names = [entry.name for entry in entries]
        for name in names:
            if not _is_run_log_entry(name) or not _expired_at(dir_fd, name, cutoff):
                continue
            try:
                os.unlink(name, dir_fd=dir_fd)
            except OSError:
                logger.warning("run_log.retention_skipped", path=str(root / name))
                continue
            removed += 1
    finally:
        os.close(dir_fd)
    logger.info("run_log.retention_pruned", removed=removed, retention_days=retention_days)
    return removed

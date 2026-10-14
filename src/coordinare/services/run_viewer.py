"""The read side of run transcripts, for the dashboard transcript viewer (spec 133).

The writer in :mod:`coordinare.services.run_transcript` owns the layout. This module
only reads it, through the same functions (:func:`transcript_path`,
:func:`sanitize_segment`), so a reader and the writer cannot disagree about where a
run lives.

Rules this module keeps:

* Every path segment must already be in sanitized form. A segment the writer would
  have rewritten is not a run address, so it is a lookup error, not a file to open.
* Every folder and the file below the root are checked with ``lstat``. A symlink or a
  file with more than one name is refused, as the writer refuses it.
* A file is read line by line and never loaded whole. The header is the first line.
* A line that does not parse is returned as an unreadable record with its line
  number. It is never dropped.
* Long values are previewed and flagged. :func:`full_field` returns one in full.

Nothing here writes. Nothing here raises past its callers except the two lookup
exceptions, which the router maps to 404 and 403.
"""

from __future__ import annotations

import errno
import json
import os
import re
import stat
import threading
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, NoReturn

import structlog

from coordinare.lib.path_segments import is_conductor_folder
from coordinare.services.run_header import RUN_HEADER_MAX_BYTES, RUN_HEADER_TYPE
from coordinare.services.run_transcript import (
    TRANSCRIPT_TYPES,
    sanitize_segment,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator
    from pathlib import Path
    from typing import BinaryIO

logger = structlog.get_logger(__name__)

PAGE_DEFAULT_LIMIT = 200
PAGE_MAX_LIMIT = 500
SEARCH_DEFAULT_LIMIT = 50
SEARCH_MAX_LIMIT = 200
LIST_MAX_RUNS = 200
# A preview is the first this many characters of a long value. The full value is
# one request away (:func:`full_field`), so nothing is silently cut.
PREVIEW_CHARS = 2000
# A line that does not parse is shown only this far. Its full bytes stay on disk.
UNREADABLE_PREVIEW_CHARS = 512
OUTCOME_PREVIEW_CHARS = 300
# A tool call's one-line argument, e.g. the command for Bash. Clipped like the feed's summary.
TOOL_ARG_CHARS = 120
SUMMARY_CACHE_SIZE = 32
DOWNLOAD_CHUNK_BYTES = 64 * 1024
# Markdown is yielded in chunks of about this size. One chunk is one thread-pool hop when the
# app streams it, so a per-record yield made a 20k-record download take seconds.
MARKDOWN_CHUNK_CHARS = 64 * 1024
# Search stops after scanning this many bytes and hands back a cursor. Searches hold a slot
# of SEARCH_CONCURRENCY, so a few large runs cannot starve the others.
SEARCH_BYTE_BUDGET = 16 * 1024 * 1024
SEARCH_CONCURRENCY = 4
# A performer event's time and its transcript record's time come from the same process
# clock, but the record can be stamped a moment before the event object. The link allows
# this much slack when it looks for the first record at or after an event.
EVENT_TIME_SLACK_SECONDS = 0.25

FULL_FIELDS = ("text", "input", "output")
ERROR_TYPES = frozenset({"error"})
UNREADABLE_TYPE = "unreadable"
TOOL_CALL_TYPE = "tool_call"
TOOL_RESULT_TYPE = "tool_result"
RESULT_TYPE = "result"
USAGE_TYPE = "usage"
TRUNCATED_TYPE = "truncated"
USAGE_FIELDS = ("input_tokens", "output_tokens", "cache_read", "cache_write")
NO_COST_REASON = (
    "the transcript records no cost; the cost ledger (#134) is not on main, so no cost is shown"
)
NO_USAGE_REASON = "no usage records in this transcript"
NO_OUTCOME_REASON = "no result record in this transcript (the run may still be going, or was cut off)"
NO_HEADER_REASON = "the first line is not a run header (an older run, or one whose header was not written)"

# The argument keys a tool call is summarized by, in order of preference.
_TOOL_ARG_KEYS = ("command", "file_path", "path", "pattern", "url", "query", "description")
_EXIT_CODE_TEXT = re.compile(r"exit(?: code)?[ :=]+(-?\d+)", re.IGNORECASE)
_PR_URL = re.compile(r"https://github\.com/[^\s/]+/[^\s/]+/pull/\d+")
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_TRANSCRIPT_SUFFIX = ".jsonl"


class RunLookupError(Exception):
    """The run is not there, or its address is not a valid one. Maps to 404."""


class RunRefusedError(Exception):
    """A link, a folder, or a file with several names is on the path. Maps to 403."""


@dataclass(frozen=True)
class RunRef:
    symphony: str
    card: str
    run: str


@dataclass(frozen=True)
class RecordFilter:
    """The type and error filters a page or a search applies. Empty types and no errors keep everything."""

    types: frozenset[str] = frozenset()
    errors: bool = False


@dataclass(frozen=True)
class RecordPage:
    records: list[dict[str, Any]]
    next_after: int | None
    focus_line: int | None
    # Set when a search stopped at its byte budget (the byte offset it stopped at).
    stopped_at_byte: int | None = None
    # Set by a backward page: the line to start the next page back from.
    prev_before: int | None = None


@dataclass
class _Summary:
    """What one full scan of a run learns. Cached by file identity."""

    record_count: int = 0
    unreadable_count: int = 0
    error_count: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    first_ts: str | None = None
    last_ts: str | None = None
    usage_message: dict[str, int] = field(default_factory=dict)
    usage_run: dict[str, int] | None = None
    result_is_error: bool | None = None
    result_text: str | None = None
    pr_url: str | None = None
    truncated: bool = False


_summary_cache: OrderedDict[tuple[str, int, int, int, int], _Summary] = OrderedDict()
# Requests run on worker threads, so the cache is guarded.
_summary_lock = threading.Lock()
_SEARCH_SLOTS = threading.BoundedSemaphore(SEARCH_CONCURRENCY)


# --------------------------------------------------------------------------- #
# Addressing and safe opening
# --------------------------------------------------------------------------- #


def _valid_segment(value: str) -> bool:
    """Only the writer's own sanitized form is an address. ``sanitize_segment("")`` is not empty."""
    return bool(value) and sanitize_segment(value) == value


def _valid_symphony_segment(value: str) -> bool:
    """A symphony address: the sanitized form, or the conductor's own folder (spec 131).

    The conductor's folder is the one admitted name that sanitize_segment never produces, so
    the address cannot name a symphony's runs.
    """
    return is_conductor_folder(value) or _valid_segment(value)


def run_file(root: Path, ref: RunRef) -> Path:
    """The run's file under ``root``, after every segment and every folder on the way is checked."""
    if not _valid_symphony_segment(ref.symphony):
        message = f"not a run address: {ref.symphony!r}"
        raise RunLookupError(message)
    for segment in (ref.card, ref.run):
        if not _valid_segment(segment):
            message = f"not a run address: {segment!r}"
            raise RunLookupError(message)
    # The symphony folder is already a valid folder name here, so it is joined as it is.
    path = root / ref.symphony / ref.card / f"{ref.run}{_TRANSCRIPT_SUFFIX}"
    current = root
    for part in path.relative_to(root).parts:
        current = current / part
        try:
            info = os.lstat(current)
        except FileNotFoundError as exc:
            message = f"no transcript for run {ref.run!r}"
            raise RunLookupError(message) from exc
        if stat.S_ISLNK(info.st_mode):
            _refuse(ref, f"a link is on the path: {part}")
        if current != path and not stat.S_ISDIR(info.st_mode):
            _refuse(ref, f"not a folder: {part}")
    return path


def _refuse(ref: RunRef, reason: str) -> NoReturn:
    logger.warning("run_viewer.refused", run=ref.run, card=ref.card, symphony=ref.symphony, reason=reason)
    raise RunRefusedError(reason)


def open_run(path: Path, ref: RunRef) -> BinaryIO:
    """Open the run file for reading. Refuses a link, a non-file, or a file with several names.

    The open itself does not follow a link (``O_NOFOLLOW``) and does not block on a FIFO
    (``O_NONBLOCK``). The checks run on the open descriptor, so they describe the file
    that is read.
    """
    try:
        fd = os.open(path, os.O_RDONLY | _NOFOLLOW | _NONBLOCK)
    except FileNotFoundError as exc:
        message = f"no transcript for run {ref.run!r}"
        raise RunLookupError(message) from exc
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            _refuse(ref, "the file is a link")
        raise
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            _refuse(ref, "the transcript is not a regular file")
        if info.st_nlink != 1:
            _refuse(ref, f"the transcript has {info.st_nlink} hard links")
    except BaseException:
        os.close(fd)
        raise
    return os.fdopen(fd, "rb")


# --------------------------------------------------------------------------- #
# Parsing and previews
# --------------------------------------------------------------------------- #


def _parse(raw: bytes) -> dict[str, Any] | None:
    try:
        parsed: object = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _as_text(value: object) -> str:
    """A value as the text the viewer shows. Strings stay as they are; anything else is JSON."""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _preview_fields(parsed: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name in FULL_FIELDS:
        if parsed.get(name) is None:
            continue
        text = _as_text(parsed[name])
        out[name] = text[:PREVIEW_CHARS]
        out[f"{name}_truncated"] = len(text) > PREVIEW_CHARS
        out[f"{name}_bytes"] = len(text.encode("utf-8", "surrogatepass"))
        out[f"{name}_format"] = "text" if isinstance(parsed[name], str) else "json"
    return out


def _unreadable_view(line: int, raw: bytes) -> dict[str, Any]:
    text = raw.decode("utf-8", "replace").rstrip("\n")
    return {
        "line": line,
        "seq": None,
        "type": UNREADABLE_TYPE,
        "unreadable": True,
        "reason": "this line is not a JSON object",
        "bytes": len(raw),
        "preview": text[:UNREADABLE_PREVIEW_CHARS],
        "preview_truncated": len(text) > UNREADABLE_PREVIEW_CHARS,
    }


def _record_view(line: int, raw: bytes, parsed: dict[str, Any]) -> dict[str, Any]:
    seq = parsed.get("seq")
    view: dict[str, Any] = {
        "line": line,
        "seq": seq if isinstance(seq, int) and not isinstance(seq, bool) else None,
        "type": str(parsed.get("type") or ""),
        "unreadable": False,
        "ts": parsed.get("ts"),
        "run_id": parsed.get("run_id"),
        "tool_name": parsed.get("tool_name"),
        "call_id": parsed.get("call_id"),
        "is_error": parsed.get("is_error"),
        "scope": parsed.get("scope"),
        "usage": parsed.get("usage"),
    }
    view.update(_preview_fields(parsed))
    if view["type"] == RUN_HEADER_TYPE:
        view["header"] = True
    if view["type"] == TOOL_CALL_TYPE:
        view["arg"] = _tool_arg(parsed.get("input"))
    if view["type"] == TOOL_RESULT_TYPE:
        view["exit_code"] = _exit_code(parsed.get("output"))
    return view


def _tool_arg(value: object) -> str:
    """The one line a tool call is named by: its command, path or pattern, clipped."""
    text = ""
    if isinstance(value, dict):
        for key in _TOOL_ARG_KEYS:
            if isinstance(value.get(key), str) and value[key]:
                text = str(value[key])
                break
    elif isinstance(value, str):
        text = value
    text = " ".join(text.split())
    return text if len(text) <= TOOL_ARG_CHARS else f"{text[:TOOL_ARG_CHARS]}..."


def _exit_code(value: object) -> int | None:
    """The exit status of a tool result: a structured field first, then a line of its text."""
    if isinstance(value, dict):
        for key in ("exit_code", "returncode", "exit_status"):
            found = value.get(key)
            if isinstance(found, int) and not isinstance(found, bool):
                return found
    found_text = _EXIT_CODE_TEXT.search(_as_text(value))
    return int(found_text.group(1)) if found_text is not None else None


def _is_error(parsed: dict[str, Any]) -> bool:
    return str(parsed.get("type") or "") in ERROR_TYPES or parsed.get("is_error") is True


def _matches_types(parsed: dict[str, Any], types: frozenset[str], errors: bool) -> bool:
    """The filter. ``errors`` narrows to failures; ``types`` narrows to the named types. Both apply."""
    if errors and not _is_error(parsed):
        return False
    return not types or str(parsed.get("type") or "") in types


def _is_event(parsed: dict[str, Any]) -> bool:
    """The header is the run's inputs, not an event, so pages and searches never return it."""
    return parsed.get("type") != RUN_HEADER_TYPE


# --------------------------------------------------------------------------- #
# Header, listing and lookup
# --------------------------------------------------------------------------- #


def read_header(handle: BinaryIO) -> dict[str, Any] | None:
    """The header record, from the first line only. None when the first line is not one."""
    handle.seek(0)
    raw = handle.readline(RUN_HEADER_MAX_BYTES + 1)
    parsed = _parse(raw)
    if parsed is None or parsed.get("type") != RUN_HEADER_TYPE:
        return None
    return parsed


def _header_of(path: Path, ref: RunRef) -> dict[str, Any] | None:
    with open_run(path, ref) as handle:
        return read_header(handle)


def _section(record: dict[str, Any], key: str) -> dict[str, Any]:
    """A nested object of a record, or an empty one when it is absent or not an object."""
    value = record.get(key)
    return value if isinstance(value, dict) else {}


def _header_summary(header: dict[str, Any] | None) -> dict[str, Any]:
    """The fields the viewer's header and the run lists show, from the header record."""
    if header is None:
        return {"available": False, "reason": NO_HEADER_REASON}
    persona = _section(header, "persona")
    performer = _section(header, "performer")
    model = _section(header, "model")
    card = _section(header, "card")
    # 131: a conductor call's stage is its kind, and its decision is shown in the run lists.
    conductor = _section(header, "conductor")
    return {
        "available": True,
        "started_at": header.get("ts"),
        "stage": f"conductor:{conductor['kind']}" if conductor.get("kind") else persona.get("name"),
        "conductor": {
            "kind": conductor.get("kind"),
            "decision": conductor.get("decision"),
            "symphony": conductor.get("symphony"),
        } if conductor else None,
        "agent": performer.get("backend"),
        "transport": performer.get("transport"),
        "endpoint": performer.get("endpoint"),
        "model": model.get("model"),
        "persona": {
            "name": persona.get("name"),
            "version": persona.get("version"),
            "version_sha256": persona.get("version_sha256"),
            "changed_at": persona.get("changed_at"),
            "changed_by": persona.get("changed_by"),
            "reason": persona.get("reason"),
        },
        "card": {
            "id": card.get("id"),
            "issue_number": card.get("issue_number"),
            "title": card.get("title"),
        },
    }


def list_runs(root: Path, *, symphony: str | None, card: str) -> dict[str, Any]:
    """The runs for one card, under one symphony or under every symphony, newest first.

    Only folder names the writer could have created are read, and no link is followed.
    Each run costs one header read and one ``stat``, so a listing never scans a transcript.
    """
    if not _valid_segment(card) or (symphony is not None and not _valid_symphony_segment(symphony)):
        message = "not a card address"
        raise RunLookupError(message)
    # Stat every file first (cheap), keep the newest LIST_MAX_RUNS, and read headers only for those.
    candidates: list[tuple[float, RunRef, Path, os.stat_result]] = []
    for sym, folder in _card_folders(root, card, symphony):
        for entry in _run_files(folder):
            run = entry.name[: -len(_TRANSCRIPT_SUFFIX)]
            ref = RunRef(symphony=sym, card=card, run=run)
            try:
                path = run_file(root, ref)
                info = path.stat()
            except (RunLookupError, RunRefusedError, OSError):
                continue
            candidates.append((info.st_mtime, ref, path, info))
    candidates.sort(key=lambda c: c[0], reverse=True)
    rows: list[dict[str, Any]] = []
    for mtime, ref, path, info in candidates[:LIST_MAX_RUNS]:
        try:
            header = _header_of(path, ref)
        except (RunLookupError, RunRefusedError, OSError):
            continue
        rows.append({
            "symphony": ref.symphony,
            "card": card,
            "run": ref.run,
            "bytes": info.st_size,
            "modified_at": datetime.fromtimestamp(mtime, UTC).isoformat(),
            **_header_summary(header),
        })
    return {"runs": rows, "truncated": len(candidates) > LIST_MAX_RUNS}


def _symphony_folders(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    folders: list[Path] = []
    for entry in sorted(root.iterdir(), key=lambda p: p.name):
        if entry.is_symlink() or not entry.is_dir() or not _valid_symphony_segment(entry.name):
            continue
        folders.append(entry)
    return folders


def _card_folders(root: Path, card: str, symphony: str | None) -> list[tuple[str, Path]]:
    """The ``(symphony, folder)`` pairs that hold runs for ``card``, without following links."""
    symphonies = [root / symphony] if symphony is not None else _symphony_folders(root)
    found: list[tuple[str, Path]] = []
    for sym_dir in symphonies:
        if sym_dir.is_symlink() or not sym_dir.is_dir():
            continue
        card_dir = sym_dir / card
        if card_dir.is_symlink() or not card_dir.is_dir():
            continue
        found.append((sym_dir.name, card_dir))
    return found


def _run_files(folder: Path) -> list[os.DirEntry[str]]:
    files: list[os.DirEntry[str]] = []
    with os.scandir(folder) as entries:
        for entry in entries:
            if not entry.name.endswith(_TRANSCRIPT_SUFFIX):
                continue
            stem = entry.name[: -len(_TRANSCRIPT_SUFFIX)]
            if not _valid_segment(stem) or entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                continue
            files.append(entry)
    return files


def resolve_run(root: Path, run: str) -> RunRef:
    """Find the one run with this id across the layout. Used by ``/runs/<run>`` links."""
    if not _valid_segment(run):
        message = f"not a run address: {run!r}"
        raise RunLookupError(message)
    for sym_dir in _symphony_folders(root):
        for card_dir in sorted(p for p in sym_dir.iterdir() if p.is_dir() and not p.is_symlink()):
            candidate = card_dir / f"{run}{_TRANSCRIPT_SUFFIX}"
            if candidate.is_symlink() or not candidate.is_file():
                continue
            return RunRef(symphony=sym_dir.name, card=card_dir.name, run=run)
    message = f"no transcript for run {run!r}"
    raise RunLookupError(message)


# --------------------------------------------------------------------------- #
# Summary (one streamed scan, cached)
# --------------------------------------------------------------------------- #


def _scan_summary(handle: BinaryIO) -> _Summary:
    summary = _Summary()
    handle.seek(0)
    for raw in handle:
        parsed = _parse(raw)
        if parsed is None:
            summary.record_count += 1
            summary.unreadable_count += 1
            continue
        kind = str(parsed.get("type") or "")
        if kind == RUN_HEADER_TYPE:
            continue
        summary.record_count += 1
        summary.counts[kind] = summary.counts.get(kind, 0) + 1
        ts = parsed.get("ts")
        if isinstance(ts, str):
            summary.first_ts = summary.first_ts or ts
            summary.last_ts = ts
        if _is_error(parsed):
            summary.error_count += 1
        if kind == USAGE_TYPE:
            _add_usage(summary, parsed)
        elif kind == RESULT_TYPE:
            summary.result_is_error = parsed.get("is_error") is True
            text = _as_text(parsed.get("text") or "")
            summary.result_text = text[:OUTCOME_PREVIEW_CHARS]
            found = _PR_URL.search(text) or _PR_URL.search(_as_text(parsed.get("output") or ""))
            if found is not None:
                summary.pr_url = found.group(0)
        elif kind == TRUNCATED_TYPE:
            summary.truncated = True
    return summary


def _add_usage(summary: _Summary, parsed: dict[str, Any]) -> None:
    usage = parsed.get("usage")
    if not isinstance(usage, dict):
        return
    values = {
        name: int(usage[name])
        for name in USAGE_FIELDS
        if isinstance(usage.get(name), int) and not isinstance(usage.get(name), bool)
    }
    if parsed.get("scope") == "run":
        summary.usage_run = values
        return
    for name, value in values.items():
        summary.usage_message[name] = summary.usage_message.get(name, 0) + value


def _usage_totals(summary: _Summary) -> dict[str, Any]:
    """Run-scope totals when the run recorded them, otherwise the sum of the turns."""
    source = summary.usage_run if summary.usage_run is not None else summary.usage_message
    scope = "run" if summary.usage_run is not None else ("message" if source else None)
    if scope is None:
        return {"scope": None, "reason": NO_USAGE_REASON, "input": None, "output": None,
                "cache_read": None, "cache_write": None}
    return {
        "scope": scope,
        "reason": None,
        "input": source.get("input_tokens"),
        "output": source.get("output_tokens"),
        "cache_read": source.get("cache_read"),
        "cache_write": source.get("cache_write"),
    }


def _duration_seconds(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    try:
        began = _aware(datetime.fromisoformat(start))
        ended = _aware(datetime.fromisoformat(end))
    except (TypeError, ValueError):
        return None
    return round((ended - began).total_seconds(), 3)


def _summary_for(path: Path, handle: BinaryIO) -> _Summary:
    # Device, inode, size and mtime together: a file replaced in place by the same size and
    # mtime (an atomic rename) is a different file and must not be served from the cache.
    info = os.fstat(handle.fileno())
    key = (str(path), info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    with _summary_lock:
        cached = _summary_cache.get(key)
        if cached is not None:
            _summary_cache.move_to_end(key)
            return cached
    summary = _scan_summary(handle)
    with _summary_lock:
        _summary_cache[key] = summary
        while len(_summary_cache) > SUMMARY_CACHE_SIZE:
            _summary_cache.popitem(last=False)
    return summary


def run_detail(root: Path, ref: RunRef) -> dict[str, Any]:
    """The header and the summary of one run. Raises the lookup errors for a missing run."""
    path = run_file(root, ref)
    with open_run(path, ref) as handle:
        header = read_header(handle)
        summary = _summary_for(path, handle)
    outcome: dict[str, Any]
    if summary.result_is_error is None:
        outcome = {"status": None, "reason": NO_OUTCOME_REASON, "text": None}
    else:
        outcome = {
            "status": "failed" if summary.result_is_error else "completed",
            "reason": None,
            "text": summary.result_text,
        }
    ended = summary.last_ts
    return {
        "symphony": ref.symphony,
        "card": ref.card,
        "run": ref.run,
        "header": header,
        "header_summary": _header_summary(header),
        "summary": {
            "records": summary.record_count,
            "unreadable": summary.unreadable_count,
            "errors": summary.error_count,
            "counts": summary.counts,
            "truncated": summary.truncated,
            "started_at": (header or {}).get("ts") or summary.first_ts,
            "ended_at": ended,
            "duration_seconds": _duration_seconds((header or {}).get("ts") or summary.first_ts, ended),
            "tokens": _usage_totals(summary),
            "cost": {"value": None, "reason": NO_COST_REASON},
            "outcome": outcome,
            "pr_url": summary.pr_url,
            "pr_reason": None if summary.pr_url else "no pull request link in the transcript",
        },
    }


# --------------------------------------------------------------------------- #
# Paging, positioning and search
# --------------------------------------------------------------------------- #


def read_page(
    handle: BinaryIO,
    *,
    after: int,
    limit: int,
    types: frozenset[str],
    errors: bool,
) -> RecordPage:
    """Records on lines after ``after``, up to ``limit`` of them, filtered by type or error.

    ``next_after`` is the last line scanned when the page filled, so the next call starts
    there. It is None at the end of the file.
    """
    handle.seek(0)
    records: list[dict[str, Any]] = []
    filled_at: int | None = None
    for number, raw in enumerate(handle, start=1):
        if number <= after:
            continue
        if len(records) >= limit:
            filled_at = number - 1
            break
        parsed = _parse(raw)
        if parsed is None:
            # An unreadable line is shown whenever it is not filtered out by type.
            if errors or not types:
                records.append(_unreadable_view(number, raw))
            continue
        if _is_event(parsed) and _matches_types(parsed, types, errors):
            records.append(_record_view(number, raw, parsed))
    next_after = filled_at if filled_at is not None else None
    return RecordPage(records=records, next_after=next_after, focus_line=None)


def read_before(
    handle: BinaryIO,
    *,
    before: int,
    limit: int,
    record_filter: RecordFilter,
) -> RecordPage:
    """The nearest ``limit`` matching records on lines before ``before``, in file order.

    Used to page back from a jump target. ``prev_before`` is the line of the first record
    returned, when more matching records come before it; None at the start of the run.
    The whole prefix is streamed (only the last ``limit`` matches are kept in memory).
    """
    window: deque[dict[str, Any]] = deque(maxlen=limit)
    total = 0
    handle.seek(0)
    for number, raw in enumerate(handle, start=1):
        if number >= before:
            break
        parsed = _parse(raw)
        if parsed is None:
            if record_filter.errors or not record_filter.types:
                window.append(_unreadable_view(number, raw))
                total += 1
            continue
        if _is_event(parsed) and _matches_types(parsed, record_filter.types, record_filter.errors):
            window.append(_record_view(number, raw, parsed))
            total += 1
    records = list(window)
    prev = records[0]["line"] if records and total > len(records) else None
    return RecordPage(records=records, next_after=None, focus_line=None, prev_before=prev)


def line_for_seq(handle: BinaryIO, seq: int) -> int | None:
    """The line whose record has this seq. A streamed scan."""
    handle.seek(0)
    for number, raw in enumerate(handle, start=1):
        parsed = _parse(raw)
        if parsed is not None and parsed.get("seq") == seq:
            return number
    return None


def line_for_time(handle: BinaryIO, at: datetime) -> int | None:
    """The first record at or after ``at``. Lines whose timestamp does not parse are passed over."""
    handle.seek(0)
    for number, raw in enumerate(handle, start=1):
        parsed = _parse(raw)
        if parsed is None or parsed.get("type") == RUN_HEADER_TYPE:
            continue
        ts = parsed.get("ts")
        if not isinstance(ts, str):
            continue
        try:
            when = datetime.fromisoformat(ts)
        except ValueError:
            continue
        if _aware(when) >= _aware(at):
            return number
    return None


def _aware(value: datetime) -> datetime:
    """A naive timestamp is UTC. Both sides are made aware before they are compared."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def search(
    handle: BinaryIO,
    *,
    query: str,
    record_filter: RecordFilter,
    after: int,
    limit: int,
) -> RecordPage:
    """Matching records after line ``after``, over the full text of each record (not its preview).

    ``query`` is matched case-insensitively. The text searched is the record's text,
    input, output and tool name, serialized. An unreadable line matches on its raw text
    when there is no type filter. A search scans at most SEARCH_BYTE_BUDGET bytes; when it
    stops early, ``stopped_at_byte`` says where and ``next_after`` is the cursor to continue.
    """
    needle = query.casefold()
    types = record_filter.types
    errors = record_filter.errors
    matches: list[dict[str, Any]] = []
    scanned = 0
    with _SEARCH_SLOTS:
        handle.seek(0)
        for number, raw in enumerate(handle, start=1):
            if number <= after:
                continue
            if len(matches) >= limit:
                return RecordPage(records=matches, next_after=number - 1, focus_line=None)
            if scanned >= SEARCH_BYTE_BUDGET:
                return RecordPage(
                    records=matches, next_after=number - 1, focus_line=None, stopped_at_byte=scanned,
                )
            scanned += len(raw)
            parsed = _parse(raw)
            if parsed is None:
                raw_text = raw.decode("utf-8", "replace").casefold()
                if errors or (not types and needle in raw_text):
                    matches.append(_unreadable_view(number, raw))
                continue
            if not _is_event(parsed) or not _matches_types(parsed, types, errors):
                continue
            if needle and needle not in _searchable(parsed):
                continue
            matches.append(_record_view(number, raw, parsed))
    return RecordPage(records=matches, next_after=None, focus_line=None)


def _searchable(parsed: dict[str, Any]) -> str:
    parts = [
        _as_text(parsed.get(name)) for name in ("text", "input", "output", "tool_name")
        if parsed.get(name) is not None
    ]
    return "\n".join(parts).casefold()


def full_field(handle: BinaryIO, *, line: int, field_name: str) -> dict[str, Any]:
    """One field of one record, in full. The value is always text; ``format`` says how it was made."""
    if field_name not in FULL_FIELDS:
        message = f"not a field the viewer loads in full: {field_name!r}"
        raise RunLookupError(message)
    handle.seek(0)
    for number, raw in enumerate(handle, start=1):
        if number != line:
            continue
        parsed = _parse(raw)
        if parsed is None:
            return {
                "line": line, "seq": None, "field": field_name, "present": True,
                "format": "text", "value": raw.decode("utf-8", "replace").rstrip("\n"),
                "bytes": len(raw), "unreadable": True,
            }
        value = parsed.get(field_name)
        text = "" if value is None else _as_text(value)
        seq = parsed.get("seq")
        return {
            "line": line,
            "seq": seq if isinstance(seq, int) and not isinstance(seq, bool) else None,
            "field": field_name,
            "present": value is not None,
            "format": "text" if isinstance(value, str) or value is None else "json",
            "value": text,
            "bytes": len(text.encode("utf-8", "surrogatepass")),
            "unreadable": False,
        }
    message = f"no line {line} in this transcript"
    raise RunLookupError(message)


def iter_bytes(handle: BinaryIO) -> Iterator[bytes]:
    """The file as stored, in chunks. Closes the handle when the stream ends or is dropped."""
    with handle:
        while True:
            chunk = handle.read(DOWNLOAD_CHUNK_BYTES)
            if not chunk:
                return
            yield chunk


def _fence(body: str, info: str = "") -> str:
    """A code fence longer than any backtick run in ``body``, so the content cannot close it."""
    longest = max((len(run) for run in re.findall(r"`+", body)), default=0)
    marker = "`" * max(3, longest + 1)
    return f"{marker}{info}\n{body}\n{marker}\n"


def _code(value: object) -> str:
    """A value as an inline code span, so Markdown shows it as literal text.

    Line breaks and tabs become spaces, so the value stays on its line. The span's backtick
    fence is one longer than the longest backtick run in the value, so the value cannot
    close it. Used for every value outside a fenced body, which is every header value,
    heading and tool name.
    """
    text = re.sub(r"[\r\n\t]+", " ", _as_text(value))
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * (longest + 1)
    # A space on each side is needed only when the value starts or ends with a backtick or space.
    pad = " " if text[:1] in ("`", " ") or text[-1:] in ("`", " ") else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def _markdown_parts(handle: BinaryIO, ref: RunRef) -> Iterator[str]:
    handle.seek(0)
    header = read_header(handle)
    summary = _header_summary(header)
    yield f"# Run {_code(ref.run)}\n\n"
    yield f"Card {_code(ref.card)} · symphony {_code(ref.symphony)}\n\n"
    for label, value in (
        ("Stage", summary.get("stage")),
        ("Agent", summary.get("agent")),
        ("Model", summary.get("model")),
        ("Endpoint", summary.get("endpoint")),
        ("Started", summary.get("started_at")),
    ):
        if value is not None:
            yield f"- {label}: {_code(value)}\n"
    yield "\n"
    handle.seek(0)
    for number, raw in enumerate(handle, start=1):
        yield from _markdown_event(number, raw)


def iter_markdown(handle: BinaryIO, ref: RunRef) -> Iterator[str]:
    """The run as Markdown, in chunks of about MARKDOWN_CHUNK_CHARS.

    Every value outside a fenced body is an inline code span (see _code) and every body is a
    fence longer than its own backtick runs (see _fence), so nothing is rendered as markup.
    """
    with handle:
        buffer: list[str] = []
        size = 0
        for part in _markdown_parts(handle, ref):
            buffer.append(part)
            size += len(part)
            if size >= MARKDOWN_CHUNK_CHARS:
                yield "".join(buffer)
                buffer = []
                size = 0
        if buffer:
            yield "".join(buffer)


def _markdown_event(number: int, raw: bytes) -> Iterator[str]:
    parsed = _parse(raw)
    if parsed is None:
        yield f"### Line {number}: unreadable\n\n"
        yield _fence(raw.decode("utf-8", "replace").rstrip("\n"), "text")
        yield "\n"
        return
    kind = str(parsed.get("type") or "")
    if kind == RUN_HEADER_TYPE:
        return
    seq = parsed.get("seq")
    label = str(seq) if seq is not None else str(number)
    yield f"### {label} · {_code(kind)}\n\n"
    tool = parsed.get("tool_name")
    if tool:
        yield f"Tool: {_code(tool)}\n\n"
    if parsed.get("text") is not None:
        yield _fence(_as_text(parsed["text"]), "text")
    if parsed.get("input") is not None:
        yield _fence(_as_text(parsed["input"]), "json")
    if parsed.get("output") is not None:
        yield _fence(_as_text(parsed["output"]), "json")
    if isinstance(parsed.get("usage"), dict):
        yield _fence(_as_text(parsed["usage"]), "json")
    yield "\n"


def known_types(types: Iterable[str]) -> frozenset[str]:
    """The requested types that the schema knows. Unknown names are dropped, not guessed."""
    return frozenset(t for t in types if t in TRANSCRIPT_TYPES)


__all__ = [
    "FULL_FIELDS",
    "LIST_MAX_RUNS",
    "PAGE_DEFAULT_LIMIT",
    "PAGE_MAX_LIMIT",
    "PREVIEW_CHARS",
    "SEARCH_DEFAULT_LIMIT",
    "SEARCH_MAX_LIMIT",
    "RecordPage",
    "RunLookupError",
    "RunRef",
    "RunRefusedError",
    "full_field",
    "iter_bytes",
    "iter_markdown",
    "known_types",
    "line_for_seq",
    "line_for_time",
    "list_runs",
    "open_run",
    "read_header",
    "read_page",
    "resolve_run",
    "run_detail",
    "run_file",
    "search",
]

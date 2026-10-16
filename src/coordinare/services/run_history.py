"""150: the History page's run table, read from durable storage. Read-only.

One listing answers the History page. It walks the transcripts directory: each symphony and
card folder costs an lstat, and each transcript costs an lstat, all against one budget. A
run's stage, agent and title come from its header (the first line), and its outcome and
pull request from the end of the file. Tokens and cost come from the token ledger (134), one
pass per page. No transcript is read whole, nothing is kept between requests, and the
in-memory activity log is never read.

Sources, by column:
- time: the header's ts (the start of the run); the modification time when there is no header
- symphony, card: the transcript folders (a conductor call is attributed to its symphony)
- stage, agent, model, card title: the run header
- duration: from the header's ts to the last record's ts
- tokens, cost: the token ledger, by run id
- outcome, pull request: the last result record in the last HISTORY_TAIL_BYTES of the file

Bounds, per request. Every lstat counts against HISTORY_STAT_MAX. The folder phase (symphony
and card folders) may spend at most half of it, so transcripts always get the other half.
Each card folder lists at most HISTORY_FOLDER_LIST_MAX entries, keeps its newest
HISTORY_CARD_FILES_MAX transcripts by modification time, and at most HISTORY_EXAMINE_MAX
candidates have their header read. Every bound that cut the scan short is reported in
``partial_reasons``, with a matching message in ``partial_details``.

Paging. Page one pins an anchor: the server's wall-clock time in nanoseconds when that page was
produced. Later pages carry the anchor in the cursor, skip every transcript modified at or after
it, and count them in ``newer_since_anchor`` so the client can say new runs exist. Equal counts
as new, so a run that lands on the anchor's own tick is announced rather than skipped. On a
filesystem whose modification times have one-second granularity, a run written in the same
second as page one can carry a time at or below the anchor. Such a run is listed only if it
sorts after the cursor, so it can be missed until the list is refreshed. Filesystems with
nanosecond times do not have this gap.

The cursor also carries a fingerprint of the filters it was issued for. A cursor for other
filters is ignored, and page one is served, with ``cursor_ignored`` set.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

import structlog

from coordinare.lib.path_segments import CONDUCTOR_FOLDER
from coordinare.lib.redaction import redact_state_value
from coordinare.services import run_viewer
from coordinare.services.run_viewer import RunLookupError, RunRef, RunRefusedError
from coordinare.services.token_usage import usage_summary

if TYPE_CHECKING:
    from pathlib import Path

    from coordinare.services.token_usage import ModelTotals, PriceTable, TokenLedger

logger = structlog.get_logger(__name__)

HISTORY_DEFAULT_LIMIT = 50
HISTORY_MAX_LIMIT = 200
HISTORY_STAT_MAX = 20_000
HISTORY_FOLDER_LIST_MAX = 2_000
HISTORY_CARD_FILES_MAX = 1_000
HISTORY_EXAMINE_MAX = 300
HISTORY_TAIL_BYTES = 64 * 1024
HISTORY_OUTCOME_CHARS = 300
CHECK_STAGE_PREFIX = "conductor:"
OUTCOMES = ("completed", "failed", "unknown")
TRANSCRIPT_SUFFIX = ".jsonl"
CURSOR_SEPARATOR = "|"
CURSOR_PARTS = 6
CURSOR_NUMBER_MAX_DIGITS = 20
FINGERPRINT_CHARS = 12
ASCII_DIGITS = "0123456789"
ASCII_HEX = "0123456789abcdef"
FOLDER_CAP_REASON = "folder_budget"
STAT_CAP_REASON = "stat_budget"
CARD_CAP_REASON = "card_budget"
LIST_CAP_REASON = "folder_listing"
EXAMINE_CAP_REASON = "examine_budget"
PARTIAL_DETAILS = {
    FOLDER_CAP_REASON: (
        "This listing is incomplete: not every card was checked. Runs in cards the scan did not "
        "reach are missing."
    ),
    STAT_CAP_REASON: (
        f"This listing is incomplete: the scan stopped after {HISTORY_STAT_MAX} entries, so runs it "
        "did not reach are missing."
    ),
    CARD_CAP_REASON: (
        f"This listing is incomplete: a card has more than {HISTORY_CARD_FILES_MAX} runs, so its "
        "oldest runs are not listed."
    ),
    LIST_CAP_REASON: (
        f"This listing is incomplete: a card folder has more than {HISTORY_FOLDER_LIST_MAX} entries, "
        "and the entries past the first read are not listed."
    ),
    EXAMINE_CAP_REASON: (
        f"This page is short: {HISTORY_EXAMINE_MAX} candidates were read without filling it. "
        "The next page continues from there."
    ),
}
NO_OUTCOME_REASON = "no result record in the end of the transcript (the run may still be going, or was cut off)"
NO_PR_REASON = "no pull request link in the run's result"
NO_DURATION_REASON = "the run has no timestamps"
NO_USAGE_REASON = "no usage records in this run"
NO_LEDGER_REASON = "transcripts are disabled, so no usage is tracked"


class HistoryQueryError(Exception):
    """A filter, a limit or a cursor that is not a valid query. Maps to 400."""


class HistorySymphonyError(Exception):
    """A symphony filter that names a symphony this daemon does not run. Maps to 404."""


@dataclass(frozen=True)
class HistoryFilter:
    symphony: str | None = None
    card: str | None = None
    stage: str | None = None
    agent: str | None = None
    outcome: str | None = None
    since: date | None = None
    until: date | None = None


@dataclass(frozen=True)
class HistoryQuery:
    """What the viewer asked for. ``visible`` is the symphonies this daemon runs (the scope)."""

    filters: HistoryFilter
    visible: frozenset[str]
    limit: int = HISTORY_DEFAULT_LIMIT
    cursor: str | None = None


@dataclass(frozen=True)
class UsageSource:
    """Where tokens and cost come from. ``ledger`` is None when transcription is off."""

    ledger: TokenLedger | None
    table: PriceTable


@dataclass(frozen=True)
class _Candidate:
    mtime_ns: int
    modified: datetime
    symphony: str
    card: str
    run: str
    path: Path

    @property
    def key(self) -> tuple[int, str, str, str]:
        return (self.mtime_ns, self.symphony, self.card, self.run)

    @property
    def ref(self) -> RunRef:
        return RunRef(symphony=self.symphony, card=self.card, run=self.run)


@dataclass(frozen=True)
class _Tail:
    outcome: str
    outcome_text: str | None
    pr_url: str | None
    last_ts: str | None


@dataclass
class _Budget:
    """One count for every lstat the listing makes. ``take`` may stop at a lower ceiling."""

    limit: int
    used: int = 0
    deadline: float | None = None

    def take(self, ceiling: int) -> bool:
        if self.used >= ceiling or (self.deadline is not None and time.monotonic() >= self.deadline):
            return False
        self.used += 1
        return True


@dataclass
class _Walk:
    found: list[_Candidate] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def note(self, reason: str) -> None:
        if reason not in self.reasons:
            self.reasons.append(reason)


# --------------------------------------------------------------------------- #
# Filters and cursor
# --------------------------------------------------------------------------- #


def filter_fingerprint(filters: HistoryFilter) -> str:
    """A short, stable hash of the filters, so a cursor is only used with the filters it came from."""
    canonical = {
        "symphony": filters.symphony,
        "card": filters.card,
        "stage": filters.stage,
        "agent": filters.agent,
        "outcome": filters.outcome,
        "since": filters.since.isoformat() if filters.since is not None else None,
        "until": filters.until.isoformat() if filters.until is not None else None,
    }
    text = json.dumps(canonical, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:FINGERPRINT_CHARS]


def _is_digits(text: str) -> bool:
    """ASCII digits only, at most CURSOR_NUMBER_MAX_DIGITS of them (not ``isdigit``, which admits '²')."""
    return 0 < len(text) <= CURSOR_NUMBER_MAX_DIGITS and all(char in ASCII_DIGITS for char in text)


def _is_fingerprint(text: str) -> bool:
    return len(text) == FINGERPRINT_CHARS and all(char in ASCII_HEX for char in text)


def _format_cursor(fingerprint: str, anchor: int, candidate: _Candidate) -> str:
    return CURSOR_SEPARATOR.join((
        fingerprint, str(anchor), str(candidate.mtime_ns), candidate.symphony, candidate.card, candidate.run,
    ))


def _parse_cursor(raw: str) -> tuple[str, int, tuple[int, str, str, str]]:
    parts = raw.split(CURSOR_SEPARATOR)
    if (
        len(parts) != CURSOR_PARTS
        or not _is_fingerprint(parts[0])
        or not _is_digits(parts[1])
        or not _is_digits(parts[2])
        or not all(parts[3:])
    ):
        message = "the cursor is not one this listing issued"
        raise HistoryQueryError(message)
    return parts[0], int(parts[1]), (int(parts[2]), parts[3], parts[4], parts[5])


# --------------------------------------------------------------------------- #
# Walk (lstat only, bounded by one budget)
# --------------------------------------------------------------------------- #


def _symphony_names(query: HistoryQuery) -> list[str]:
    if query.filters.symphony is not None:
        return [query.filters.symphony, CONDUCTOR_FOLDER]
    return [*sorted(query.visible), CONDUCTOR_FOLDER]


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except OSError:
        return None


def _names(folder: Path) -> tuple[list[str], bool]:
    """Entry names from a folder listing, at most HISTORY_FOLDER_LIST_MAX of them, and whether it was cut.

    No stat is made. Past the bound the listing stops, so the entries not read are not seen.
    """
    names: list[str] = []
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                if len(names) == HISTORY_FOLDER_LIST_MAX:
                    return names, True
                names.append(entry.name)
    except OSError:
        return [], False
    return names, False


def _card_folders(
    root: Path, query: HistoryQuery, budget: _Budget, ceiling: int, walk: _Walk,
) -> list[tuple[int, str, str, Path]]:
    """Every card folder the query may read, with its modification time. Each check is counted."""
    folders: list[tuple[int, str, str, Path]] = []
    for symphony in _symphony_names(query):
        symphony_dir = root / symphony
        if not budget.take(ceiling):
            walk.note(FOLDER_CAP_REASON)
            return folders
        info = _lstat(symphony_dir)
        if info is None or not stat.S_ISDIR(info.st_mode):
            continue
        if query.filters.card is not None:
            cards = [query.filters.card]
        else:
            listed, cut = _names(symphony_dir)
            if cut:
                walk.note(LIST_CAP_REASON)
            cards = sorted(listed)
        for card in cards:
            if not run_viewer.is_address_segment(card):
                continue
            if not budget.take(ceiling):
                walk.note(FOLDER_CAP_REASON)
                return folders
            card_dir = symphony_dir / card
            card_info = _lstat(card_dir)
            if card_info is None or not stat.S_ISDIR(card_info.st_mode):
                continue
            folders.append((card_info.st_mtime_ns, symphony, card, card_dir))
    return folders


def _candidate(folder: Path, symphony: str, card: str, name: str) -> _Candidate | None:
    run = name[: -len(TRANSCRIPT_SUFFIX)]
    if not run_viewer.is_address_segment(run):
        return None
    path = folder / name
    info = _lstat(path)
    if info is None or not stat.S_ISREG(info.st_mode):
        return None
    return _Candidate(
        mtime_ns=info.st_mtime_ns,
        modified=datetime.fromtimestamp(info.st_mtime, UTC),
        symphony=symphony,
        card=card,
        run=run,
        path=path,
    )


def _card_runs(folder: Path, symphony: str, card: str, budget: _Budget, walk: _Walk) -> None:
    """The newest transcripts of one card, each statted once, within the shared budget."""
    listed, cut = _names(folder)
    if cut:
        walk.note(LIST_CAP_REASON)
    names = [name for name in listed if name.endswith(TRANSCRIPT_SUFFIX)]
    gathered: list[_Candidate] = []
    for name in names:
        if not budget.take(budget.limit):
            walk.note(STAT_CAP_REASON)
            break
        candidate = _candidate(folder, symphony, card, name)
        if candidate is not None:
            gathered.append(candidate)
    gathered.sort(key=lambda item: (item.mtime_ns, item.run), reverse=True)
    if len(gathered) > HISTORY_CARD_FILES_MAX:
        walk.note(CARD_CAP_REASON)
        gathered = gathered[:HISTORY_CARD_FILES_MAX]
    walk.found.extend(gathered)


def _walk(root: Path, query: HistoryQuery, budget: _Budget) -> _Walk:
    """Every transcript under the folders the query may read, within the budget.

    The folder phase may spend at most half of the budget. Folders are then read newest first,
    so under a tight budget the folders most likely to hold the newest runs are the ones read.
    A run appended to after its folder last changed can still sit in an unread folder, which is
    why a capped walk is reported as incomplete rather than as the newest runs.
    """
    walk = _Walk()
    folder_ceiling = budget.limit // 2
    folders = _card_folders(root, query, budget, folder_ceiling, walk)
    folders.sort(key=lambda folder: folder[:3], reverse=True)
    for _mtime, symphony, card, folder in folders:
        if budget.deadline is not None and time.monotonic() >= budget.deadline:
            walk.note(STAT_CAP_REASON)
            break
        if STAT_CAP_REASON in walk.reasons:
            break
        _card_runs(folder, symphony, card, budget, walk)
    return walk


# --------------------------------------------------------------------------- #
# Per-run reads (bounded)
# --------------------------------------------------------------------------- #


def _parse_record(raw: bytes) -> dict[str, Any] | None:
    try:
        parsed: object = json.loads(raw)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _tail(path: Path, ref: RunRef) -> _Tail:
    """The outcome and the last timestamp, from the last HISTORY_TAIL_BYTES of the transcript."""
    with run_viewer.open_run(path, ref) as handle:
        size = handle.seek(0, os.SEEK_END)
        start = max(0, size - HISTORY_TAIL_BYTES)
        handle.seek(start)
        data = handle.read()
    lines = data.split(b"\n")
    if start > 0:
        lines = lines[1:]  # the first piece starts mid-line
    last_ts: str | None = None
    result: dict[str, Any] | None = None
    for raw in reversed(lines):
        parsed = _parse_record(raw)
        if parsed is None:
            continue
        ts = parsed.get("ts")
        if last_ts is None and isinstance(ts, str):
            last_ts = ts
        if parsed.get("type") == run_viewer.RESULT_TYPE:
            result = parsed
            break
    if result is None:
        return _Tail("unknown", None, None, last_ts)
    text = str(result.get("text") or "")
    pr_url = run_viewer.find_pr_url(text) or run_viewer.find_pr_url(str(result.get("output") or ""))
    outcome = "failed" if result.get("is_error") is True else "completed"
    return _Tail(outcome, text[:HISTORY_OUTCOME_CHARS], pr_url, last_ts)


def _seconds_between(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    try:
        began = datetime.fromisoformat(start)
        ended = datetime.fromisoformat(end)
    except ValueError:
        return None
    if began.tzinfo is None or ended.tzinfo is None:
        return None
    return round((ended - began).total_seconds(), 3)


def _started_at(header_ts: object, modified: datetime) -> datetime:
    if isinstance(header_ts, str):
        try:
            started = datetime.fromisoformat(header_ts)
        except ValueError:
            return modified
        return started if started.tzinfo is not None else modified
    return modified


def _symphony_scope_ok(summary: dict[str, Any], query: HistoryQuery) -> str | None:
    """The symphony a conductor call belongs to, when the viewer may see it. None otherwise."""
    conductor = summary.get("conductor") or {}
    owner = conductor.get("symphony")
    if not isinstance(owner, str) or owner not in query.visible:
        return None
    if query.filters.symphony is not None and owner != query.filters.symphony:
        return None
    return owner


def _passes_header_filters(summary: dict[str, Any], started: datetime, filters: HistoryFilter) -> bool:
    if filters.agent is not None and (summary.get("persona") or {}).get("name") != filters.agent:
        return False
    if filters.stage is not None and summary.get("stage") != filters.stage:
        return False
    day = started.astimezone(UTC).date()
    if filters.since is not None and day < filters.since:
        return False
    return not (filters.until is not None and day > filters.until)


def _row_for(candidate: _Candidate, query: HistoryQuery) -> dict[str, Any] | None:
    """One row of the listing, or None when the run is outside the query or cannot be read."""
    try:
        header = run_viewer.header_of(candidate.path, candidate.ref)
        tail = _tail(candidate.path, candidate.ref)
    except (RunLookupError, RunRefusedError, OSError):
        return None
    summary = run_viewer.summarise_header(header)
    folder = candidate.symphony
    if folder == CONDUCTOR_FOLDER:
        owner = _symphony_scope_ok(summary, query)
        if owner is None:
            return None
        folder = owner
    started = _started_at((header or {}).get("ts"), candidate.modified)
    if not _passes_header_filters(summary, started, query.filters):
        return None
    if query.filters.outcome is not None and tail.outcome != query.filters.outcome:
        return None
    stage = summary.get("stage")
    card = summary.get("card") or {}
    duration = _seconds_between((header or {}).get("ts"), tail.last_ts)
    return {
        "kind": "check" if isinstance(stage, str) and stage.startswith(CHECK_STAGE_PREFIX) else "run",
        "symphony": candidate.symphony,
        "attributed_symphony": folder,
        "card": candidate.card,
        "run": candidate.run,
        "started_at": started.isoformat(),
        "modified_at": candidate.modified.isoformat(),
        "stage": stage,
        "stage_reason": None if stage else summary.get("reason"),
        "decision": (summary.get("conductor") or {}).get("decision"),
        "agent": summary.get("agent"),
        "persona": (summary.get("persona") or {}).get("name"),
        "persona_version": (summary.get("persona") or {}).get("version"),
        "persona_sha256": (summary.get("persona") or {}).get("version_sha256"),
        "persona_changed_at": (summary.get("persona") or {}).get("changed_at"),
        "model": summary.get("model"),
        "card_title": redact_state_value(card.get("title")),
        "duration_seconds": duration,
        "duration_reason": None if duration is not None else NO_DURATION_REASON,
        "outcome": tail.outcome,
        "outcome_reason": NO_OUTCOME_REASON if tail.outcome == "unknown" else None,
        "outcome_text": redact_state_value(tail.outcome_text),
        "pr_url": tail.pr_url,
        "pr_reason": None if tail.pr_url else NO_PR_REASON,
    }


def retained_runs(
    root: Path, visible: frozenset[str], *, limit: int, deadline: float,
) -> dict[str, Any]:
    """A bounded, unpaged source for statistics using the History reader's guards.

    Unlike the interactive listing this reads at most ``limit`` candidates once,
    and reports every discovery bound. No usage or transcript body is read here.
    """
    query = HistoryQuery(filters=HistoryFilter(), visible=visible)
    budget = _Budget(HISTORY_STAT_MAX, deadline=deadline)
    walk = _walk(root, query, budget)
    walk.found.sort(key=lambda candidate: candidate.key, reverse=True)
    rows = []
    reasons = list(walk.reasons)
    if time.monotonic() >= deadline:
        reasons.append("history_time_budget")
    for index, candidate in enumerate(walk.found):
        if index >= limit:
            reasons.append("run_limit")
            break
        if time.monotonic() >= deadline:
            reasons.append("history_time_budget")
            break
        row = _row_for(candidate, query)
        if row is None:
            reasons.append("unreadable_run")
        else:
            rows.append(row)
    return {"rows": rows, "partial_reasons": sorted(set(reasons)), "scanned": budget.used}


# --------------------------------------------------------------------------- #
# Tokens and cost (once per page)
# --------------------------------------------------------------------------- #


def _usage_figures(usage: UsageSource, totals: ModelTotals | None) -> dict[str, Any]:
    """Tokens and cost of one run. Each figure is None with a reason when it is not known."""
    if usage.ledger is None:
        return {"tokens": None, "tokens_reason": NO_LEDGER_REASON, "cost_usd": None, "cost_reason": NO_LEDGER_REASON}
    if totals is None or sum(counts.total for counts in totals.values()) == 0:
        return {"tokens": None, "tokens_reason": NO_USAGE_REASON, "cost_usd": None, "cost_reason": NO_USAGE_REASON}
    summary = usage_summary(totals, usage.table)
    cost = summary["cost"]
    return {
        "tokens": summary["tokens"]["total"],
        "tokens_reason": None,
        "cost_usd": cost["usd"],
        "cost_reason": None if cost["usd"] is not None else cost["unknown_reason"],
    }


def _attach_usage(rows: list[dict[str, Any]], usage: UsageSource) -> None:
    """One ledger pass for the whole page, then one dict lookup per row."""
    totals: dict[str, ModelTotals] = {}
    if usage.ledger is not None and rows:
        totals = usage.ledger.totals_by_run(row["run"] for row in rows)
    for row in rows:
        row.update(_usage_figures(usage, totals.get(row["run"])))


# --------------------------------------------------------------------------- #
# The listing
# --------------------------------------------------------------------------- #


def list_history(root: Path, query: HistoryQuery, usage: UsageSource) -> dict[str, Any]:
    """One page of runs, newest modification first, filtered on the server.

    Raises HistorySymphonyError for a symphony this daemon does not run, and HistoryQueryError
    for a card that is not an address or a cursor that is malformed. A well-formed cursor for
    other filters is ignored, and page one is served.
    """
    filters = query.filters
    if filters.symphony is not None and filters.symphony not in query.visible:
        message = f"no symphony named {filters.symphony!r} on this daemon"
        raise HistorySymphonyError(message)
    if filters.card is not None and not run_viewer.is_address_segment(filters.card):
        message = "the card filter is not a card address"
        raise HistoryQueryError(message)
    fingerprint = filter_fingerprint(filters)
    anchor = time.time_ns()
    after: tuple[int, str, str, str] | None = None
    cursor_ignored = False
    if query.cursor is not None:
        cursor_fingerprint, cursor_anchor, cursor_after = _parse_cursor(query.cursor)
        if cursor_fingerprint == fingerprint:
            anchor, after = cursor_anchor, cursor_after
        else:
            cursor_ignored = True
    paging = after is not None
    budget = _Budget(HISTORY_STAT_MAX)
    walk = _walk(root, query, budget)
    newer = sum(1 for candidate in walk.found if paging and candidate.mtime_ns >= anchor)
    walk.found.sort(key=lambda candidate: candidate.key, reverse=True)
    rows: list[dict[str, Any]] = []
    examined = 0
    last: _Candidate | None = None
    more = False
    examine_hit = False
    for candidate in walk.found:
        if after is not None and candidate.key >= after:
            continue
        if paging and candidate.mtime_ns >= anchor:
            continue
        if len(rows) == query.limit:
            more = True
            break
        if examined == HISTORY_EXAMINE_MAX:
            more = True
            examine_hit = True
            break
        examined += 1
        last = candidate
        row = _row_for(candidate, query)
        if row is not None:
            rows.append(row)
    _attach_usage(rows, usage)
    if examine_hit:
        walk.note(EXAMINE_CAP_REASON)
    next_cursor = _format_cursor(fingerprint, anchor, last) if more and last is not None else None
    logger.debug(
        "run_history.listed", rows=len(rows), examined=examined, scanned=budget.used, reasons=walk.reasons,
    )
    return {
        "rows": rows,
        "next_cursor": next_cursor,
        "limit": query.limit,
        "examined": examined,
        "scanned": budget.used,
        "partial": bool(walk.reasons),
        "partial_reasons": list(walk.reasons),
        "partial_details": [PARTIAL_DETAILS[reason] for reason in walk.reasons],
        "newer_since_anchor": newer,
        "cursor_ignored": cursor_ignored,
    }

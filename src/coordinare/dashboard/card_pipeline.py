"""147: the card pipeline, read from the daemon for the snapshot and the timeline endpoint.

The board strip is computed from the lifecycle log and the live sessions, and cached per card: a
card whose history and stage have not changed since the last snapshot is not recomputed, so the
snapshot costs nothing for it. The timeline endpoint reads each run's header from the transcript
index, and the end of each run's file: a bounded tail, never the whole body. Each request is held to
a byte budget (TIMELINE_READ_BYTES) across all its runs, and the run count is capped by the listing
(run_viewer.LIST_MAX_RUNS). A run past the budget is listed with no duration and no outcome. Tokens
and cost come from the ledger. Free text is redacted by the caller before it leaves the server.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from coordinare.lib.path_segments import sanitize_segment
from coordinare.services import run_viewer
from coordinare.services.card_lifecycle import CardLifecycleLog
from coordinare.services.card_timeline import (
    PIPELINE_STAGES,
    CardRef,
    LiveStage,
    RunIndex,
    UsageLedger,
    build_timeline,
    pipeline_strip,
    stage_for_run,
)
from coordinare.services.run_transcript import TranscriptCollector
from coordinare.services.token_usage import (
    TokenLedger,
    price_table_from_config,
    usage_summary,
)

if TYPE_CHECKING:
    from pathlib import Path

    from coordinare.daemon import CoordinareDaemon

NOT_CONFIGURED = "run transcripts are not configured on this daemon"
TRANSCRIPTS_DISABLED = "transcripts are disabled, so token and cost figures are not tracked"
#: The bytes read from the end of one run's transcript, to find its last timestamp and its outcome.
TIMELINE_TAIL_BYTES = 64 * 1024
#: The bytes read across all the runs of one timeline request.
TIMELINE_READ_BYTES = 4 * 1024 * 1024
#: A session in this phase has no stage running; any other phase names the stage it is in.
_IDLE_PHASE = "idle"


def _log(daemon: CoordinareDaemon) -> CardLifecycleLog | None:
    log = daemon.state.get("card_lifecycle")
    return log if isinstance(log, CardLifecycleLog) else None


def _lifecycle(daemon: CoordinareDaemon) -> list[str]:
    return [str(stage) for stage in (daemon.state.get("lifecycle_sequence") or []) if isinstance(stage, str)]


def _symphony_state(daemon: CoordinareDaemon, symphony: str) -> Any:
    return (daemon.state.get("symphony_states") or {}).get(symphony)


def _sessions_of(daemon: CoordinareDaemon, symphony: str) -> dict[str, Any]:
    sym_state = _symphony_state(daemon, symphony)
    if sym_state is not None:
        sessions = getattr(sym_state, "active_sessions", None)
        if isinstance(sessions, dict):
            return sessions
    top = daemon.state.get("active_sessions")
    return top if isinstance(top, dict) else {}


def live_stage(daemon: CoordinareDaemon, symphony: str, card_id: str) -> str | None:
    """The stage a card's live session is in, when the session is not idle. None otherwise."""
    session = _sessions_of(daemon, symphony).get(card_id)
    if not isinstance(session, dict):
        return None
    stage = session.get("performer_stage")
    if not isinstance(stage, str) or session.get("phase") in (None, _IDLE_PHASE):
        return None
    return stage if stage in PIPELINE_STAGES else None


def live_head(daemon: CoordinareDaemon, symphony: str, card_id: str) -> str | None:
    """The PR head the card's live session last reported, from session state. None when it has no session
    or the session has not yet seen a PR. Read from memory only: GitHub is never called for this."""
    session = _sessions_of(daemon, symphony).get(card_id)
    card = session.get("current_card") if isinstance(session, dict) else None
    head = card.get("head_after") if isinstance(card, dict) else None
    return head if isinstance(head, str) and head else None


def configured_symphonies(daemon: CoordinareDaemon) -> list[str]:
    return [str(name) for name in (daemon.state.get("symphony_configs") or {})]


def board_cards(daemon: CoordinareDaemon, symphony: str) -> set[str]:
    """Every card id on the symphony's board, from its snapshot. Empty when it has no board yet."""
    sym_state = _symphony_state(daemon, symphony)
    board = getattr(sym_state, "board_snapshot", None) if sym_state is not None else None
    if not isinstance(board, dict):
        return set()
    out: set[str] = set()
    for column in board.values():
        if isinstance(column, list):
            out.update(str(item) for item in column if isinstance(item, str))
    return out


def card_pipeline_block(daemon: CoordinareDaemon) -> dict[str, dict[str, dict[str, Any]]]:
    """Per symphony, each logged card's board strip, from memory.

    A card's strip is cached on the log with the revision of its history, its live stage and the
    lifecycle it is judged against. An unchanged card returns its cached strip and costs no work.
    Cache entries for cards the log no longer holds are dropped.
    """
    log = _log(daemon)
    if log is None:
        return {}
    lifecycle = tuple(_lifecycle(daemon))
    block: dict[str, dict[str, dict[str, Any]]] = {}
    seen: set[tuple[str, ...]] = set()
    for symphony in log.symphonies():
        strips: dict[str, dict[str, Any]] = {}
        for card_id in log.cards(symphony):
            current = live_stage(daemon, symphony, card_id)
            head = live_head(daemon, symphony, card_id)
            key = ("strip", symphony, card_id)
            seen.add(key)
            revision = (log.revision(symphony, card_id), current, lifecycle, head)
            cached = log.derived.get(key)
            if cached is not None and cached[0] == revision:
                strips[card_id] = cached[1]
                continue
            strip = pipeline_strip(
                log.peek(symphony, card_id), LiveStage(current=current, lifecycle=lifecycle, head=head),
            )
            log.derived[key] = (revision, strip)
            strips[card_id] = strip
        block[symphony] = strips
    for stale in [k for k in log.derived if k[0] == "strip" and k not in seen]:
        del log.derived[stale]
    return block


def _ledger(daemon: CoordinareDaemon) -> TokenLedger | None:
    collector = daemon.state.get("transcript_collector")
    usage = getattr(collector, "usage", None)
    if isinstance(usage, TokenLedger) and getattr(collector, "enabled", False) is True:
        return usage
    return None


def card_stage_usage(
    daemon: CoordinareDaemon, *, symphony: str, card_id: str,
) -> tuple[dict[str, dict[str, Any] | None] | None, str | None]:
    """Tokens and cost per stage from the ledger. (None, reason) when the ledger is not tracking."""
    ledger = _ledger(daemon)
    if ledger is None:
        return None, TRANSCRIPTS_DISABLED
    table = price_table_from_config(getattr(daemon.state.get("config"), "cost_tracking", None))
    folder = sanitize_segment(symphony)
    blocks: dict[str, dict[str, Any] | None] = {}
    for stage in PIPELINE_STAGES:
        totals = ledger.totals(symphony=folder, card=card_id, stage=stage)
        has_tokens = sum(counts.total for counts in totals.values()) > 0
        blocks[stage] = usage_summary(totals, table) if has_tokens else None
    return blocks, None


def _seconds_between(start: object, end: object) -> float | None:
    """The seconds from one recorded timestamp to another, or None when either is missing or unreadable."""
    if not isinstance(start, str) or not isinstance(end, str):
        return None
    try:
        began = datetime.fromisoformat(start)
        ended = datetime.fromisoformat(end)
    except ValueError:
        return None
    began = began if began.tzinfo else began.replace(tzinfo=UTC)
    ended = ended if ended.tzinfo else ended.replace(tzinfo=UTC)
    return round((ended - began).total_seconds(), 3)


#: The state of one run's read. "done": its result record is in the tail. "running": the tail holds no
#: result, so the run has not ended. "budget": the request's byte budget was spent before this run.
#: "unreadable": the file could not be read.
READ_DONE = "done"
READ_RUNNING = "running"
READ_BUDGET = "budget"
READ_UNREADABLE = "unreadable"


def _tail_facts(root: Path, ref: run_viewer.RunRef, budget: int) -> tuple[str | None, str | None, int, str]:
    """(end time, outcome, bytes read, read state) from the end of one run's transcript.

    Reads at most TIMELINE_TAIL_BYTES and at most ``budget`` bytes. An end time and an outcome come
    only from a terminal result record in that tail. A run with no such record in the tail is still
    running, and gets neither.
    """
    if budget <= 0:
        return None, None, 0, READ_BUDGET
    path = run_viewer.run_file(root, ref)
    with run_viewer.open_run(path, ref) as handle:
        size = handle.seek(0, os.SEEK_END)
        want = min(size, TIMELINE_TAIL_BYTES, budget)
        handle.seek(size - want)
        raw = handle.read(want)
    lines = raw.split(b"\n")
    if want < size:
        lines = lines[1:]  # the first line of a tail may be cut mid-record
    for line in reversed(lines):
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict) or record.get("type") != "result":
            continue
        ts = record.get("ts")
        if not isinstance(ts, str):
            break
        outcome = "failed" if record.get("is_error") is True else "completed"
        return ts, outcome, want, READ_DONE
    return None, None, want, READ_RUNNING


def transcript_runs(
    root: Path | None, *, symphony: str, card_id: str,
) -> tuple[list[dict[str, Any]] | None, str | None, bool]:
    """The card's runs from the transcript index, newest first. (runs, reason, truncated).

    ``runs`` is None when the transcripts are not configured. Each run's start comes from its
    header (the index); its end and outcome come from the tail of its file, within the request's
    byte budget. A run past the budget, or one that cannot be read, is listed with no end and no
    outcome rather than hidden.
    """
    if root is None:
        return None, NOT_CONFIGURED, False
    folder = sanitize_segment(symphony)
    try:
        listing = run_viewer.list_runs(root, symphony=folder, card=card_id)
    except run_viewer.RunLookupError:
        return [], None, False
    runs: list[dict[str, Any]] = []
    budget = TIMELINE_READ_BYTES
    for row in listing["runs"]:
        ref = run_viewer.RunRef(symphony=str(row["symphony"]), card=card_id, run=str(row["run"]))
        ended: str | None = None
        outcome: str | None = None
        try:
            ended, outcome, used, read_state = _tail_facts(root, ref, budget)
            budget -= used
        except (run_viewer.RunLookupError, run_viewer.RunRefusedError, OSError):
            read_state = READ_UNREADABLE
        started = row.get("started_at")
        runs.append({
            "run": ref.run,
            "stage": stage_for_run(row.get("stage")),
            "started_at": started,
            "ended_at": ended,
            "duration_seconds": _seconds_between(started, ended),
            "outcome": outcome,
            "read_state": read_state,
        })
    return runs, None, bool(listing.get("truncated"))


def transcript_root(daemon: CoordinareDaemon) -> Path | None:
    collector = daemon.state.get("transcript_collector")
    if not isinstance(collector, TranscriptCollector):
        return None
    return collector.root


def card_is_known(
    daemon: CoordinareDaemon, symphony: str, card_id: str, *, events: bool, runs: list[dict[str, Any]] | None,
) -> bool:
    """Whether the card belongs to this symphony: its log, its board, its live session, or its runs.

    A symphony the daemon does not configure and has never logged is unknown, so a card id from
    another symphony is refused under this name.
    """
    log = _log(daemon)
    logged = log.symphonies() if log is not None else []
    if symphony not in configured_symphonies(daemon) and symphony not in logged:
        return False
    if events or card_id in board_cards(daemon, symphony) or card_id in _sessions_of(daemon, symphony):
        return True
    return bool(runs)


def assemble_timeline(
    daemon: CoordinareDaemon, root: Path | None, *, symphony: str, card_id: str,
) -> dict[str, Any] | None:
    """The full timeline for one card of one symphony, or None when the card is not in that symphony."""
    log = _log(daemon)
    recorded = log.events(symphony, card_id) if log is not None else []
    runs, runs_reason, truncated = transcript_runs(root, symphony=symphony, card_id=card_id)
    if not card_is_known(daemon, symphony, card_id, events=bool(recorded), runs=runs):
        return None
    usage, usage_reason = card_stage_usage(daemon, symphony=symphony, card_id=card_id)
    return build_timeline(
        CardRef(symphony=symphony, card_id=card_id),
        recorded,
        LiveStage(current=live_stage(daemon, symphony, card_id), lifecycle=tuple(_lifecycle(daemon))),
        RunIndex(runs=runs, reason=runs_reason, truncated=truncated),
        UsageLedger(stages=usage, reason=usage_reason),
    )

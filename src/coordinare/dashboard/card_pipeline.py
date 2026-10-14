"""147: the card pipeline, read from the daemon for the snapshot and the timeline endpoint.

The board strip is computed in memory from the lifecycle log and the live sessions, so the
snapshot carries it at no per-card cost. The timeline endpoint adds the transcript index (run
counts, durations, outcomes) and the token ledger (tokens and cost per stage). Neither reads a
transcript body. Free text is redacted by the caller before it leaves the server.
"""

from __future__ import annotations

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
    """Per symphony, each logged card's board strip. Read from memory only."""
    log = _log(daemon)
    if log is None:
        return {}
    lifecycle = _lifecycle(daemon)
    block: dict[str, dict[str, dict[str, Any]]] = {}
    for symphony in log.symphonies():
        block[symphony] = {
            card_id: pipeline_strip(
                log.events(symphony, card_id),
                LiveStage(current=live_stage(daemon, symphony, card_id), lifecycle=tuple(lifecycle)),
            )
            for card_id in log.cards(symphony)
        }
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


def transcript_runs(
    root: Path | None, *, symphony: str, card_id: str,
) -> tuple[list[dict[str, Any]] | None, str | None, bool]:
    """The card's runs from the transcript index, newest first. (runs, reason, truncated).

    ``runs`` is None when the transcripts are not configured. Each run's duration and outcome
    come from its own recorded timestamps and result record (run_viewer.run_detail). A run that
    cannot be read is still listed, with no duration and no outcome, rather than hidden.
    """
    if root is None:
        return None, NOT_CONFIGURED, False
    folder = sanitize_segment(symphony)
    try:
        listing = run_viewer.list_runs(root, symphony=folder, card=card_id)
    except run_viewer.RunLookupError:
        return [], None, False
    runs: list[dict[str, Any]] = []
    for row in listing["runs"]:
        ref = run_viewer.RunRef(symphony=str(row["symphony"]), card=card_id, run=str(row["run"]))
        summary: dict[str, Any] = {}
        outcome: str | None = None
        try:
            summary = run_viewer.run_detail(root, ref, usage={})["summary"]
            outcome = summary["outcome"].get("status")
        except (run_viewer.RunLookupError, run_viewer.RunRefusedError, OSError):
            outcome = None
        runs.append({
            "run": ref.run,
            "stage": stage_for_run(row.get("stage")),
            "started_at": summary.get("started_at"),
            "ended_at": summary.get("ended_at"),
            "duration_seconds": summary.get("duration_seconds"),
            "outcome": outcome,
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

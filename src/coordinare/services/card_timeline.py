"""147: a card's pipeline timeline, assembled from what was recorded. Pure: no I/O.

The sources are read by the callers and passed in: the lifecycle log (services/card_lifecycle),
the transcript index and run summaries (services/run_viewer) and the token ledger
(services/token_usage). This module decides each stage's status, orders the lifecycle events and
totals the runs. Nothing is invented: a stage or a figure with no source is None, and the payload
names the source that is missing.

Stages are the pipeline stages of ``lifecycle.ROLE_TO_STAGE``, in pipeline order. The advocate
and the environment builder run outside a card's pipeline and are not listed here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from coordinare.lifecycle import ROLE_TO_STAGE

#: The card pipeline, in order. Each stage is a performer stage code (ROLE_TO_STAGE values).
PIPELINE_STAGES: tuple[str, ...] = (
    "assessing",
    "architecting",
    "implementing",
    "reviewing",
    "security",
    "qa",
    "documenting",
    "closing_review",
)

STATUS_DONE = "done"
STATUS_CURRENT = "current"
STATUS_FAILED = "failed"
STATUS_RAN = "ran"
STATUS_SKIPPED = "skipped"
STATUS_PENDING = "pending"
STATUS_NOT_RECORDED = "not_recorded"

RESULT_PASSED = "pass"
RESULT_FAILED = "fail"
#: The status a stage's latest verdict decides on its own.
_VERDICT_STATUS: dict[str, str] = {RESULT_FAILED: STATUS_FAILED, RESULT_PASSED: STATUS_DONE}

_STAGE_EVENT_KINDS = frozenset({"dispatched", "verdict"})


@dataclass(frozen=True)
class CardRef:
    """The card a timeline is for: its symphony and its id."""

    symphony: str
    card_id: str


@dataclass(frozen=True)
class LiveStage:
    """Where the card is now (its live stage, if any) and the stages this deployment runs."""

    current: str | None = None
    lifecycle: tuple[str, ...] = ()


@dataclass(frozen=True)
class RunIndex:
    """The card's transcript index. ``runs`` is None when the transcripts are not available."""

    runs: list[dict[str, Any]] | None
    reason: str | None = None
    truncated: bool = False


@dataclass(frozen=True)
class UsageLedger:
    """The ledger's usage per stage. ``stages`` is None when the ledger is not tracking."""

    stages: dict[str, dict[str, Any] | None] | None
    reason: str | None = None


@dataclass(frozen=True)
class _Sources:
    """What a stage block is read from, bundled so the block takes few arguments."""

    events: list[dict[str, Any]]
    runs: list[dict[str, Any]] | None
    usage: UsageLedger
    dropped: int = 0


@dataclass(frozen=True)
class _Frame:
    """The facts a stage's status is decided from, computed once per timeline."""

    current: str | None
    lifecycle: frozenset[str]
    verdicts: dict[str, dict[str, Any]]
    reached: frozenset[str]
    last_index: int
    merged: bool
    recorded: bool


def stage_for_run(name: object) -> str | None:
    """The pipeline stage a run belongs to, from the persona name its header records.

    The header names the role (``implementer``) and a few older runs name the stage itself.
    A conductor call and anything outside the pipeline is None.
    """
    if not isinstance(name, str) or not name:
        return None
    stage = ROLE_TO_STAGE.get(name, name)
    return stage if stage in PIPELINE_STAGES else None


def _latest_verdicts(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.get("kind") != "verdict" or event.get("stage") not in PIPELINE_STAGES:
            continue
        detail = event.get("detail") or {}
        latest[str(event["stage"])] = {
            "result": detail.get("result"),
            "marker": detail.get("marker"),
            "at": event.get("at"),
        }
    return latest


def _reached(events: list[dict[str, Any]]) -> set[str]:
    return {
        str(event["stage"])
        for event in events
        if event.get("kind") in _STAGE_EVENT_KINDS and event.get("stage") in PIPELINE_STAGES
    }


def _frame(events: list[dict[str, Any]], live: LiveStage) -> _Frame:
    reached = _reached(events)
    if live.current in PIPELINE_STAGES:
        reached.add(str(live.current))
    ordered = [index for index, stage in enumerate(PIPELINE_STAGES) if stage in reached]
    merged = any(event.get("kind") == "merged" for event in events)
    last_index = max(ordered) if ordered else -1
    if merged and ordered:
        last_index = len(PIPELINE_STAGES) - 1
    return _Frame(
        current=live.current if live.current in PIPELINE_STAGES else None,
        lifecycle=frozenset(live.lifecycle),
        verdicts=_latest_verdicts(events),
        reached=frozenset(reached),
        last_index=last_index,
        merged=merged,
        recorded=bool(events) or live.current in PIPELINE_STAGES,
    )


def _status_of(index: int, stage: str, frame: _Frame) -> str:
    """One stage's status. The order of the checks is the rule: the first that decides wins."""
    if frame.lifecycle and stage not in frame.lifecycle:
        return STATUS_SKIPPED
    if stage == frame.current:
        return STATUS_CURRENT
    verdict = frame.verdicts.get(stage)
    if verdict is not None and verdict.get("result") in _VERDICT_STATUS:
        return _VERDICT_STATUS[str(verdict["result"])]
    if stage in frame.reached:
        return STATUS_DONE if frame.merged or index < frame.last_index else STATUS_RAN
    if frame.merged or index < frame.last_index:
        return STATUS_SKIPPED
    return STATUS_PENDING if frame.recorded else STATUS_NOT_RECORDED


def stage_statuses(
    events: list[dict[str, Any]],
    *,
    current_stage: str | None,
    lifecycle: list[str] | None = None,
) -> dict[str, str]:
    """Each pipeline stage's status. A stage is skipped when the card got past it without running it.

    ``lifecycle`` is the performer stages this deployment runs. A stage outside it is skipped for
    every card. With no lifecycle known, the rule is the card's own events only.
    """
    frame = _frame(events, LiveStage(current=current_stage, lifecycle=tuple(lifecycle or ())))
    return {stage: _status_of(index, stage, frame) for index, stage in enumerate(PIPELINE_STAGES)}


def pipeline_strip(events: list[dict[str, Any]], live: LiveStage) -> dict[str, Any]:
    """The compact form a board card's dots read: the current stage and each stage's status, in order."""
    statuses = stage_statuses(events, current_stage=live.current, lifecycle=list(live.lifecycle))
    return {
        "current": live.current if live.current in PIPELINE_STAGES else None,
        "statuses": [{"stage": stage, "status": statuses[stage]} for stage in PIPELINE_STAGES],
    }


def _run_block(run: dict[str, Any]) -> dict[str, Any]:
    return {
        "run": run.get("run"),
        "stage": run.get("stage"),
        "started_at": run.get("started_at"),
        "ended_at": run.get("ended_at"),
        "duration_seconds": run.get("duration_seconds"),
        "outcome": run.get("outcome"),
        "read_state": run.get("read_state"),
    }


def _runs_of(stage: str, runs: list[dict[str, Any]]) -> tuple[int | None, str | None, float | None, bool, list[dict[str, Any]]]:
    """(run count, source, duration, duration partial, listed runs) for one stage."""
    mine = [run for run in runs if run.get("stage") == stage]
    durations = [run["duration_seconds"] for run in mine if run.get("duration_seconds") is not None]
    duration = round(sum(durations), 3) if mine and durations else None
    return len(mine), "transcripts", duration, bool(mine) and len(durations) < len(mine), [_run_block(run) for run in mine]


def _usage_of(stage: str, usage: UsageLedger) -> tuple[dict[str, Any] | None, str | None]:
    if usage.stages is None:
        return None, usage.reason or "Token and cost figures are not tracked."
    stage_usage = usage.stages.get(stage)
    return stage_usage, None if stage_usage is not None else "No token usage was recorded for this stage."


def _stage_block(stage: str, status: str, sources: _Sources) -> dict[str, Any]:
    dispatches = sum(1 for event in sources.events if event.get("kind") == "dispatched" and event.get("stage") == stage)
    # A count from the events is a lower bound once the cap has dropped any: the repeatable dispatches may be gone.
    at_least = False
    if sources.runs is not None:
        run_count, run_source, duration, duration_partial, listed = _runs_of(stage, sources.runs)
    elif dispatches:
        at_least = sources.dropped > 0
        run_count, run_source, duration, duration_partial, listed = dispatches, "lifecycle", None, False, []
    else:
        run_count, run_source, duration, duration_partial, listed = None, None, None, False, []
    stage_usage, usage_reason = _usage_of(stage, sources.usage)
    return {
        "stage": stage,
        "status": status,
        "run_count": run_count,
        "run_count_source": run_source,
        "run_count_at_least": at_least,
        "duration_seconds": duration,
        "duration_partial": duration_partial,
        "verdict": _latest_verdicts(sources.events).get(stage),
        "usage": stage_usage,
        "usage_reason": usage_reason,
        "runs": listed,
    }


def _not_recorded(events: list[dict[str, Any]], runs: RunIndex, usage: UsageLedger) -> list[str]:
    notes: list[str] = []
    if runs.runs is None:
        notes.append(runs.reason or "Run transcripts are not available, so run counts and durations are not recorded.")
    elif runs.truncated:
        notes.append("Only the newest runs are listed, so older runs are not counted.")
    if usage.stages is None:
        notes.append(usage.reason or "Token and cost figures are not tracked.")
    if not events:
        notes.append("No lifecycle events were recorded for this card. Cards finished before the timeline was recorded have none.")
    return notes


def build_timeline(
    card: CardRef,
    events: list[dict[str, Any]],
    live: LiveStage,
    runs: RunIndex,
    usage: UsageLedger,
) -> dict[str, Any]:
    """The timeline payload for one card.

    ``runs`` is the transcript index for the card, each run already carrying its ``stage``,
    ``started_at``, ``ended_at``, ``duration_seconds`` and ``outcome`` (None when a run has no
    transcript). ``usage`` is the ledger's block per stage (see :class:`UsageLedger`).
    """
    statuses = stage_statuses(events, current_stage=live.current, lifecycle=list(live.lifecycle))
    dropped = int(events[0].get("dropped") or 0) if events else 0
    sources = _Sources(events=events, runs=runs.runs, usage=usage, dropped=dropped)
    stages = [_stage_block(stage, statuses[stage], sources) for stage in PIPELINE_STAGES]
    unassigned = sum(1 for run in (runs.runs or []) if run.get("stage") is None)
    not_recorded = _not_recorded(events, runs, usage)
    if dropped:
        not_recorded.insert(0, f"{dropped} earlier events not shown")
    return {
        "symphony": card.symphony,
        "card_id": card.card_id,
        "current_stage": live.current if live.current in PIPELINE_STAGES else None,
        "merged": any(event.get("kind") == "merged" for event in events),
        "stages": stages,
        "events": sorted(events, key=lambda event: str(event.get("at") or "")),
        "events_not_shown": dropped,
        "runs_available": runs.runs is not None,
        "runs_reason": None if runs.runs is not None else runs.reason,
        "unassigned_runs": unassigned,
        "not_recorded": not_recorded,
    }


__all__ = [
    "PIPELINE_STAGES",
    "CardRef",
    "LiveStage",
    "RunIndex",
    "UsageLedger",
    "build_timeline",
    "pipeline_strip",
    "stage_for_run",
    "stage_statuses",
]

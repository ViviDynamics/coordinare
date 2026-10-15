from __future__ import annotations

import asyncio
import copy
import time
from bisect import bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from coordinare.lifecycle import ROLE_TO_STAGE
from coordinare.services import stats_sources
from coordinare.services.stats_flow import card_flow, instant, lifecycle_records, percentiles
from coordinare.services.token_usage import PriceTable, UsageCounts, price_totals, usage_summary

if TYPE_CHECKING:
    from pathlib import Path

    from coordinare.services.stats_params import StatsQuery
    from coordinare.services.stats_sources import StatsSources
    from coordinare.services.token_usage import ModelTotals

CACHE_SECONDS = 30.0
WINDOW_HOURS = {"24h": 24, "7d": 168, "30d": 720}
OUTCOMES = ("success", "changes_requested", "blocked", "system_error", "cancelled", "unknown")
OUTCOME_MAP = {"completed": "success", "failed": "system_error"}
DIMENSIONS = ("symphony", "stage", "agent", "model", "backend", "outcome")
MAX_REASON_CHARS = 300
DEFINITIONS = {
    "window": "Run start in [start, end]; all usage of those runs. Daily series uses UTC run start day.",
    "agent": "Persona name in the run header. Missing dimensions use null.",
    "model": "Dispatch model; actual usage models are individually priced by the existing ledger rules.",
    "outcome": "Latest lifecycle verdict or terminal event; otherwise transcript result. No result is unknown.",
    "success_rate": "Successful runs / runs with known terminal outcomes. Unknown excluded.",
    "first_pass_rate": "Successful card/stage pairs with exactly one retained run / resolved card/stage pairs. Attributed to latest run.",
    "retries": "Runs after the first retained run of the same symphony, card and stage, including prior window context.",
    "bounces": "Runs with changes_requested or a recorded CI BOUNCE, counted once per run.",
    "runs_per_card": "Runs / distinct (symphony, card) in the group.",
    "duration_seconds": "Header start to final transcript timestamp; negative or missing durations excluded. Linear percentiles.",
    "cost": "Existing PriceTable and TokenLedger precedence; usd is null if usage is missing or any model is unpriced.",
    "cost_precision": "usd is rounded to microdollars for display. Sum Decimal(usd_exact) for exact reconciliation across groups; partial_usd_exact contains known costs.",
    "card_flow": "Lifecycle observations per symphony, independent of run dimension filters. Merge time selects completed cards and cycles.",
    "cycle_time_seconds": "First dispatch of a card lifecycle to its merge, including dispatches before the window.",
    "human_review_wait_seconds": "Existing human versus automation wait classification. Recorded human intervals end at human_review_ended, human review, redispatch or merge and are clipped to the window. Null without observations.",
    "revision_rounds_per_pr": "CHANGES_REQUESTED review observations / distinct PRs in lifecycles active in the window, including PRs opened earlier; null with no PR observations.",
    "coverage": "Retained observations only. Partial reasons expose read bounds and unavailable sources; old unrecorded history cannot be inferred.",
}


@dataclass(frozen=True)
class StatsFrame:
    visible: frozenset[str]
    lifecycle: dict[str, Any]
    boards: dict[str, Any]
    now: datetime
    table: PriceTable


def _decision(event: dict[str, Any]) -> tuple[datetime, str, str] | None:
    detail = event.get("detail") or {}
    at = instant(event.get("at"))
    if at is None:
        return None
    if event["kind"] == "verdict" and detail.get("result") in {"pass", "fail"}:
        outcome = "success" if detail["result"] == "pass" else "changes_requested"
        return at, outcome, str(detail.get("marker") or outcome)
    if event["kind"] == "run_outcome" and detail.get("outcome") in OUTCOMES:
        return at, detail["outcome"], str(detail.get("reason") or detail["outcome"])
    return None


def _attach_observations(attempts: list[dict[str, Any]], events: list[dict[str, Any]]) -> None:
    """Assign each observation once, using identity first and binary search otherwise."""
    attempts.sort(key=lambda row: (row["started_at"], row["run"]))
    starts = [row["started_at"] for row in attempts]
    by_id = {row["run"]: row for row in attempts}
    decisions: dict[str, tuple[datetime, str, str]] = {}
    for event in events:
        at = instant(event.get("at"))
        if at is None:
            continue
        run_id = (event.get("detail") or {}).get("run_id")
        row = by_id.get(run_id)
        if row is None:
            if run_id:
                continue
            index = bisect_right(starts, at.isoformat()) - 1
            if index < 0:
                continue
            row = attempts[index]
        detail = event.get("detail") or {}
        if event["kind"] == "ci_result" and detail.get("result") == "BOUNCE":
            row["bounce"] = True
        decision = _decision(event)
        if decision is not None:
            previous = decisions.get(row["run"])
            if previous is None or decision[0] >= previous[0]:
                decisions[row["run"]] = decision
    for index, row in enumerate(attempts):
        if row["run"] in decisions:
            _, row["outcome"], row["reason"] = decisions[row["run"]]
        row["retry"] = index > 0
        row["bounce"] = row["bounce"] or row["outcome"] == "changes_requested"
        row["resolved_pair"] = index == len(attempts) - 1 and row["outcome"] != "unknown"
        row["first_pass"] = row["resolved_pair"] and len(attempts) == 1 and row["outcome"] == "success"


def _prepare(
    sources: StatsSources, lifecycle: dict[tuple[str, str], list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    runs = []
    by_card_stage: dict[tuple[str, str, str | None], list[dict[str, Any]]] = defaultdict(list)
    for source in sources.runs:
        recorded_stage = source.get("stage")
        stage = (
            ROLE_TO_STAGE.get(recorded_stage, recorded_stage)
            if isinstance(recorded_stage, str)
            else None
        )
        row = {
            **source,
            "symphony": source["attributed_symphony"],
            "stage": stage,
            "agent": source.get("persona"),
            "backend": source.get("agent"),
            "outcome": OUTCOME_MAP.get(source["outcome"], "unknown"),
            "reason": source.get("outcome_text"),
            "bounce": False,
        }
        began = instant(row["started_at"])
        if began is not None:
            row["started_at"] = began.isoformat()
        runs.append(row)
        by_card_stage[row["symphony"], row["card"], stage].append(row)
    terminal_events: dict[tuple[str, str, str | None], list[dict[str, Any]]] = defaultdict(list)
    for event in sources.events:
        if event["kind"] in OUTCOMES:
            terminal_events[event["symphony"], event["card_id"], event["stage"]].append({
                "kind": "run_outcome", "at": event["ts"],
                "detail": {"run_id": event["run_id"], "outcome": event["kind"], "reason": event["message"]},
            })
    for (symphony, card, stage), attempts in by_card_stage.items():
        observations = [
            event for event in lifecycle.get((symphony, card), []) if event.get("stage") == stage
        ]
        observations.extend(terminal_events.get((symphony, card, stage), []))
        _attach_observations(attempts, observations)
    return runs


def _measures(
    rows: list[dict[str, Any]], usage: dict[str, ModelTotals], table: PriceTable, top_n: int,
) -> dict[str, Any]:
    outcomes = Counter(row["outcome"] for row in rows)
    known = len(rows) - outcomes["unknown"]
    resolved = sum(row["resolved_pair"] for row in rows)
    totals: ModelTotals = {}
    missing = 0
    for row in rows:
        run_usage = usage.get(row["run"])
        if not run_usage:
            missing += 1
        for model, counts in (run_usage or {}).items():
            totals[model] = totals.get(model, UsageCounts()) + counts
    summary = usage_summary(totals, table)
    priced = price_totals(totals, table)
    summary["cost"]["usd_exact"] = str(priced.cost_usd) if priced.cost_usd is not None else None
    summary["cost"]["partial_usd_exact"] = str(priced.partial_usd)
    if missing:
        summary["cost"]["usd"] = None
        summary["cost"]["usd_exact"] = None
        summary["cost"]["unknown_reason"] = "Some runs have no complete usage records"
    reasons = Counter(
        str(row.get("reason") or row["outcome"])[:MAX_REASON_CHARS]
        for row in rows
        if row["outcome"] not in {"success", "unknown"}
    )
    cards = {(row["symphony"], row["card"]) for row in rows}
    return {
        "runs": len(rows),
        "outcomes": {outcome: outcomes[outcome] for outcome in OUTCOMES},
        "success_rate": outcomes["success"] / known if known else None,
        "first_pass_rate": sum(row["first_pass"] for row in rows) / resolved if resolved else None,
        "resolved_card_stages": resolved,
        "runs_per_card": len(rows) / len(cards) if cards else None,
        "retries": sum(row["retry"] for row in rows),
        "bounces": sum(row["bounce"] for row in rows),
        "duration_seconds": percentiles(
            [row["duration_seconds"] for row in rows if row["duration_seconds"] is not None],
        ),
        "last_run_at": max((row["started_at"] for row in rows), default=None),
        "tokens": summary["tokens"],
        "cost": summary["cost"],
        "pricing": summary["pricing"],
        "missing_usage_runs": missing,
        "failure_reasons": [
            {"reason": reason, "runs": count}
            for reason, count in sorted(reasons.items(), key=lambda item: (-item[1], item[0]))[
                :top_n
            ]
        ],
    }


def aggregate(
    sources: StatsSources,
    params: StatsQuery,
    frame: StatsFrame,
) -> dict[str, Any]:
    visible, now, table = frame.visible, frame.now, frame.table
    scope = frozenset({params.symphony}) if params.symphony is not None else visible
    lifecycle = lifecycle_records(frame.lifecycle, sources.events, scope)
    runs = [row for row in _prepare(sources, lifecycle) if row["symphony"] in scope]
    start = now - timedelta(hours=WINDOW_HOURS[params.window]) if params.window != "all" else None
    selected = []
    for row in runs:
        began = instant(row["started_at"])
        if began is None or began > now or (start is not None and began < start):
            continue
        if any(
            getattr(params, dim) is not None and getattr(params, dim) != row[dim]
            for dim in DIMENSIONS
        ):
            continue
        selected.append(row)
    daily: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in selected:
        daily[row["started_at"][: len("YYYY-MM-DD")]].append(row)
    reasons = sorted(set(sources.reasons))
    if any(event.get("dropped") for events in lifecycle.values() for event in events):
        reasons = sorted({*reasons, "lifecycle_events_dropped"})
    return {
        "group_by": params.group_by,
        "window": params.window,
        "start": start.isoformat() if start is not None else None,
        "end": now.isoformat(),
        "groups": _group_measures(selected, params, sources.usage, table),
        "totals": _measures(selected, sources.usage, table, params.top_n),
        "daily": [
            {
                "day": day,
                **_measures(rows, sources.usage, table, params.top_n),
                "groups": _group_measures(rows, params, sources.usage, table),
            }
            for day, rows in sorted(daily.items())
        ],
        "card_flow": card_flow(lifecycle, frame.boards, scope, start, now),
        "partial": bool(reasons),
        "partial_reasons": reasons,
        "coverage": {
            "retained_runs_read": len(runs),
            "usage_runs_read": sum(row["run"] in sources.usage for row in runs),
            "lifecycle_cards": len(lifecycle),
            "oldest_run_at": min((row["started_at"] for row in runs), default=None),
            "human_wait_recording": "Only intervals with a recorded human_review_started boundary are measured.",
        },
        "definitions": DEFINITIONS,
        "cache_ttl_seconds": CACHE_SECONDS,
    }


def _group_measures(
    rows: list[dict[str, Any]], params: StatsQuery, usage: dict[str, ModelTotals], table: PriceTable,
) -> list[dict[str, Any]]:
    groups: dict[str | None, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[row[params.group_by]].append(row)
    return [
        {"key": key, **_measures(group, usage, table, params.top_n)}
        for key, group in sorted(groups.items(), key=lambda item: (item[0] is None, item[0] or ""))
    ]


class RunStatistics:
    """One bounded source cache shared by all dashboard and MCP statistics reads."""

    def __init__(
        self, root: Path | None, *, event_path: Path | None = None, table: PriceTable | None = None,
    ) -> None:
        self.root = root
        self.event_path = event_path
        self.table = table or PriceTable()
        self._lock = asyncio.Lock()
        self._sources: StatsSources | None = None
        self._visible: frozenset[str] = frozenset()
        self._expires = 0.0

    async def query(
        self,
        params: StatsQuery,
        *,
        visible: frozenset[str],
        lifecycle: dict[str, Any],
        boards: dict[str, Any],
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if params.symphony is not None and params.symphony not in visible:
            message = "no symphony with that name on this daemon"
            raise LookupError(message)
        now = now or datetime.now(UTC)
        async with self._lock:
            if (
                self._sources is None
                or self._visible != visible
                or time.monotonic() >= self._expires
            ):
                self._sources = await asyncio.to_thread(
                    stats_sources.load_sources, self.root, self.event_path, visible, now.isoformat(),
                )
                self._visible = visible
                self._expires = time.monotonic() + CACHE_SECONDS
            sources = self._sources
        frame = StatsFrame(visible, copy.deepcopy(lifecycle), copy.deepcopy(boards), now, self.table)
        return await asyncio.to_thread(aggregate, sources, params, frame)

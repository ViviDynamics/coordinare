from __future__ import annotations

import asyncio
import copy
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from coordinare.graph.nodes.monitor.constants import VERDICT_STAGES
from coordinare.lifecycle import ROLE_TO_STAGE
from coordinare.services import stats_sources
from coordinare.services.event_store_query import QUERY_TIMEOUT_SECONDS
from coordinare.services.stats_flow import (
    card_flow,
    instant,
    lifecycle_records,
)
from coordinare.services.stats_measures import OUTCOMES, _measures
from coordinare.services.stats_observability import reliability
from coordinare.services.stats_persona import (
    _persona_details,
    _persona_edits,
    _recent_runs,
    _shared_persona_edits,
)
from coordinare.services.stats_runtime import persona_runtime
from coordinare.services.token_usage import PriceTable

if TYPE_CHECKING:
    from pathlib import Path

    from coordinare.services.stats_params import StatsQuery
    from coordinare.services.stats_sources import StatsSources
    from coordinare.services.token_usage import ModelTotals

CACHE_SECONDS = 30.0
WINDOW_HOURS = {"24h": 24, "7d": 168, "30d": 720}
OUTCOME_MAP = {"completed": "completed_without_verdict", "failed": "system_error"}
DIMENSIONS = ("symphony", "stage", "agent", "model", "backend", "outcome")
MILLISECONDS_PER_SECOND = 1000
DEFINITIONS = {
    "window": "Run start in [start, end]; all usage of those runs. Daily series uses UTC run start day.",
    "agent": "Persona name in the run header. Missing dimensions use null.",
    "model": "Dispatch model; actual usage models are individually priced by the existing ledger rules.",
    "outcome": "Latest lifecycle verdict or terminal event. Verdict pass/fail is success/failure. Transcript completion alone is completed_without_verdict; transcript error is system_error. Missing result is unknown.",
    "success_rate": "Passing verdict runs / runs with a pass or fail verdict in a verdict stage. Completion and terminal errors without verdicts are excluded; null with no verdicts.",
    "verdict_stages": ", ".join(sorted(VERDICT_STAGES)),
    "verdict_runs": "Runs with an attributed pass or fail verdict in a verdict stage, regardless of completion or later terminal events.",
    "resolved_card_stages": "Card/stage pairs whose latest retained run has an attributed verdict in a verdict stage.",
    "first_pass_rate": "Passing verdict card/stage pairs with exactly one retained run / card/stage pairs whose latest run has a verdict in a verdict stage. Attributed to that latest run; null with no resolved pairs.",
    "retries": "Runs after the first retained run of the same symphony, card and stage, including prior window context.",
    "bounces": "Runs with a failing verdict or a recorded CI BOUNCE, counted once per run.",
    "runs_per_card": "Runs / distinct (symphony, card) in the group.",
    "duration_seconds": "Header start to final transcript timestamp; negative or missing durations excluded. Linear percentiles.",
    "cost": "Indexed event store usage aggregated per run and model in SQL, with TokenLedger scope precedence and PriceTable pricing. Transcript fallback covers runs predating retained event coverage, runs with dropped or partially pruned usage events, and runs with no event store. usd is null if usage is missing or any model is unpriced.",
    "usage_read_bounds": f"At most 2000 runs and 10000 run/model groups, in batches of 128 runs. Each SQLite statement has a {QUERY_TIMEOUT_SECONDS * MILLISECONDS_PER_SECOND:g} ms deadline and all usage batches share a one second deadline. Legacy transcripts share a 16 MiB usage byte budget.",
    "cost_per_run_usd": "Total priced USD / runs in the cohort; null for missing usage, unpriced models or an empty cohort.",
    "shared_persona_edits": "Shared retained persona history metadata in the window, including edits after the latest run. Symphony attribution is not recorded; these are not symphony run statistics.",
    "persona_versions": "Recorded run header version and content hash, separated by symphony. Missing identities stay null. Edit markers use recorded changed_at, not the run start.",
    "recent_runs": "Newest ten retained runs in the same filtered start cohort, with the same accounting and outcome rules as totals.",
    "cost_precision": "usd is rounded to microdollars for display. Sum Decimal(usd_exact) for exact reconciliation across groups; partial_usd_exact contains known costs.",
    "card_flow": "Lifecycle observations per symphony, independent of run dimension filters. Merge time selects completed cards and cycles.",
    "daily_completed": "UTC daily merge events, using the same merge time cohort as cards_completed.",
    "recent_completed": "Last 10 recorded card merges in the selected window, newest first; reopened cards can appear more than once.",
    "cost_per_completed_card_usd": "Run start cohort cost divided by merge cohort cards_completed. The cohorts may differ. Null with no completed cards or incomplete cost or source coverage.",
    "symphony_health": "Current symphony identity, state and environment metadata, independent of selected window. Slot occupancy reuses the scheduler rules.",
    "reliability": "Successful recorded upstream board polls divided by recorded polls in the window. Cache hits and backoff waits excluded; legacy checks are not recorded. Recent errors retain their observation timestamps.",
    "cycle_time_seconds": "First dispatch of a card lifecycle to its merge, including dispatches before the window.",
    "human_review_wait_seconds": "Existing human versus automation wait classification. Recorded human intervals end at human_review_ended, human review, redispatch or merge and are clipped to the window. Only positive overlaps contribute seconds and samples. Null without overlapping observations.",
    "revision_rounds_per_pr": "CHANGES_REQUESTED review observations / distinct PRs in lifecycles active in the window, including PRs opened earlier; null with no PR observations.",
    "coverage": "Retained observations only. Legacy observations without run_id require exactly one open run of the same card and normalized stage at the event time; ambiguous, unmatched and missing identities are counted as unattributed_observations. Partial reasons expose read bounds and unavailable sources.",
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
        outcome = "success" if detail["result"] == "pass" else "failure"
        return at, outcome, str(detail.get("marker") or outcome)
    if event["kind"] == "run_outcome" and detail.get("outcome") in {"blocked", "system_error", "cancelled", "completed_without_verdict"}:
        return at, detail["outcome"], str(detail.get("reason") or detail["outcome"])
    return None


def _normal_stage(stage: object) -> str | None:
    return ROLE_TO_STAGE.get(stage, stage) if isinstance(stage, str) else None


def _closed_at(row: dict[str, Any]) -> datetime | None:
    began = instant(row["started_at"])
    duration = row.get("duration_seconds")
    if began is not None and row["outcome"] != "unknown" and duration is not None and duration >= 0:
        return began + timedelta(seconds=duration)
    return None


def _observation_row(
    attempts: list[dict[str, Any]], by_id: dict[str, dict[str, Any]],
    event: dict[str, Any], at: datetime,
) -> dict[str, Any] | None:
    run_id = (event.get("detail") or {}).get("run_id")
    if run_id:
        return by_id.get(run_id)
    opened = []
    for row in attempts:
        began = instant(row["started_at"])
        if began is None or began > at:
            continue
        ended = row["closed_at"]
        if ended is None or at <= ended:
            opened.append(row)
    return opened[0] if len(opened) == 1 else None


def _attach_observations(attempts: list[dict[str, Any]], events: list[dict[str, Any]]) -> int:
    """Use identity first; legacy observations require one open card/stage run."""
    attempts.sort(key=lambda row: (row["started_at"], row["run"]))
    by_id = {row["run"]: row for row in attempts}
    decisions: dict[str, tuple[datetime, str, str]] = {}
    unattributed = 0
    for event in sorted(events, key=lambda event: str(event.get("at") or "")):
        at = instant(event.get("at"))
        if at is None:
            continue
        row = _observation_row(attempts, by_id, event, at)
        if row is None:
            unattributed += 1
            continue
        detail = event.get("detail") or {}
        if event["kind"] == "ci_result" and detail.get("result") == "BOUNCE":
            row["bounce"] = True
        decision = _decision(event)
        if event["kind"] == "verdict":
            if row["stage"] not in VERDICT_STAGES:
                continue
            if decision is not None:
                row["verdict"] = decision[1]
        if decision is not None:
            previous = decisions.get(row["run"])
            if previous is None or decision[0] >= previous[0]:
                decisions[row["run"]] = decision
            row["closed_at"] = min(row["closed_at"] or at, at)
    for index, row in enumerate(attempts):
        if row["run"] in decisions:
            _, row["outcome"], row["reason"] = decisions[row["run"]]
        row["retry"] = index > 0
        row["bounce"] = row["bounce"] or row["verdict"] == "failure"
        row["resolved_pair"] = index == len(attempts) - 1 and row["verdict"] is not None
        row["first_pass"] = row["resolved_pair"] and len(attempts) == 1 and row["verdict"] == "success"
    return unattributed


def _prepare(
    sources: StatsSources, lifecycle: dict[tuple[str, str], list[dict[str, Any]]],
    scope: frozenset[str],
) -> tuple[list[dict[str, Any]], int]:
    runs = []
    by_card_stage: dict[tuple[str, str, str | None], list[dict[str, Any]]] = defaultdict(list)
    for source in sources.runs:
        if source["attributed_symphony"] not in scope:
            continue
        stage = _normal_stage(source.get("stage"))
        row = {
            **source,
            "transcript_symphony": source["symphony"],
            "symphony": source["attributed_symphony"],
            "stage": stage,
            "agent": source.get("persona"),
            "backend": source.get("agent"),
            "outcome": OUTCOME_MAP.get(source["outcome"], "unknown"),
            "reason": source.get("outcome_text"),
            "bounce": False,
            "verdict": None,
        }
        began = instant(row["started_at"])
        if began is not None:
            row["started_at"] = began.isoformat()
        row["closed_at"] = _closed_at(row)
        runs.append(row)
        by_card_stage[row["symphony"], row["card"], stage].append(row)
    observations: dict[tuple[str, str, str | None], list[dict[str, Any]]] = defaultdict(list)
    for (symphony, card), events in lifecycle.items():
        for event in events:
            if event["kind"] in {"verdict", "ci_result", "run_outcome"}:
                observations[symphony, card, _normal_stage(event.get("stage"))].append(event)
    for event in sources.events:
        if event["kind"] in OUTCOMES and event["symphony"] in scope:
            observations[event["symphony"], event["card_id"], _normal_stage(event["stage"])].append({
                "kind": "run_outcome", "at": event["ts"],
                "detail": {"run_id": event["run_id"], "outcome": event["kind"], "reason": event["message"]},
            })
    unattributed = sum(
        _attach_observations(by_card_stage.get(key, []), observations.get(key, []))
        for key in by_card_stage.keys() | observations.keys()
    )
    return runs, unattributed


def aggregate(
    sources: StatsSources,
    params: StatsQuery,
    frame: StatsFrame,
) -> dict[str, Any]:
    visible, now, table = frame.visible, frame.now, frame.table
    scope = frozenset({params.symphony}) if params.symphony is not None else visible
    lifecycle = lifecycle_records(frame.lifecycle, sources.events, scope)
    runs, unattributed = _prepare(sources, lifecycle, scope)
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
    edits = _persona_edits(selected, start, now) if params.agent else {}
    flow = card_flow(lifecycle, frame.boards, scope, start, now)
    symphony_groups = []
    for name, block in flow.items():
        measures = _measures([row for row in selected if row["symphony"] == name], sources.usage, table, params.top_n)
        symphony_groups.append({"key": name, **measures})
        cost = measures["cost"]["usd"]
        block["cost_per_completed_card_usd"] = (
            cost / block["cards_completed"]
            if not reasons and cost is not None and block["cards_completed"] else None
        )
    return {
        "group_by": params.group_by,
        "window": params.window,
        "start": start.isoformat() if start is not None else None,
        "end": now.isoformat(),
        "groups": symphony_groups if params.group_by == "symphony" and not params.agent else _group_measures(selected, params, sources.usage, table),
        "totals": _measures(selected, sources.usage, table, params.top_n),
        **(_persona_details(selected, sources, params, frame, edits) if params.agent else {}),
        "daily": [
            {
                "day": day,
                **({"persona_edits": edits.get(day, [])} if params.agent else {}),
                **_measures(rows, sources.usage, table, params.top_n),
                "groups": _group_measures(rows, params, sources.usage, table),
            }
            for day, rows in sorted(daily.items())
        ],
        "card_flow": flow,
        "reliability": reliability(sources.events, scope, start, now),
        "recent_runs": _recent_runs(selected, sources, params, table),
        "partial": bool(reasons),
        "partial_reasons": reasons,
        "coverage": {
            "retained_runs_read": len(runs),
            "unattributed_observations": unattributed,
            "usage_runs_read": sum(row["run"] in sources.usage for row in runs),
            "usage_events_dropped_runs": sum(row["run"] in sources.usage_coverage.dropped_runs for row in runs),
            "usage_partially_pruned_runs": sum(row["run"] in sources.usage_coverage.pruned_runs for row in runs),
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
        persona_root: Path | None = None,
    ) -> None:
        self.root = root
        self.persona_root = persona_root
        self.event_path = event_path
        self.table = table or PriceTable()
        self._lock = asyncio.Lock()
        self._sources: StatsSources | None = None
        self._visible: frozenset[str] = frozenset()
        self._expires = 0.0

    persona_runtime = staticmethod(persona_runtime)

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
                    copy.deepcopy(lifecycle),
                )
                self._visible = visible
                self._expires = time.monotonic() + CACHE_SECONDS
            sources = self._sources
        frame = StatsFrame(visible, copy.deepcopy(lifecycle), copy.deepcopy(boards), now, self.table)
        body = await asyncio.to_thread(aggregate, sources, params, frame)
        if params.agent:
            shared_edits = await asyncio.to_thread(
                _shared_persona_edits, self.persona_root, params.agent, instant(body["start"]), now,
            )
            seen = {(edit["version"], instant(edit["changed_at"])) for edit in body["persona_edits"]}
            body["shared_persona_edits"] = []
            for edit in shared_edits:
                key = (edit["version"], instant(edit["changed_at"]))
                if key not in seen:
                    body["shared_persona_edits"].append(edit)
                    seen.add(key)
        return body

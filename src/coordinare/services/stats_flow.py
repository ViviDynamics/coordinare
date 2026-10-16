from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, datetime
from math import ceil, floor
from typing import Any

from coordinare.services.stats_sources import LIFECYCLE_KIND

P90 = 0.9
MEDIAN = 0.5
ROUND_DIGITS = 3
RECENT_LIMIT = 10


def instant(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None


def percentiles(values: list[float]) -> dict[str, Any]:
    ordered = sorted(value for value in values if value >= 0)
    result: dict[str, Any] = {"median": None, "p90": None, "samples": len(ordered)}
    if not ordered:
        return result
    for name, fraction in (("median", MEDIAN), ("p90", P90)):
        index = (len(ordered) - 1) * fraction
        lower, upper = floor(index), ceil(index)
        result[name] = round(
            ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower), ROUND_DIGITS,
        )
    return result


def lifecycle_records(
    snapshot: dict[str, Any],
    events: list[dict[str, Any]],
    visible: frozenset[str],
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Union snapshot and SQLite observations without counting their overlap twice."""
    collected: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for symphony in sorted(visible):
        for card, records in snapshot.get(symphony, {}).items():
            collected[symphony, card].extend(records)
    for event in events:
        if event["kind"] == LIFECYCLE_KIND and event["symphony"] in visible:
            collected[event["symphony"], event["card_id"]].append(event["data"])
    for key, records in collected.items():
        unique = {
            (
                record.get("at"),
                record.get("kind"),
                record.get("stage"),
                json.dumps(record.get("detail") or {}, sort_keys=True),
            ): record
            for record in records
            if instant(record.get("at")) is not None
        }
        collected[key] = sorted(unique.values(), key=lambda record: instant(record["at"]) or datetime.min.replace(tzinfo=UTC))
    return dict(collected)


def _closes_wait(kind: object, detail: dict[str, Any]) -> bool:
    return kind in {"merged", "dispatched", "human_review_ended"} or (
        kind == "review_received" and str(detail.get("reviewer") or "").lower() == "human"
    )


def _episodes(records: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """A reopened card starts a fresh cycle after its previous merge."""
    episodes: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for record in records:
        current.append(record)
        if record.get("kind") == "merged":
            episodes.append(current)
            current = []
    if current:
        episodes.append(current)
    return episodes


def _wait_overlap(began: datetime, closed: datetime, start: datetime | None) -> tuple[float, int]:
    seconds = max(0.0, (closed - max(began, start or began)).total_seconds())
    return seconds, int(seconds > 0)


def _flow_one(
    cards: list[list[dict[str, Any]]],
    start: datetime | None,
    end: datetime,
) -> dict[str, Any]:
    completed = 0
    cycles: list[float] = []
    wait_seconds = 0.0
    waits = 0
    revisions = 0
    prs: set[str] = set()
    for records in cards:
        began: datetime | None = None
        waiting: datetime | None = None
        observed_prs: set[str] = set()
        active = False
        for record in records:
            at = instant(record.get("at"))
            if at is None or at > end:
                continue
            kind = record.get("kind")
            detail = record.get("detail") or {}
            in_window = start is None or at >= start
            active = active or in_window
            if kind == "dispatched" and began is None:
                began = at
            if kind == "human_review_started":
                waiting = waiting or at
            if waiting is not None and _closes_wait(kind, detail):
                seconds, samples = _wait_overlap(waiting, at, start)
                wait_seconds += seconds
                waits += samples
                waiting = None
            if kind == "pr_opened":
                observed_prs.add(str(detail.get("pr_url") or detail.get("pr_number") or record.get("at")))
            if (
                kind == "review_received"
                and in_window
                and detail.get("review_state") == "CHANGES_REQUESTED"
            ):
                revisions += 1
            if kind == "merged":
                completed += int(in_window)
                if in_window and began is not None:
                    cycles.append((at - began).total_seconds())
                began = None
        if waiting is not None:
            seconds, samples = _wait_overlap(waiting, end, start)
            wait_seconds += seconds
            waits += samples
        if active or waiting is not None:
            prs.update(observed_prs)
    return {
        "cards_completed": completed,
        "prs_merged": completed,
        "cycle_time_seconds": percentiles(cycles),
        "human_review_wait_seconds": round(wait_seconds, ROUND_DIGITS) if waits else None,
        "human_review_wait_samples": waits,
        "revision_rounds": revisions,
        "prs_observed": len(prs),
        "revision_rounds_per_pr": revisions / len(prs) if prs else None,
    }


def card_flow(
    records: dict[tuple[str, str], list[dict[str, Any]]],
    boards: dict[str, Any],
    visible: frozenset[str],
    start: datetime | None,
    end: datetime,
) -> dict[str, Any]:
    result = {}
    for symphony in sorted(visible):
        cards = [events for (owner, _), events in records.items() if owner == symphony]
        block = _flow_one(
            [episode for events in cards for episode in _episodes(events)], start, end,
        )
        board = boards.get(symphony)
        block["columns_now"] = (
            {str(column): len(cards) for column, cards in board.items() if isinstance(cards, list)}
            if isinstance(board, dict)
            else None
        )
        completed = [
            {"card": card, "merged_at": event["at"]}
            for (owner, card), events in records.items() if owner == symphony
            for event in events
            if event.get("kind") == "merged"
            and (at := instant(event.get("at"))) is not None
            and at <= end and (start is None or at >= start)
        ]
        daily: dict[str, int] = defaultdict(int)
        for item in completed:
            at = instant(item["merged_at"])
            if at is not None:
                daily[at.date().isoformat()] += 1
        block["daily_completed"] = [
            {"day": day, "cards_completed": count} for day, count in sorted(daily.items())
        ]
        block["recent_completed"] = sorted(
            completed, key=lambda item: instant(item["merged_at"]) or end, reverse=True,
        )[:RECENT_LIMIT]
        result[symphony] = block
    return result

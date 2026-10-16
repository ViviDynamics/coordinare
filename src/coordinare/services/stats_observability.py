from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from coordinare.graph.nodes.check_board import _count_slot_consuming_sessions
from coordinare.services.pipeline_budget import has_live_work, is_parked_in_review
from coordinare.services.stats_flow import RECENT_LIMIT, instant
from coordinare.services.stats_poll import BOARD_CHECK_KIND

if TYPE_CHECKING:
    from datetime import datetime

    from coordinare.graph.state import CoordinareState


def live_health(entries: dict[str, Any], scope: frozenset[str]) -> dict[str, Any]:
    result = {}
    for name in sorted(scope):
        if name not in entries:
            continue
        entry = entries[name]
        state = entry.get("state") or {}
        sessions = state.get("active_sessions") or {}
        running_cards = [card for card, session in sessions.items() if has_live_work(session)]
        result[name] = {
            **entry,
            "cards_active": len(running_cards),
            "running_card_ids": running_cards,
            "cards_queued": sum(
                wait.get("reason") == "queued" and card not in sessions
                for card, wait in (state.get("todo_waits") or {}).items()
            ) + sum(bool(session.get("slot_queued_since")) for session in sessions.values()),
            "cards_parked": sum(is_parked_in_review(session) for session in sessions.values()),
            "slots_in_use": _count_slot_consuming_sessions(cast("CoordinareState", state)),
        }
    return result


def reliability(
    events: list[dict[str, Any]], scope: frozenset[str], start: datetime | None, end: datetime,
) -> dict[str, Any]:
    result = {}
    for name in sorted(scope):
        checks = [
            row for row in events if row["symphony"] == name and row["kind"] == BOARD_CHECK_KIND
            and (at := instant(row.get("ts"))) is not None
            and at <= end and (start is None or at >= start)
        ]
        result[name] = {
            "checks": len(checks),
            "success_rate": sum(row["data"].get("success") is True for row in checks) / len(checks) if checks else None,
            "recent_errors": [
                {"at": row["ts"], "reason": row["message"]}
                for row in sorted(checks, key=lambda row: row["ts"], reverse=True)
                if row["data"].get("success") is False
            ][:RECENT_LIMIT],
        }
    return result

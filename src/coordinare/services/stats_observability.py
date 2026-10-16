from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from coordinare.services.pipeline_budget import (
    count_slot_consuming_sessions,
    has_live_work,
    is_parked_in_review,
)
from coordinare.services.stats_flow import RECENT_LIMIT, instant
from coordinare.services.stats_poll import BOARD_CHECK_KIND

if TYPE_CHECKING:
    from datetime import datetime

    from coordinare.graph.state import CoordinareState


# Only metadata consumed by symphony_stats.js and the shared status/identity helpers.
HEALTH_FIELDS = (
    "name", "enabled", "github_project_number", "github_org", "repo", "last_poll_at",
    "poll_interval_seconds", "daemon_cycles_completed", "cycle_count", "error_count",
    "cards_paused", "idle_reason", "merge_review", "env_status", "env_bootstrap",
    "max_concurrent_cards", "env_bootstrap_performer_id", "bootstrap_in_flight",
)
HEALTH_STATE_FIELDS = (
    "last_error", "board_snapshot", "board_titles", "todo_waits", "session_skip_reasons", "todo_capacity",
)


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
            **{key: entry[key] for key in HEALTH_FIELDS if key in entry},
            "state": {
                **{key: state[key] for key in HEALTH_STATE_FIELDS if key in state},
                "active_sessions": {
                    card: {key: session[key] for key in ("block_cause", "review_wait_reason") if key in session}
                    for card, session in sessions.items()
                },
            },
            "cards_active": len(running_cards),
            "running_card_ids": running_cards,
            "cards_queued": sum(
                wait.get("reason") == "queued" and card not in sessions
                for card, wait in (state.get("todo_waits") or {}).items()
            ) + sum(bool(session.get("slot_queued_since")) for session in sessions.values()),
            "cards_parked": sum(is_parked_in_review(session) for session in sessions.values()),
            "slots_in_use": count_slot_consuming_sessions(cast("CoordinareState", state)),
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

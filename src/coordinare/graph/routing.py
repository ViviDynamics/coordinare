from __future__ import annotations

from typing import Any


def route_from_board_check(state: dict[str, Any]) -> str:
    if state.get("stale_session_reconciliation_deferred"):
        return "idle"  # retain session state; retry reconciliation next tick
    phase = state.get("phase", "idle")
    if phase == "dispatching":
        # Feedback or an override already selected a performer stage. Re-assessing a card
        # with an open PR would send it back to monitoring before the selected
        # performer can address the review (#545).
        if (
            state.get("relay_feedback") or state.get("pending_override")
            or state.get("reconciled_dispatch_pending")
        ):
            return "dispatch"
        # When the lifecycle includes "assessing", the assessor runs as a
        # proper performer via dispatch_performer → monitor_performer.  Skip
        # the legacy assess_card node and go straight to dispatch.
        lifecycle: list[str] = list(state.get("lifecycle_sequence") or [])
        if "assessing" in lifecycle:
            return "dispatch"
        return "assess"
    if phase == "monitoring_pr":
        return "monitor_pr"
    if phase in ("monitoring_agent", "monitoring_performer"):
        return "monitor_agent"
    if phase == "blocked":
        return "blocked"
    if phase == "system_error":
        return "handle_system_error"
    return "idle"


def route_from_review(state: dict[str, object]) -> str:
    phase = state.get("phase", "monitoring_pr")
    if phase == "merging":
        return "merge"
    if phase == "blocked":
        return "blocked"
    if state.get("pending_reviews"):
        return "relay"
    return "monitor"


def route_from_assess(state: dict[str, object]) -> str:
    phase = state.get("phase", "dispatching")
    if phase == "blocked":
        return "blocked"
    if phase == "monitoring_pr":
        # 073 US6: assess_card short-circuited because the card already has an
        # open PR — hand off to the PR monitor instead of dispatching.
        return "monitor_pr"
    return "dispatch"


def route_from_dispatch(state: dict[str, object]) -> str:
    phase = state.get("phase", "monitoring_agent")
    if phase == "blocked":
        return "blocked"
    if phase == "system_error":
        return "handle_system_error"
    return "notify"


def route_from_system_error(state: dict[str, object]) -> str:
    phase = state.get("phase", "idle")
    if phase == "dispatching":
        return "dispatch"
    return "idle"


def route_from_agent_status(state: dict[str, object]) -> str:
    phase = state.get("phase", "monitoring_agent")
    if phase == "monitoring_pr":
        return "review"
    if phase == "blocked":
        return "blocked"
    if phase == "system_error":
        return "handle_system_error"
    if phase == "dispatching":
        return "dispatch"
    return "monitor"

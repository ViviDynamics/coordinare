from __future__ import annotations


def route_from_board_check(state: dict[str, object]) -> str:
    phase = state.get("phase", "idle")
    if phase == "dispatching":
        return "dispatch"
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

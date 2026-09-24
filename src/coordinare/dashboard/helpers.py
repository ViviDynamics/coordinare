"""Pure helpers shared by the dashboard package (436)."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog

from coordinare.services.card_ownership import ownership_policy

_log = structlog.get_logger(__name__)


def _json_default(o: Any) -> Any:
    """JSON encoder fallback for non-serializable types appearing in snapshots.

    Session dicts carry ``set``-typed fields (processed_issue_comment_ids,
    processed_review_ids, advocate_history); coerce to a sorted list when
    elements are orderable, otherwise plain list.
    """
    if isinstance(o, set):
        try:
            return sorted(o)
        except TypeError:
            return list(o)
    if isinstance(o, datetime):
        return o.isoformat()
    from pathlib import PurePath
    if isinstance(o, PurePath):
        return str(o)
    raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")


def format_phase_label(phase: str) -> str:
    """Convert a raw phase string to a human-readable label.

    Examples:
        "monitoring_agent" -> "Monitoring Agent"
        "relay_feedback"   -> "Relay Feedback"
        "idle"             -> "Idle"
    """
    return phase.replace("_", " ").title()


def is_session_stale(agent_dispatch_at_iso: str | None, threshold_minutes: int = 30) -> bool:
    """Return True if the session has been running longer than threshold_minutes."""
    if not agent_dispatch_at_iso:
        return False
    try:
        dispatched = datetime.fromisoformat(agent_dispatch_at_iso)
        elapsed = datetime.now(UTC) - dispatched
        return elapsed.total_seconds() > threshold_minutes * 60
    except (ValueError, TypeError):
        return False


def compute_overall_health(subsystems: list[dict[str, Any]]) -> str:
    """Return overall health of required subsystems.

    Returns "healthy", "degraded", or "unavailable".
    Non-required subsystems are excluded from the computation.
    """
    required = [s for s in subsystems if s.get("required")]
    # Empty required list (no required subsystems) is intentionally "healthy"
    if not required or all(s.get("status") == "healthy" for s in required):
        return "healthy"
    if any(s.get("status") == "unavailable" for s in required):
        return "unavailable"
    return "degraded"


def ownership_hint(config: Any) -> str:
    """Name the card-ownership policy in force, for the dashboard (160 FR-010).

    Asks ``ownership_policy`` rather than reading the config fields directly, so
    the hint cannot drift from the gate it describes: whatever makes
    ``check_board`` narrow the board is exactly what makes this return a string.

    Eligibility is a union, so the hint has three shapes, and the empty one is a
    claim too -- it says no policy is in force and every card is coordinare's.
    Rendering the login alone was correct only while ``include_unassigned`` was a
    modifier that did nothing without it; as an independent opt-in it can be the
    whole policy, which is the shape a deployment authenticating as a GitHub App
    must use, since an App cannot be assigned to an issue.

    The login is shown as the operator spelled it. Matching lowercases both
    sides, but echoing their own configuration back at them is what makes a
    typo'd login findable.
    """
    policy = ownership_policy(config)
    if not policy.active:
        return ""
    raw = getattr(config, "assignee_filter", None)
    login = raw.strip() if isinstance(raw, str) else ""
    if login and policy.include_unassigned:
        return f"{login} + unassigned"
    if login:
        return login
    return "unassigned"


def render_performer_pool_widget(pool: Any) -> dict[str, Any]:
    """Render performer pool state for the dashboard snapshot.

    Args:
        pool: the PerformerPool instance, or None if not available.

    Returns:
        A dict with performer registrations and metadata, suitable for JSON
        serialization and inclusion in the dashboard state snapshot.
    """
    if pool is None:
        return {
            "performers": [],
            "total_registered": 0,
            "total_excluded": 0,
            "total_idle": 0,
            "total_busy": 0,
        }

    performers = []
    total_excluded = 0
    total_idle = 0
    total_busy = 0

    for state in pool.list_all():
        is_excluded = state.excluded_until_recovery
        if is_excluded:
            total_excluded += 1

        if state.availability == "idle":
            total_idle += 1
        elif state.availability == "busy":
            total_busy += 1

        performer_entry = {
            "id": state.id,
            "mode": state.mode,
            "availability": state.availability,
            "endpoint": str(state.endpoint) if state.endpoint else None,
            "current_job_id": state.current_job_id,
            "capabilities": (
                {
                    "backends": state.capabilities.backends,
                    "tool_flags": state.capabilities.tool_flags,
                }
                if state.capabilities
                else None
            ),
            "consecutive_failures": state.consecutive_failures,
            "excluded_until_recovery": is_excluded,
            "last_status_at": (
                state.last_status_at.isoformat()
                if state.last_status_at
                else None
            ),
        }
        performers.append(performer_entry)

    return {
        "performers": performers,
        "total_registered": len(performers),
        "total_excluded": total_excluded,
        "total_idle": total_idle,
        "total_busy": total_busy,
    }

_ACTIVE_PHASES = {"monitoring_performer", "monitoring_agent", "dispatching"}

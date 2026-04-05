"""035: Multi-card parallelism -- CardSession abstraction.

A CardSession holds all per-card state that was previously flat on
CoordinareState.  Helper functions copy fields between a session and
the flat state so that graph nodes -- which always operate on the flat
CoordinareState -- work identically regardless of concurrency mode.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypedDict

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path


class CardSession(TypedDict, total=False):
    """Per-card state container.

    Each field mirrors a card-scoped field on CoordinareState.
    """

    current_card: dict[str, Any] | None
    performer_stage: str
    agent_dispatch: dict[str, Any]
    agent_dispatch_at: datetime | None
    workspace_path: Path | None
    workspace_branch: str | None
    performer_events: list[dict]
    performer_metrics: dict | None
    card_tokens_total: int
    card_cost_estimate: float
    card_budget_alert_sent: bool
    open_questions: list[str]
    card_clarifications: list[dict]
    relay_feedback: list[dict[str, Any]]
    pending_reviews: list[dict[str, Any]]
    system_error_count: int
    system_error_last_at: datetime | None
    system_error_reason: str | None
    system_error_notified: bool
    commit_summary: str | None
    agent_health_status: str | None
    phase: str
    pending_override: dict[str, Any] | None
    requirements_changed: bool
    requirements_changed_details: dict[str, Any]
    last_blocked_notified_at: datetime | None
    phase_entered_at: datetime | None


# Fields that live on both CardSession and CoordinareState (flat).
# Used for round-tripping between session ←→ state.
_SESSION_FIELDS: tuple[str, ...] = (
    "current_card",
    "performer_stage",
    "agent_dispatch",
    "agent_dispatch_at",
    "workspace_path",
    "workspace_branch",
    "performer_events",
    "performer_metrics",
    "card_tokens_total",
    "card_cost_estimate",
    "card_budget_alert_sent",
    "open_questions",
    "card_clarifications",
    "relay_feedback",
    "pending_reviews",
    "system_error_count",
    "system_error_last_at",
    "system_error_reason",
    "system_error_notified",
    "commit_summary",
    "agent_health_status",
    "phase",
    "pending_override",
    "requirements_changed",
    "requirements_changed_details",
    "last_blocked_notified_at",
    "phase_entered_at",
)


def create_session_from_card(card: dict[str, Any]) -> CardSession:
    """Create a fresh CardSession for a newly-picked-up card."""
    return CardSession(
        current_card=card,
        performer_stage="implementing",
        agent_dispatch={},
        agent_dispatch_at=None,
        workspace_path=None,
        workspace_branch=None,
        performer_events=[],
        performer_metrics=None,
        card_tokens_total=0,
        card_cost_estimate=0.0,
        card_budget_alert_sent=False,
        open_questions=[],
        card_clarifications=[],
        relay_feedback=[],
        pending_reviews=[],
        system_error_count=0,
        system_error_last_at=None,
        system_error_reason=None,
        system_error_notified=False,
        commit_summary=None,
        agent_health_status=None,
        phase="dispatching",
        pending_override=None,
        requirements_changed=False,
        requirements_changed_details={},
        last_blocked_notified_at=None,
        phase_entered_at=None,
    )


def session_to_state(session: CardSession, state: dict[str, Any]) -> None:
    """Copy session fields into the flat CoordinareState dict.

    This is called *before* invoking graph nodes so that node code
    sees the session's card-specific values in the usual flat fields.
    """
    for field in _SESSION_FIELDS:
        if field in session:
            state[field] = session[field]


def state_to_session(state: dict[str, Any]) -> CardSession:
    """Copy card-scoped fields from flat CoordinareState into a CardSession.

    Called *after* graph node invocation to capture any mutations the
    node made to card-specific fields back into the session.
    """
    session = CardSession()
    for field in _SESSION_FIELDS:
        if field in state:
            session[field] = state[field]  # type: ignore[literal-required]
    return session

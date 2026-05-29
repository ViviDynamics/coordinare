"""035: Multi-card parallelism -- CardSession abstraction.

A CardSession holds all per-card state that was previously flat on
CoordinareState.  Helper functions copy fields between a session and
the flat state so that graph nodes -- which always operate on the flat
CoordinareState -- work identically regardless of concurrency mode.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, TypedDict

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path


# 074 — Persona scope tiering (per-card classifier output).
Depth = Literal["skim", "normal", "full", "skip"]


class PersonaScopeSlice(TypedDict):
    """Per-persona slice of the PersonaScope (depth + focus + structured overrides)."""

    depth: Depth
    focus: str
    overrides: list[str]


class PersonaScope(TypedDict):
    """Per-card classifier output — maps each persona to its scope slice.

    Lives on CardSession; round-trips through _SESSION_FIELDS; persisted as
    optional `persona_scope` on PersistedSession (schema v4+).
    """

    computed_at: str
    cycle_index: int
    classifier_model: str
    head_sha: str
    files_summary: list[dict[str, Any]]
    personas: dict[str, PersonaScopeSlice]


@dataclass
class SessionStats:
    """052: Live session stats polled from the performer backend's HTTP API."""

    title: str | None
    files_changed: int
    lines_added: int
    lines_removed: int


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
    # 069 FR-004: per-session Slack-delivery watermark for card_blocked.
    # Stamped by graph/nodes/notify.py on successful Slack dispatch; read by
    # the same node on subsequent passes to enforce
    # card_blocked_reminder_cooldown_seconds.  Must round-trip through the
    # session ↔ state copy or the cooldown gate sees None every cycle and
    # falls through to the NotificationService's per-channel dedup window
    # (default 600s), producing repeated blocked-status Slack posts.
    last_blocked_slack_delivered_at: datetime | None
    phase_entered_at: datetime | None
    backend_ui_url: str | None
    session_stats: SessionStats | None
    last_issue_comment_id: int | None
    processed_issue_comment_ids: set[int]
    qa_screenshots: list[Any]
    feedback_cycle_count: int
    total_feedback_cycles: int
    triage_blocks: int
    # 072 FR-072-8..11: head-delta audit trail. ``head_at_dispatch`` is the
    # branch HEAD captured the first time this card was dispatched in its
    # current lifecycle pass; ``head_at_last_turn`` is the most recent
    # ``head_after`` from a terminal performer response.
    head_at_dispatch: str | None
    head_at_last_turn: str | None
    # 072 FR-072-5(c): clarifications-count snapshot at dispatch — see
    # CoordinareState for semantics.
    clarifications_count_at_dispatch: int
    # In-memory dedup record: performer_stage values for which a
    # card_dispatched Slack notification has already fired on this active
    # session. Round-trips through session ↔ state on each cycle so the
    # suppression survives the daemon's flat-state copies, but is
    # intentionally NOT persisted to disk — a restart re-announces active
    # dispatches so operators see which cards came back.
    dispatched_notified_stages: list[str]
    # Lifecycle handoff timestamp written by monitor_performer when a card
    # advances to monitoring_pr.  Used by monitor_pr as a cutoff to ignore
    # automation-stage reviews (reviewer/security/qa) that landed before the
    # human-review handoff.  MUST round-trip through session ↔ state or the
    # next cycle sees cutoff=None, classifies pre-handoff bot reviews as
    # actionable human feedback, and re-dispatches the card to implementing.
    lifecycle_completed_at: datetime | None
    # Review node IDs already dispatched to classify_human_feedback.  Lives
    # on flat state as ``set[str]``; persisted to disk as a sorted list via
    # ``PersistedSession``.  MUST round-trip through session ↔ state or the
    # same review re-classifies every cycle until the snapshot is rewritten.
    processed_review_ids: set[str]
    # 074 FR-011: per-card persona-scope classification.  MUST round-trip
    # through session ↔ state or the next cycle re-classifies from scratch
    # (acceptable, but loses the previous-cycle fallback chain in FR-006).
    persona_scope: PersonaScope | None
    # 075: per-HEAD CI-gate bounce counter.  Keyed by head SHA so a new push
    # resets the counter for that SHA; prior entries are preserved as audit.
    # MUST round-trip through session ↔ state or the gate forgets bounces
    # between cycles and never escalates to needs_human_review.
    bounce_counter: dict[str, int]
    # 075: most recent CIGateDecision dict, written by monitor_performer and
    # consumed by notify.py for the PR rollup comment (one-cycle delay).
    latest_ci_gate_decision: dict[str, Any] | None
    # 075: signature of the last CI-gate rollup comment posted to the PR
    # (notify.py dedup).  MUST round-trip through session ↔ state or the dedup
    # check fails between cycles and triggers a redundant GitHub API call every
    # HOLD cycle.  None = no comment posted yet for this card.
    ci_gate_rollup_signature: str | None
    # 075 FR-014: advisory (non-required) check failures surfaced on a PASS
    # verdict so the reviewer persona can see what still needs attention.
    # Cleared on every PASS; stale on BOUNCE/HOLD/ESCALATE (check the
    # latest_ci_gate_decision verdict before consuming).
    ci_gate_advisory_failures: list[dict[str, Any]]
    # 076 dispatcher-dedup state (schema v7+).  All five default to safe
    # empty values; live on both CardSession and PersistedSession so they
    # survive daemon restarts.  See specs/076-qa-cycle/data-model.md §8.
    idle_timeout_retries: dict[str, dict[str, Any]]
    pr_artefacts_recorded_at: datetime | None
    multi_pr_divergence: dict[str, Any] | None
    wedge_count_window: dict[str, list[datetime]]
    reconciliation_decisions_last_startup: dict[str, str]


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
    "last_blocked_slack_delivered_at",
    "phase_entered_at",
    "backend_ui_url",
    "session_stats",
    "last_issue_comment_id",
    "processed_issue_comment_ids",
    "qa_screenshots",
    "feedback_cycle_count",
    "total_feedback_cycles",
    "triage_blocks",
    "head_at_dispatch",
    "head_at_last_turn",
    "clarifications_count_at_dispatch",
    "dispatched_notified_stages",
    "lifecycle_completed_at",
    "processed_review_ids",
    "persona_scope",
    "bounce_counter",
    "latest_ci_gate_decision",
    "ci_gate_rollup_signature",
    "ci_gate_advisory_failures",
    # 076 dispatcher dedup
    "idle_timeout_retries",
    "pr_artefacts_recorded_at",
    "multi_pr_divergence",
    "wedge_count_window",
    "reconciliation_decisions_last_startup",
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
        last_blocked_slack_delivered_at=None,
        phase_entered_at=None,
        backend_ui_url=None,
        session_stats=None,
        last_issue_comment_id=None,
        processed_issue_comment_ids=set(),
        qa_screenshots=[],
        feedback_cycle_count=0,
        total_feedback_cycles=0,
        triage_blocks=0,
        head_at_dispatch=None,
        head_at_last_turn=None,
        clarifications_count_at_dispatch=0,
        dispatched_notified_stages=[],
        lifecycle_completed_at=None,
        processed_review_ids=set(),
        persona_scope=None,
        bounce_counter={},
        latest_ci_gate_decision=None,
        ci_gate_rollup_signature=None,
        ci_gate_advisory_failures=[],
        # 076 dispatcher dedup
        idle_timeout_retries={},
        pr_artefacts_recorded_at=None,
        multi_pr_divergence=None,
        wedge_count_window={},
        reconciliation_decisions_last_startup={},
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

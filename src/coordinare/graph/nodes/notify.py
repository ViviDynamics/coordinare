from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from coordinare.graph.state import CoordinareState
from coordinare.models.notification import EventType, NotificationEvent, NotificationSeverity

logger = structlog.get_logger(__name__)


def _event_type_for_phase(
    phase: str, card: dict, commit_summary: str | None,
) -> EventType:
    """Map (phase, card, commit_summary) to a notification event type.

    042 fix: the ``merging`` phase is the **attempt** to merge — not the
    success state.  ``merge_pr`` sets ``phase=merging`` both when starting
    a merge AND on every retry after a failed squash_merge call.  Mapping
    that phase to ``card_merged`` produced false "✅ merged!" Slack
    notifications every cycle the merge was being rejected by the API
    (e.g., when the GitHub App lacks bypass permission on a branch ruleset).

    Real success is detected via ``commit_summary`` — ``merge_pr`` only
    populates it after the squash_merge mutation returns successfully —
    or via ``card.status == "DONE"``, which is set in the same success
    path.  Either signal is sufficient.
    """
    if commit_summary or str(card.get("status", "")) == "DONE":
        return EventType.card_merged
    mapping: dict[str, EventType] = {
        "dispatching": EventType.card_dispatched,
        "monitoring_performer": EventType.card_dispatched,  # performer just dispatched
        "monitoring_agent": EventType.card_dispatched,  # legacy agent just dispatched
        "blocked": EventType.card_blocked,
        "relay_feedback": EventType.card_transition,
        "monitoring_pr": EventType.card_transition,
        # NOTE: ``merging`` is intentionally absent — it's an in-flight phase.
        # The success branch above catches the actual merge via commit_summary.
    }
    return mapping.get(phase, EventType.card_transition)


async def notify(state: CoordinareState) -> CoordinareState:
    notification_service = state.get("notification_service")
    card = state.get("current_card")
    if not isinstance(card, dict) or notification_service is None:
        return state

    phase = state.get("phase", "idle")
    open_questions_raw = state.get("open_questions")
    open_questions = [str(item) for item in open_questions_raw] if isinstance(open_questions_raw, list) else []
    commit_summary_raw = state.get("commit_summary")
    commit_summary = str(commit_summary_raw) if isinstance(commit_summary_raw, str) else None

    event_type = _event_type_for_phase(phase, card, commit_summary)

    # 042: When merge_pr fails and loops back to phase=merging without a
    # commit_summary, there's nothing new to tell the operator — suppress
    # the notification rather than emit a misleading ``card_transition``
    # for every retry.  The circuit breaker / stuck card alerts already
    # surface the underlying GitHub error.
    if phase == "merging" and not commit_summary and str(card.get("status", "")) != "DONE":
        return state

    # Build human-readable summary
    card_title = str(card.get("title", ""))
    card_number = card.get("issue_number", "")
    card_ref = f"#{card_number}" if card_number else ""
    title_short = card_title[:60] + "…" if len(card_title) > 60 else card_title
    display_title = f"{card_ref} {title_short}".strip()

    status = str(card.get("status", ""))
    prev_status = str(card.get("previous_status", ""))
    pr_url = str(card.get("pr_url", "") or "")
    # Coerce None/non-string to "" BEFORE stringifying.  ``str(None)``
    # returns the literal "None" (truthy), which would then leak into the
    # dedup key and the "dispatched to None" summary — same bug class as
    # the _build_snapshot fix.  ``state.get(..., "")`` only fires the
    # default on MISSING keys, not on keys whose value is explicitly None.
    _raw_stage = state.get("performer_stage")
    performer_stage = _raw_stage if isinstance(_raw_stage, str) else ""

    # Human-readable summary per event type
    if event_type == EventType.card_dispatched:
        role = performer_stage or "implementer"
        summary = f"🚀 {display_title} — dispatched to {role}"
    elif event_type == EventType.card_blocked:
        questions_preview = open_questions[0][:80] if open_questions else "needs input"
        summary = f"🚫 {display_title} — blocked: {questions_preview}"
    elif event_type == EventType.card_merged:
        summary = f"✅ {display_title} — merged!"
    elif prev_status and status and prev_status != status:
        summary = f"📋 {display_title} — {prev_status} → {status}"
    else:
        summary = f"📋 {display_title} [{status}]"

    if pr_url:
        summary += f"\n   PR: {pr_url}"

    payload: dict[str, str] = {
        "event_type": event_type.value,
        "severity": NotificationSeverity.info.value,
        "source": "board",
        "summary": summary,
        "card_title": card_title,
        "card_number": str(card_number),
        "card_status": status,
        "previous_status": prev_status,
    }
    if open_questions:
        payload["open_questions"] = "; ".join(open_questions)
    if commit_summary:
        payload["commit_summary"] = commit_summary
    if pr_url:
        payload["pr_url"] = pr_url

    card_id = card.get("id") or card.get("title", "unknown")
    # Include performer_stage in dedup key so each lifecycle stage gets its own notification
    dedup_key = f"{event_type.value}:{card_id}:{card.get('status', '')}:{performer_stage}"

    event = NotificationEvent(
        event_type=event_type,
        severity=NotificationSeverity.info,
        payload=payload,
        source="board",
        dedup_key=dedup_key,
    )

    try:
        await notification_service.dispatch(event)
    except Exception as exc:
        logger.warning("notification.dispatch_failed", error=str(exc))

    return state

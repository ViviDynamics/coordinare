from __future__ import annotations

import hashlib
from datetime import UTC, datetime
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

    card_id_for_guard = str(card.get("id") or card.get("title", "unknown"))

    # 069 FR-003: empty open_questions means there's nothing actionable for
    # operators — never emit a content-free card_blocked Slack post.
    if event_type == EventType.card_blocked and not open_questions:
        logger.info(
            "notify.card_blocked_skipped_empty_questions",
            card_id=card_id_for_guard,
        )
        return state

    # 069 FR-005: when a fresh performer is already running for this card
    # (active session in dispatching / monitoring_*), suppress a stale
    # card_blocked emitted from a rehydrated top-level phase.  Reproduces
    # 2026-05-22 incident where restart-with-fresh-dispatch double-spammed.
    active_sessions = state.get("active_sessions") or {}
    sess = active_sessions.get(card_id_for_guard, {}) if isinstance(active_sessions, dict) else {}
    sess_phase = sess.get("phase") if isinstance(sess, dict) else None
    if event_type == EventType.card_blocked and sess_phase in {
        "dispatching",
        "monitoring_performer",
        "monitoring_agent",
    }:
        logger.info(
            "notify.card_blocked_suppressed_active_session",
            card_id=card_id_for_guard,
            session_phase=sess_phase,
        )
        return state

    # 069 FR-004: reminder-cooldown gate.  Compare wall-clock now against
    # the per-session ``last_blocked_slack_delivered_at`` watermark and
    # suppress if the cooldown has not elapsed.  This is the primary gate
    # for card_blocked re-emission: it survives restarts (the watermark is
    # persisted) AND prevents per-cycle dispatch loops from leaking through
    # once the per-channel DeduplicationWindow expires.
    #
    # We read ``last_blocked_slack_delivered_at`` and NOT
    # ``last_blocked_notified_at``.  The latter is rewritten by
    # handle_blocked on every pass (it triples as check_board cutoff +
    # GitHub 24h dedup) so it does not prove Slack actually went out.
    if event_type == EventType.card_blocked:
        cooldown_seconds = getattr(
            notification_service, "card_blocked_reminder_cooldown_seconds", 3600
        )
        sess_watermark = sess.get("last_blocked_slack_delivered_at") if isinstance(sess, dict) else None
        if cooldown_seconds > 0 and sess_watermark is not None:
            try:
                elapsed = (datetime.now(UTC) - sess_watermark).total_seconds()
            except TypeError:
                elapsed = None
            if elapsed is not None and elapsed < cooldown_seconds:
                logger.info(
                    "notify.card_blocked_suppressed_reminder_cooldown",
                    card_id=card_id_for_guard,
                    elapsed_seconds=elapsed,
                    cooldown_seconds=cooldown_seconds,
                )
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

    # Dispatched-once gate: NotificationService's per-channel
    # DeduplicationWindow (default 600s) re-fires the same card_dispatched
    # event after expiry. While env_bootstrap is in flight the session can
    # sit in monitoring_performer for >10 min, producing duplicate Slack
    # posts. Suppress re-emission for a (card, performer_stage) we already
    # announced on this active session; a stage change (e.g. implementing
    # → reviewing) re-emits naturally because the gate keys on stage. The
    # stage bucket key uses ``__unknown__`` when performer_stage is blank so
    # a misordered early emit can't permanently silence later real stages.
    stage_bucket = performer_stage or "__unknown__"
    if event_type == EventType.card_dispatched and isinstance(sess, dict):
        already = sess.get("dispatched_notified_stages")
        if isinstance(already, list) and stage_bucket in already:
            logger.info(
                "notify.card_dispatched_suppressed_already_announced",
                card_id=card_id_for_guard,
                performer_stage=performer_stage,
            )
            return state

    # Human-readable summary per event type
    if event_type == EventType.card_dispatched:
        role = performer_stage or "implementer"
        summary = f"🚀 {display_title} — dispatched to {role}"
    elif event_type == EventType.card_blocked:
        # 069 FR-003: empty-questions case is suppressed earlier; we can
        # safely index here without the "needs input" fallback.
        questions_preview = open_questions[0][:80]
        # 046: If the card is blocked due to dependencies, include blocker
        # issue numbers and columns in the summary so the Slack message
        # is actionable without cross-referencing the board.
        blocked_deps = state.get("blocked_by_dependencies") or []
        if blocked_deps:
            dep_labels = ", ".join(
                f"#{d.get('issue_number', '?')} ({d.get('column') or 'off-board'})"
                for d in blocked_deps[:3]
            )
            summary = f"🚫 {display_title} — blocked: waiting on {dep_labels}"
        else:
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

    # Use the same stringified id as the FR-004 prefix scan so dedup_key
    # and prefix match across types (e.g., int issue numbers in tests).
    card_id = card_id_for_guard
    # Include performer_stage in dedup key so each lifecycle stage gets its own notification
    dedup_key = f"{event_type.value}:{card_id}:{card.get('status', '')}:{performer_stage}"
    # 069 FR-006: incorporate a content hash of open_questions into the
    # card_blocked dedup key so different question sets produce distinct
    # notifications (previously, status+stage alone collapsed unrelated
    # blocked rounds into one dedup bucket).
    if event_type == EventType.card_blocked and open_questions:
        digest = hashlib.sha256("\n".join(open_questions).encode("utf-8")).hexdigest()[:12]
        dedup_key = f"{dedup_key}:{digest}"

    event = NotificationEvent(
        event_type=event_type,
        severity=NotificationSeverity.info,
        payload=payload,
        source="board",
        dedup_key=dedup_key,
    )

    try:
        await notification_service.dispatch(event)
        # 069 FR-004: stamp the per-session Slack-delivery watermark on
        # successful card_blocked dispatch so post-restart suppression has a
        # truthful gate that survives NotificationHistory loss.  We update
        # the live active_sessions entry in place; daemon._persist_active_sessions
        # snapshots it on the next save cycle.
        if event_type == EventType.card_blocked and isinstance(sess, dict):
            sess["last_blocked_slack_delivered_at"] = datetime.now(UTC)
        # Stamp only when the session is registered in active_sessions —
        # otherwise ``sess`` is the throwaway ``{}`` default from .get() and
        # the stamp would be lost, leaving the gate open for the next pass.
        if (
            event_type == EventType.card_dispatched
            and isinstance(active_sessions, dict)
            and card_id_for_guard in active_sessions
            and isinstance(sess, dict)
        ):
            stages = sess.get("dispatched_notified_stages")
            if not isinstance(stages, list):
                stages = []
                sess["dispatched_notified_stages"] = stages
            if stage_bucket not in stages:
                stages.append(stage_bucket)
    except Exception as exc:
        logger.warning("notification.dispatch_failed", error=str(exc))

    return state

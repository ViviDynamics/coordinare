from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from coordinare.graph.state import CoordinareState
from coordinare.graph.attribution import coordinare_attribution
from coordinare.models.notification import EventType, NotificationEvent, NotificationSeverity
from coordinare.services.ci_gate import compute_ci_gate_signature

logger = structlog.get_logger(__name__)

PERSONA_SCOPE_ROLLUP_MARKER = "<!-- coordinare:persona-scope-rollup -->"
CI_GATE_ROLLUP_MARKER_PREFIX = "<!-- coordinare:ci-gate:"


def _persona_scope_signature(scope: dict[str, Any]) -> str:
    """Deterministic signature of (persona, depth, overrides) tuples.

    Focus prose is excluded because LLM-generated text varies slightly across
    cycles for identical classifications; including it would defeat dedup.
    Used to skip re-posting an identical PR rollup comment when the structural
    scope has not changed between cycles (FR-001 dedup).
    """
    personas = scope.get("personas") or {}
    items: list[tuple[str, str, tuple[str, ...]]] = []
    for name in sorted(personas.keys()):
        slice_ = personas[name] or {}
        depth = str(slice_.get("depth", ""))
        overrides_raw = slice_.get("overrides") or []
        overrides = (
            tuple(sorted(str(o) for o in overrides_raw))
            if isinstance(overrides_raw, list)
            else ()
        )
        items.append((name, depth, overrides))
    payload = json.dumps(items, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _format_persona_scope_rollup(scope: dict[str, Any]) -> str:
    """Render PersonaScope as a markdown table for PR comments."""
    personas = scope.get("personas") or {}
    lines = [
        PERSONA_SCOPE_ROLLUP_MARKER,
        "**Coordinare — persona scope plan for this cycle**",
        "",
        "| Persona | Depth | Focus | Overrides |",
        "| --- | --- | --- | --- |",
    ]
    for name in sorted(personas.keys()):
        slice_ = personas[name] or {}
        depth = str(slice_.get("depth", ""))
        focus = str(slice_.get("focus", "") or "—").replace("|", "\\|")
        overrides_raw = slice_.get("overrides") or []
        overrides = ", ".join(str(o) for o in overrides_raw) if overrides_raw else "—"
        overrides = overrides.replace("|", "\\|")
        lines.append(f"| `{name}` | `{depth}` | {focus} | {overrides} |")
    cycle = scope.get("cycle_index")
    model = scope.get("classifier_model") or ""
    head = scope.get("head_sha") or ""
    meta_bits = []
    if cycle is not None:
        meta_bits.append(f"cycle `{cycle}`")
    if model:
        meta_bits.append(f"classifier `{model}`")
    if head:
        meta_bits.append(f"head `{head[:7]}`")
    if meta_bits:
        lines.append("")
        lines.append("<sub>" + " · ".join(meta_bits) + "</sub>")
    return "\n".join(lines)


async def _emit_persona_scope_rollup(
    state: CoordinareState,
    session: dict[str, Any],
    card: dict[str, Any],
) -> None:
    """Post a deduplicated PR comment summarizing PersonaScope (spec 074).

    Fires only when ``session.persona_scope`` is set and its signature
    differs from the last-emitted signature on the session.  No-op when
    the PR has no node id or the github service is unavailable.
    """
    scope = session.get("persona_scope")
    if not isinstance(scope, dict) or not scope.get("personas"):
        return
    github_service = state.get("github_service")
    if github_service is None:
        return
    pr_node_id = card.get("pr_node_id")
    if not pr_node_id:
        return
    signature = _persona_scope_signature(scope)
    last_signature = session.get("persona_scope_rollup_signature")
    if signature == last_signature:
        return
    header = coordinare_attribution(state.get("config"), state.get("performer_stage"))
    body = f"{header}\n\n{_format_persona_scope_rollup(scope)}"
    try:
        await github_service.add_comment(str(pr_node_id), body)
    except Exception as exc:
        logger.warning(
            "persona_scope.rollup_post_failed",
            card_id=card.get("id"),
            error=str(exc),
        )
        return
    session["persona_scope_rollup_signature"] = signature
    logger.info(
        "persona_scope.rollup_posted",
        card_id=card.get("id"),
        signature=signature,
        persona_count=len(scope.get("personas") or {}),
    )


def _ci_gate_signature(decision: dict[str, Any]) -> str:
    """16-char dedup signature for a CIGateDecision dict (spec 075 contracts §3).

    Delegates to ``compute_ci_gate_signature`` from ``ci_gate.py`` so the two
    code paths (model form and persisted-dict form) share a single algorithm.
    """
    head_sha = str(decision.get("head_sha") or "")
    verdict = str(decision.get("verdict") or "")
    required = [str(r) for r in (decision.get("required_checks") or [])]
    failed = decision.get("failed_checks") or []
    failed_names = [str((c or {}).get("name", "")) for c in failed]
    return compute_ci_gate_signature(
        head_sha=head_sha,
        verdict=verdict,
        required_checks=required,
        failed_names=failed_names,
    )


def _render_ci_gate_comment(decision: dict[str, Any]) -> str:
    """Render the PR rollup markdown per contracts/gate-decision.md §3."""
    verdict = str(decision.get("verdict") or "").lower()
    head_sha = str(decision.get("head_sha") or "")
    head_short = head_sha[:7] if head_sha else "—"
    required = list(decision.get("required_checks") or [])
    resolver_source = str(decision.get("resolver_source") or "all_head_checks")
    failed = list(decision.get("failed_checks") or [])
    pending = list(decision.get("pending_checks") or [])
    bounce_count = int(decision.get("bounce_count_after") or 0)
    max_bounces = int(decision.get("max_bounces_per_head") or 0)
    sig = _ci_gate_signature(decision)

    lines = [
        f"{CI_GATE_ROLLUP_MARKER_PREFIX}{sig} -->",
        f"### Coordinare CI gate — {verdict.upper()}",
        "",
        f"**HEAD**: `{head_short}` &nbsp; "
        f"**Required checks**: {len(required)} ({resolver_source})",
        "",
    ]
    if verdict == "pass":
        lines.append("All required checks green. Advancing to reviewer.")
    elif verdict == "hold":
        lines.append("Waiting on pending checks:")
        for name in pending:
            lines.append(f"- `{name}`")
    elif verdict in ("bounce", "escalate"):
        if verdict == "bounce":
            bounce_label = f"{bounce_count}/{max_bounces}" if max_bounces else str(bounce_count)
            lines.append(f"Failing checks (bounce {bounce_label}):")
        else:
            max_label = f"/{max_bounces}" if max_bounces else ""
            lines.append(f"Bounce limit reached ({bounce_count}{max_label}). "
                         "Card moved to needs_human_review.")
        lines.append("")
        lines.append("| Check | Conclusion | Last line |")
        lines.append("| --- | --- | --- |")
        for c in failed:
            name = str(c.get("name", ""))
            conclusion = str(c.get("conclusion", ""))
            url = c.get("html_url") or ""
            last = (c.get("last_log_line") or "").replace("|", "\\|")
            label = f"[{name}]({url})" if url else f"`{name}`"
            last_cell = f"`{last}`" if last else "—"
            lines.append(f"| {label} | {conclusion} | {last_cell} |")
    return "\n".join(lines)


async def _emit_ci_gate_rollup(
    state: CoordinareState,
    session: dict[str, Any],
    card: dict[str, Any],
) -> None:
    """Post a deduplicated PR comment summarizing the latest CI-gate decision.

    PASS verdicts are silent (the silent-success default).  HOLD/BOUNCE/
    ESCALATE post once per (head_sha, signature); dedup checks both the
    session-side ``ci_gate_rollup_signature`` and existing PR comments so
    survival of restarts doesn't re-spam the PR.
    """
    decision = session.get("latest_ci_gate_decision")
    if not isinstance(decision, dict):
        return
    verdict = str(decision.get("verdict") or "").lower()
    if verdict not in ("hold", "bounce", "escalate"):
        return  # PASS / unknown — silent.

    signature = _ci_gate_signature(decision)
    last = session.get("ci_gate_rollup_signature")
    if signature == last:
        return

    github_service = state.get("github_service")
    if github_service is None:
        return
    pr_node_id = card.get("pr_node_id")
    if not pr_node_id:
        return

    marker = f"{CI_GATE_ROLLUP_MARKER_PREFIX}{signature} -->"

    # Best-effort scan of existing PR comments to survive restarts.
    issue_number = card.get("issue_number") or card.get("pr_number")
    get_comments = getattr(github_service, "get_issue_comments", None)
    if callable(get_comments) and issue_number is not None:
        try:
            existing = await get_comments(int(issue_number))
        except Exception as exc:
            logger.debug("ci_gate.rollup_comment_scan_failed", error=str(exc))
            existing = []
        for c in existing or []:
            body = c.get("body") if isinstance(c, dict) else None
            if isinstance(body, str) and marker in body:
                session["ci_gate_rollup_signature"] = signature
                return

    # CI gate acts on the implementer's work — tag the implementing stage.
    header = coordinare_attribution(state.get("config"), "implementing")
    body = f"{header}\n\n{_render_ci_gate_comment(decision)}"
    try:
        await github_service.add_comment(str(pr_node_id), body)
    except Exception as exc:
        logger.warning(
            "ci_gate.rollup_post_failed",
            card_id=card.get("id"),
            error=str(exc),
        )
        return
    session["ci_gate_rollup_signature"] = signature
    logger.info(
        "ci_gate.rollup_posted",
        card_id=card.get("id"),
        signature=signature,
        verdict=verdict,
    )


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

    # 076 (live QA #150): suppress card_dispatched when the dispatch was HELD
    # this cycle.  A genuine dispatch advances to phase "monitoring_performer"
    # (dispatch_performer success path, after a container actually starts)
    # BEFORE reaching notify.  Reaching notify with phase still "dispatching"
    # means dispatch_performer returned early via a hold path (env_cache not
    # ready, env_bootstrap_in_flight, or the in-flight guard) WITHOUT launching
    # a performer.  Without this guard, every poll cycle re-emits card_dispatched
    # and — once the 600s Slack dedup window lapses — leaks a duplicate
    # "dispatched to <stage>" post for a card that never started a container.
    # (Distinct from the dispatched_notified_stages gate below, which covers the
    # monitoring_performer re-emit case; here no container exists at all.)
    if event_type == EventType.card_dispatched and phase == "dispatching":
        logger.info(
            "notify.card_dispatched_suppressed_dispatch_held",
            card_id=card_id_for_guard,
        )
        return state

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

        # 076 (T062-T064, FR-010): suppress card_dispatched when the most
        # recent reconciliation pass ADOPTED an existing container for
        # this card (the operator already saw the notification from the
        # prior daemon process) or SKIPPED_PERSISTENT (no new dispatch
        # happened at all).  See contracts/notification-dedup.md.
        #
        # Round-2 fix: pop the decision after consuming it so a
        # genuinely-new dispatch for the SAME card later in the same
        # cycle (after check_board.is_stale → fresh_dispatched, say) is
        # not silently suppressed by the now-stale startup decision.
        recon_decisions = state.get("reconciliation_decisions_last_startup")
        if isinstance(recon_decisions, dict):
            decision = recon_decisions.pop(card_id_for_guard, None)
            if decision in ("adopted", "skipped_persistent"):
                logger.info(
                    "notify.card_dispatched_suppressed",
                    card_id=card_id_for_guard,
                    reconciliation_decision=decision,
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

    # Emit per-cycle persona-scope rollup as a deduplicated PR comment.
    # Independent of Slack dispatch above — runs whenever a session has a
    # fresh PersonaScope that hasn't been posted yet.
    if isinstance(sess, dict):
        await _emit_persona_scope_rollup(state, sess, card)
        await _emit_ci_gate_rollup(state, sess, card)

    return state

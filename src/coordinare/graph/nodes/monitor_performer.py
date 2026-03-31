"""Generic, role-agnostic performer monitor node (019-performer-lifecycle).

Replaces ``monitor_agent`` with a node that resolves the active service from
``performer_services[performer_stage]`` and contains **zero** role-specific
logic (FR-004).  Terminal success states trigger lifecycle advancement via
``_advance_stage``.  Error status sets ``phase="blocked"`` per FR-006
(changed from the legacy ``system_error`` routing in ``monitor_agent``).
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from coordinare.lib.acceptance_criteria import parse_acceptance_criteria
from coordinare.services.github import PermanentGitHubError
from coordinare.transport.base import TransportError

if TYPE_CHECKING:
    from coordinare.graph.state import CoordinareState

logger = structlog.get_logger(__name__)

TERMINAL_SUCCESS_STATES: frozenset[str] = frozenset({
    "pr_opened",
    "plan_committed",
    "approved",
    "security_passed",
    "qa_passed",
    "docs_committed",
})

# 032: Phase → expected board column mapping for reconciliation
PHASE_TO_EXPECTED_COLUMN: dict[str, str] = {
    "monitoring_agent": "IN_PROGRESS",
    "monitoring_performer": "IN_PROGRESS",
    "monitoring_pr": "IN_REVIEW",
    "merging": "IN_REVIEW",
}


def _find_card_column(card_id: str, board_snapshot: dict[str, Any]) -> str | None:
    """Find which board column a card is in, or None if not found."""
    for column, items in board_snapshot.items():
        if isinstance(items, list) and card_id in items:
            return column
    return None


def _reconcile_board_mismatch(
    state: dict[str, Any],
    card_id: str,
    expected_column: str,
    actual_column: str | None,
) -> bool:
    """Check for board mismatch and reconcile state if needed.

    Returns True if reconciliation occurred (caller should return early).
    """
    if actual_column == expected_column:
        return False  # consistent — no action

    if actual_column is None:
        # Card disappeared from board — handled by 026 cancel logic
        logger.warning(
            "monitor_performer.reconcile.card_not_found",
            card_id=card_id,
            expected_column=expected_column,
        )
        state["phase"] = "idle"
        state["current_card"] = None
        state["agent_dispatch"] = {}
        state["agent_dispatch_at"] = None
        return True

    # Backward move (e.g., IN_PROGRESS → TODO)
    if actual_column in ("TODO", "BACKLOG"):
        logger.warning(
            "monitor_performer.reconcile.backward_move",
            card_id=card_id,
            expected_column=expected_column,
            actual_column=actual_column,
        )
        state["phase"] = "idle"
        state["current_card"] = None
        state["agent_dispatch"] = {}
        state["agent_dispatch_at"] = None
        state["relay_feedback"] = []
        state["pending_reviews"] = []
        lifecycle_seq = state.get("lifecycle_sequence") or ["implementing"]
        state["performer_stage"] = lifecycle_seq[0] if lifecycle_seq else "implementing"
        return True

    # Forward move to DONE
    if actual_column == "DONE":
        logger.info(
            "monitor_performer.reconcile.forward_to_done",
            card_id=card_id,
            expected_column=expected_column,
        )
        lifecycle_seq = state.get("lifecycle_sequence") or ["implementing"]
        state["phase"] = "idle"
        state["current_card"] = None
        state["agent_dispatch"] = {}
        state["agent_dispatch_at"] = None
        state["relay_feedback"] = []
        state["pending_reviews"] = []
        state["open_questions"] = []
        state["performer_stage"] = lifecycle_seq[0] if lifecycle_seq else "implementing"
        state["system_error_count"] = 0
        state["system_error_reason"] = None
        state["system_error_notified"] = False
        return True

    # Move to BLOCKED
    if actual_column == "BLOCKED":
        logger.info(
            "monitor_performer.reconcile.moved_to_blocked",
            card_id=card_id,
            expected_column=expected_column,
        )
        state["phase"] = "blocked"
        state["agent_dispatch"] = {}
        state["agent_dispatch_at"] = None
        return True

    # Any other column mismatch — log but don't act
    logger.debug(
        "monitor_performer.reconcile.unknown_column",
        card_id=card_id,
        expected_column=expected_column,
        actual_column=actual_column,
    )
    return False


async def _teardown_workspace(state: CoordinareState) -> None:
    """Tear down the workspace if one was prepared for this session.

    Clears workspace_path and workspace_branch from state regardless of
    teardown outcome so stale paths never accumulate.
    """
    workspace_manager = state.get("workspace_manager")
    workspace_path = state.get("workspace_path")
    try:
        if workspace_manager is not None and workspace_path is not None:
            await workspace_manager.teardown(workspace_path)
    except Exception:
        logger.warning("workspace_teardown_failed", workspace_path=str(workspace_path))
    finally:
        state["workspace_path"] = None
        state["workspace_branch"] = None


def _apply_pending_override(state: CoordinareState) -> CoordinareState | None:
    """Check for and apply a pending human override (031-human-override-controls).

    Returns the updated state if an override was applied, or None if no
    override was pending.  The override is always cleared from state (FR-008).
    """
    override = state.get("pending_override")
    if override is None:
        return None

    action = override.get("action")
    state["pending_override"] = None  # FR-008: clear immediately

    if action == "skip":
        logger.info("override.skip", performer_stage=state.get("performer_stage"))
        updates = _advance_stage(state)
        for k, v in updates.items():
            state[k] = v  # type: ignore[literal-required]
        return state

    if action == "restart":
        target = override.get("target_stage", "")
        lifecycle = list(state.get("lifecycle_sequence") or [])
        if target in lifecycle:
            logger.info("override.restart", target_stage=target)
            state["performer_stage"] = target
            state["phase"] = "dispatching"
            state["agent_dispatch"] = {}
            state["agent_dispatch_at"] = None
        else:
            logger.warning("override.restart_invalid_role", target_stage=target)
        return state

    if action == "veto":
        logger.info("override.veto", card_id=(state.get("current_card") or {}).get("id"))
        state["phase"] = "blocked"
        state["open_questions"] = ["Lifecycle vetoed by human override."]
        return state

    logger.warning("override.unknown_action", action=action)
    return state


def _advance_stage(state: CoordinareState, status: dict[str, Any] | None = None) -> dict[str, Any]:
    """Compute the state update to advance the lifecycle to the next role.

    If more roles remain in ``lifecycle_sequence``, returns a dict that sets
    ``performer_stage`` to the next stage and resets dispatch state so the
    graph re-enters ``dispatching``.

    If no more roles remain, transitions to ``monitoring_pr`` and copies
    ``pr_url`` / ``pr_node_id`` from the terminal status into the card.
    """
    sequence: list[str] = list(state.get("lifecycle_sequence") or ["implementing"])
    current: str = state.get("performer_stage", "implementing")

    try:
        idx = sequence.index(current)
    except ValueError:
        # Unknown stage — treat as last so we fall through to monitoring_pr.
        idx = len(sequence)

    if idx + 1 < len(sequence):
        # More roles remain — advance to the next stage.
        # Persist PR identifiers from the current role's status so they're
        # available to subsequent roles (e.g. reviewer needs the PR URL).
        updates: dict[str, Any] = {
            "performer_stage": sequence[idx + 1],
            "phase": "dispatching",
            "agent_dispatch": {},
            "agent_dispatch_at": None,
        }
        if status is not None:
            card = dict(state.get("current_card") or {})
            changed = False
            pr_url = status.get("pr_url")
            pr_node_id = status.get("pr_node_id")
            plan_path = status.get("plan_path")
            if pr_url:
                card["pr_url"] = pr_url
                changed = True
            if pr_node_id:
                card["pr_node_id"] = pr_node_id
                changed = True
            if plan_path:
                card["plan_path"] = plan_path
                changed = True
            if changed:
                updates["current_card"] = card
        return updates

    # All roles complete — transition to human review.
    card: dict[str, Any] = dict(state.get("current_card") or {})
    if status is not None:
        pr_url = status.get("pr_url")
        pr_node_id = status.get("pr_node_id")
        plan_path = status.get("plan_path")
        if pr_url:
            card["pr_url"] = pr_url
        if pr_node_id:
            card["pr_node_id"] = pr_node_id
        if plan_path:
            card["plan_path"] = plan_path

    card["previous_status"] = card.get("status", "IN_PROGRESS")
    card["status"] = "IN_REVIEW"

    return {
        "phase": "monitoring_pr",
        "current_card": card,
        "system_error_count": 0,
        "system_error_last_at": None,
        "system_error_notified": False,
        "system_error_reason": None,
    }


async def monitor_performer(state: CoordinareState) -> CoordinareState:
    """Poll the active performer and route based on status.

    Reads ``performer_stage`` from state, resolves the service from
    ``performer_services[performer_stage]``, and polls ``check_status``.
    Contains zero role-specific logic — all routing is driven by the
    status string returned by the performer.
    """
    # 030: Reset requirement-change flags at cycle start so stale flags never persist.
    # Placed before all early-return paths (incl. override check, service/card guard).
    state["requirements_changed"] = False
    state["requirements_changed_details"] = {}

    card = state.get("current_card")
    github = state.get("github_service")

    # 031: Check for a pending human override before polling status.
    # Done after card/github extraction so skip-final-stage can move the card.
    override_result = _apply_pending_override(state)
    if override_result is not None:
        # Teardown workspace on override paths to avoid resource leaks.
        await _teardown_workspace(override_result)

        # When skip advances to monitoring_pr (final stage), move card on board
        # and validate PR fields — same as the normal terminal-success path.
        if override_result.get("phase") == "monitoring_pr" and github is not None:
            effective_card = override_result.get("current_card", card)
            if not isinstance(effective_card, dict):
                return override_result
            card_id = str(effective_card.get("id", ""))
            pr_url = effective_card.get("pr_url")
            pr_node_id = effective_card.get("pr_node_id")
            if pr_url and pr_node_id:
                try:
                    await github.move_card(card_id, "IN_REVIEW")
                except Exception:
                    logger.warning("override.skip_move_card_failed", card_id=card_id)
            else:
                logger.error(
                    "override.skip_final_missing_pr_fields",
                    card_id=card_id,
                    pr_url_present=bool(pr_url),
                    pr_node_id_present=bool(pr_node_id),
                )
                # Mirror normal terminal-success behavior: missing PR fields
                # is a system error. Restore card status to pre-override value
                # since we never actually moved it on the board.
                override_result["phase"] = "system_error"
                override_result["system_error_count"] = state.get("system_error_count", 0) + 1
                override_result["system_error_reason"] = (
                    "Skip override reached final stage but pr_url or pr_node_id is missing"
                )
                override_result["system_error_last_at"] = datetime.now(UTC)
                override_result["system_error_notified"] = False
                updated_card = override_result.get("current_card")
                if isinstance(updated_card, dict):
                    previous_status = updated_card.get("previous_status")
                    if previous_status is not None:
                        updated_card["status"] = previous_status
                    override_result["current_card"] = updated_card
        return override_result

    stage: str = state.get("performer_stage", "implementing")
    performer_services: dict[str, Any] = state.get("performer_services") or {}
    service = performer_services.get(stage)

    # Fallback to legacy agent_service only when performer_services is empty
    # (backward compatibility with pre-019 configurations).  When
    # performer_services is populated, a missing entry means the role was
    # not configured — do not silently monitor with the legacy service.
    if service is None and not performer_services:
        service = state.get("agent_service")

    if service is None or not isinstance(card, dict):
        state["phase"] = "idle"
        return state

    session_id = state.get("agent_dispatch", {}).get("session_id", "")
    card_id = str(card.get("id", ""))

    # 027: Enforce per-role session timeout at coordinare level
    # Only enforced when explicitly configured (role_timeouts[stage] > 0).
    role_timeouts: dict[str, int] = state.get("role_timeouts") or {}
    timeout_secs = role_timeouts.get(stage, 0)

    # Assume terminal by default; cleared only when the performer is still working.
    # The finally block guarantees teardown even on unexpected exceptions.
    _teardown_on_exit = True
    try:
        # 032: Board reconciliation — check card column before polling status
        current_phase = state.get("phase", "")
        expected_column = PHASE_TO_EXPECTED_COLUMN.get(current_phase)
        if expected_column and isinstance(card, dict):
            board_snapshot = state.get("board_snapshot") or {}
            # Only reconcile when board_snapshot has at least one card in any column.
            # An empty snapshot (all columns []) means either check_board hasn't run
            # yet this cycle, or the board is genuinely empty. In the latter case,
            # the card would be detected as "disappeared" by check_board's 026 logic.
            has_data = any(isinstance(v, list) and len(v) > 0 for v in board_snapshot.values())
            if has_data:
                actual_column = _find_card_column(card_id, board_snapshot)
                if _reconcile_board_mismatch(state, card_id, expected_column, actual_column):
                    return state
        # 027: Check session timeout before polling status
        dispatch_at = state.get("agent_dispatch_at")
        if timeout_secs > 0 and dispatch_at is not None:
            elapsed = (datetime.now(UTC) - dispatch_at).total_seconds()
            if elapsed > timeout_secs:
                logger.warning(
                    "monitor_performer.session_timeout",
                    performer_stage=stage,
                    card_id=card_id,
                    elapsed_seconds=round(elapsed),
                    timeout_seconds=timeout_secs,
                )
                state["phase"] = "blocked"
                state["open_questions"] = [
                    f"Performer ({stage}) timed out after {round(elapsed)}s "
                    f"(limit: {timeout_secs}s)"
                ]
                return state

        try:
            status = await service.check_status(str(session_id))
        except (TransportError, ConnectionError, TimeoutError) as exc:
            # Network / transport failure — transient, route through retry logic.
            # ResilientAgentService re-raises TransportError after exhausting
            # retries; ConnectionError/TimeoutError cover bare transport errors.
            logger.warning(
                "monitor_performer.transport_error",
                card_id=card_id,
                performer_stage=stage,
                exc_type=type(exc).__name__,
            )
            # If system_error_notified is True we're inheriting stale state from
            # a previous card's exhausted retry cycle (that card was BLOCKED and
            # can no longer appear in monitor_performer).  Reset so this card gets
            # its full retry budget and operator notification fires if needed.
            if state.get("system_error_notified"):
                state["system_error_count"] = 0
                state["system_error_notified"] = False
            state["system_error_count"] = state.get("system_error_count", 0) + 1
            state["system_error_last_at"] = datetime.now(UTC)
            state["system_error_reason"] = (
                f"Transport failure during status check: {type(exc).__name__}"
            )
            state["agent_dispatch"] = {}
            state["agent_dispatch_at"] = None
            state["phase"] = "system_error"
            return state
        except PermanentGitHubError as exc:
            logger.error(
                "permanent_service_failure.card_blocked",
                card_id=card_id,
                performer_stage=stage,
                error=str(exc),
            )
            if github is not None:
                try:
                    await github.move_card(card_id, "BLOCKED")
                except Exception:
                    logger.warning("move_card_to_blocked_failed", card_id=card_id)
            state["phase"] = "blocked"
            state["open_questions"] = [f"Permanent service failure: {exc}"]
            return state

        # Accumulate backend events (capped at 100 entries).
        new_events = status.get("events")
        if isinstance(new_events, list) and new_events:
            existing = list(state.get("performer_events") or [])
            state["performer_events"] = (existing + new_events)[-100:]

        # Store latest performer metrics for dashboard visibility.
        new_metrics = status.get("metrics")
        if isinstance(new_metrics, dict):
            state["performer_metrics"] = new_metrics

        marker = status.get("status", "working")

        # 030: Live requirement sync — detect card changes mid-cycle.
        # Only check when the performer is still working; terminal statuses
        # (success, error, etc.) take priority and must not be preempted.
        config = state.get("config")
        policy = "warn"
        if config is not None and hasattr(config, "requirement_change_policy"):
            policy = config.requirement_change_policy

        if (
            policy != "ignore"
            and marker == "working"
            and isinstance(card, dict)
            and github is not None
        ):
            issue_id_raw = card.get("issue_id")
            issue_id = str(issue_id_raw).strip() if issue_id_raw is not None else ""
            if issue_id:
                try:
                    fresh = await github.get_issue_details(issue_id)
                    old_desc = str(card.get("description", ""))
                    new_desc = fresh.get("body") or fresh.get("description") or ""
                    has_new_field = ("body" in fresh) or ("description" in fresh)
                    if old_desc.strip() != new_desc.strip() and (new_desc.strip() or has_new_field):
                        state["requirements_changed"] = True
                        state["requirements_changed_details"] = {
                            "old_length": len(old_desc),
                            "new_length": len(new_desc),
                        }
                        if policy == "warn":
                            logger.warning(
                                "monitor_performer.requirements_changed",
                                card_id=card_id,
                                performer_stage=stage,
                                policy=policy,
                            )
                        elif policy == "re-dispatch":
                            logger.info(
                                "monitor_performer.requirements_changed.re_dispatch",
                                card_id=card_id,
                                performer_stage=stage,
                                workspace_policy="restart",
                            )
                            card["description"] = new_desc
                            with contextlib.suppress(Exception):
                                card["acceptance_criteria"] = parse_acceptance_criteria(new_desc)
                            state["current_card"] = card
                            state["phase"] = "dispatching"
                            state["agent_dispatch"] = {}
                            state["agent_dispatch_at"] = None
                            return state
                except asyncio.CancelledError:
                    raise
                except (ConnectionError, TimeoutError, OSError, ValueError) as exc:
                    logger.debug(
                        "monitor_performer.requirement_check_failed",
                        card_id=card_id,
                        error=str(exc),
                        exc_info=True,
                    )

        # --- Terminal success states ---
        if marker in TERMINAL_SUCCESS_STATES:
            updates = _advance_stage(state, status)

            # When advancing to monitoring_pr (final role complete), move the
            # card on the GitHub board and validate required PR fields.
            if updates.get("phase") == "monitoring_pr":
                updated_card = updates.get("current_card", card)
                pr_url = updated_card.get("pr_url")
                pr_node_id = updated_card.get("pr_node_id")

                if not pr_url or not pr_node_id:
                    logger.error(
                        "monitor_performer.final_stage_missing_pr_fields",
                        card_id=card_id,
                        performer_stage=stage,
                        pr_url_present=bool(pr_url),
                        pr_node_id_present=bool(pr_node_id),
                    )
                    # Reset stale error state inherited from a previous card so
                    # this card gets its full retry budget and operator notification fires.
                    if state.get("system_error_notified"):
                        state["system_error_count"] = 0
                        state["system_error_notified"] = False
                    state["system_error_count"] = state.get("system_error_count", 0) + 1
                    state["system_error_last_at"] = datetime.now(UTC)
                    state["system_error_reason"] = (
                        "Performer reported terminal success but pr_url or pr_node_id is missing"
                    )
                    state["phase"] = "system_error"
                    return state

                if github is not None:
                    try:
                        await github.move_card(card_id, "IN_REVIEW")
                    except Exception:
                        logger.warning("move_card_to_in_review_failed", card_id=card_id)

            # Apply the computed state updates.
            for key, value in updates.items():
                state[key] = value  # type: ignore[literal-required]
            return state

        # --- Changes requested (021-reviewer-performer) ---
        # Non-terminal outcome: relay reviewer comments back to implementer.
        if marker == "changes_requested":
            raw_comments = status.get("comments", [])
            comments = raw_comments if isinstance(raw_comments, list) else []
            logger.info(
                "monitor_performer.changes_requested",
                performer_stage=stage,
                card_id=card_id,
                comment_count=len(comments),
            )
            state["relay_feedback"] = comments  # type: ignore[typeddict-unknown-key]
            state["performer_stage"] = "implementing"
            state["phase"] = "dispatching"
            state["agent_dispatch"] = {}
            state["agent_dispatch_at"] = None
            return state

        # --- Security failed (022-security-performer) ---
        # Non-terminal: route findings to implementer or architect based on routing field.
        if marker == "security_failed":
            raw_findings = status.get("findings", [])
            findings = raw_findings if isinstance(raw_findings, list) else []
            lifecycle = state.get("lifecycle_sequence") or []
            # Determine earliest routing target from findings
            targets = set()
            for f in findings:
                if isinstance(f, dict):
                    targets.add(f.get("routing", "implementer"))
            # Route to earliest: architect before implementer
            target_stage = "architecting" if "architect" in targets else "implementing"
            if target_stage not in lifecycle:
                target_stage = lifecycle[0] if lifecycle else "implementing"

            # Only relay findings targeted at this stage's role
            target_role = "architect" if target_stage == "architecting" else "implementer"
            relevant_findings = [
                f for f in findings
                if isinstance(f, dict) and f.get("routing", "implementer") == target_role
            ]

            logger.info(
                "monitor_performer.security_failed",
                performer_stage=stage,
                card_id=card_id,
                finding_count=len(findings),
                routed_count=len(relevant_findings),
                routing_target=target_stage,
            )
            state["relay_feedback"] = relevant_findings  # type: ignore[typeddict-unknown-key]
            state["performer_stage"] = target_stage
            state["phase"] = "dispatching"
            state["agent_dispatch"] = {}
            state["agent_dispatch_at"] = None
            return state

        # --- QA failed (023-qa-performer) ---
        # Non-terminal: relay failures to implementer for remediation.
        if marker == "qa_failed":
            raw_failures = status.get("failures", [])
            failures = [f for f in (raw_failures if isinstance(raw_failures, list) else []) if isinstance(f, dict)]
            logger.info(
                "monitor_performer.qa_failed",
                performer_stage=stage,
                card_id=card_id,
                failure_count=len(failures),
            )
            state["relay_feedback"] = failures  # type: ignore[typeddict-unknown-key]
            state["performer_stage"] = "implementing"
            state["phase"] = "dispatching"
            state["agent_dispatch"] = {}
            state["agent_dispatch_at"] = None
            return state

        # --- Error status (FR-006) ---
        if marker == "error":
            state["phase"] = "blocked"
            reason = str(status.get("reason", ""))
            state["open_questions"] = [
                f"Performer ({stage}) encountered an error: {reason}" if reason
                else f"Performer ({stage}) encountered an error."
            ]
            return state

        # --- Session expired ---
        if marker == "session_expired":
            # Preserve any unanswered questions so the next performer receives them.
            open_qs = [str(q) for q in (state.get("open_questions") or [])]
            if open_qs:
                existing_clarifications = list(state.get("card_clarifications") or [])
                state["card_clarifications"] = [
                    *existing_clarifications,
                    {"questions": open_qs, "answer": ""},
                ]
            state["open_questions"] = []

            # If a PR was already opened in a prior cycle, resume monitoring it
            # rather than re-queuing the card to TODO (which would trigger a
            # duplicate dispatch and a GitHub 422 error).
            if card.get("pr_node_id"):
                logger.info(
                    "monitor_performer.session_expired_resume_monitoring_pr",
                    card_id=card_id,
                    performer_stage=stage,
                    pr_node_id=card["pr_node_id"],
                    msg="Session expired but PR already open — resuming monitoring_pr",
                )
                state["agent_dispatch"] = {}
                state["agent_dispatch_at"] = None
                state["phase"] = "monitoring_pr"
            else:
                # Transient failure with no open PR — auto-requeue to TODO.
                reason = str(status.get("reason", ""))
                logger.warning(
                    "monitor_performer.session_expired_requeue",
                    card_id=card_id,
                    performer_stage=stage,
                    reason=reason,
                    msg="Session expired — moving card back to TODO for re-dispatch",
                )
                if github is not None:
                    try:
                        await github.move_card(card_id, "TODO")
                    except Exception:
                        logger.warning("move_card_to_todo_failed", card_id=card_id)
                state["agent_dispatch"] = {}
                state["agent_dispatch_at"] = None
                state["phase"] = "idle"
            return state

        # --- Blocked ---
        if marker == "blocked":
            state["phase"] = "blocked"
            questions = status.get("questions")
            if isinstance(questions, list) and questions:
                state["open_questions"] = [str(item) for item in questions]
            else:
                # Blocked with no questions — assessment backend will generate them.
                state["open_questions"] = []
            return state

        # --- In-progress (working) ---
        _teardown_on_exit = False  # still working — workspace stays active
        state["phase"] = "monitoring_performer"
        return state
    finally:
        if _teardown_on_exit:
            await _teardown_workspace(state)

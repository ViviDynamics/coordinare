"""Generic, role-agnostic performer monitor node (019-performer-lifecycle).

Replaces ``monitor_agent`` with a node that resolves the active service from
``performer_services[performer_stage]`` and contains **zero** role-specific
logic (FR-004).  Terminal success states trigger lifecycle advancement via
``_advance_stage``.  Error status sets ``phase="blocked"`` per FR-006
(changed from the legacy ``system_error`` routing in ``monitor_agent``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

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
    card = state.get("current_card")
    github = state.get("github_service")

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

    # Assume terminal by default; cleared only when the performer is still working.
    # The finally block guarantees teardown even on unexpected exceptions.
    _teardown_on_exit = True
    try:
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

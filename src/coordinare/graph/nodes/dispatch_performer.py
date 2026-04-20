"""Generic, role-agnostic performer dispatch node (019-performer-lifecycle).

Replaces dispatch_card with a stage-driven dispatch that resolves the
performer service from ``performer_services[performer_stage]`` and injects
the matching persona instructions.  Contains ZERO role-specific logic
(FR-003) -- the stage-to-persona-role mapping is the only bridge between
the lifecycle pipeline and the persona system.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from coordinare.services.github import PermanentGitHubError
from coordinare.services.persona_service import get_effective_instructions, load_personas_hot
from coordinare.transport.base import TransportError
from coordinare.workspace import WorkspaceSetupError

if TYPE_CHECKING:
    from coordinare.graph.state import AgentServiceProtocol, CoordinareState

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Stage -> persona role mapping
# ---------------------------------------------------------------------------
# The performer pipeline uses stage names (verbs / gerunds) while the persona
# system uses role names (nouns).  This mapping is the *only* place where
# stage identifiers are coupled to persona roles.
_STAGE_TO_ROLE: dict[str, str] = {
    "implementing": "implementer",
    "reviewing": "reviewer",
    "security": "security",
    "qa": "qa",
    "documenting": "tech_writer",
    "architecting": "architect",
    "assessing": "assessor",
    "advocate": "advocate",
    # 042: Closing pass — same persona shape as reviewer but distinct stage
    # so _advance_stage doesn't loop back to the substantive reviewer.
    "closing_review": "closer",
}


def _persona_role_for_stage(stage: str) -> str | None:
    """Return the persona role name for a pipeline stage, or None if unmapped."""
    return _STAGE_TO_ROLE.get(stage)


async def dispatch_performer(state: CoordinareState) -> CoordinareState:
    """Dispatch work to the performer service for the current pipeline stage.

    Reads ``performer_stage`` from state, resolves the service from
    ``performer_services[performer_stage]``, performs a health check,
    prepares the workspace, and dispatches the card.  On success the phase
    transitions to ``monitoring_performer``.

    If the resolved service is ``None`` the stage is skipped by calling
    ``_advance_stage`` from ``monitor_performer``.
    """
    # Lazy import to avoid circular dependency -- monitor_performer is created
    # in parallel and will exist by the time this node is actually invoked.
    from coordinare.graph.nodes.monitor_performer import _advance_stage, _apply_pending_override

    card: dict[str, Any] | None = state.get("current_card")
    github = state.get("github_service")

    # 031: Check for a pending human override before dispatching.
    # Done after card/github extraction so skip-final can move the card.
    override_result = _apply_pending_override(state)
    if override_result is not None:
        new_phase = override_result.get("phase")
        if new_phase == "dispatching":
            # Skip/restart kept us in dispatching — continue and dispatch
            # for the updated performer_stage in this invocation.
            state = override_result
        elif new_phase == "monitoring_pr" and github is not None:
            # Skip on final stage — move card to IN_REVIEW, validate PR fields.
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
                override_result["phase"] = "system_error"
                override_result["system_error_count"] = state.get("system_error_count", 0) + 1
                override_result["system_error_reason"] = (
                    "Skip override reached final stage but pr_url or pr_node_id is missing"
                )
                override_result["system_error_last_at"] = datetime.now(UTC)
                override_result["system_error_notified"] = False
                # Restore card status — _advance_stage set IN_REVIEW but we never moved it.
                updated_card = override_result.get("current_card")
                if isinstance(updated_card, dict):
                    previous_status = updated_card.get("previous_status")
                    if previous_status is not None:
                        updated_card["status"] = previous_status
                    override_result["current_card"] = updated_card
            return override_result
        else:
            # Non-dispatching phase (blocked, etc.) — return early.
            return override_result
    performer_stage: str = state.get("performer_stage", "")  # type: ignore[assignment]
    performer_services: dict[str, Any] = state.get("performer_services", {})  # type: ignore[assignment]

    if not isinstance(card, dict) or github is None or not performer_stage:
        logger.warning(
            "dispatch_performer.missing_prerequisites",
            has_card=isinstance(card, dict),
            has_github=github is not None,
            performer_stage=performer_stage,
        )
        state["phase"] = "idle"
        return state

    # 048: Resolve the service for this stage via SlotManager if available.
    # The SlotManager enforces per-role max_concurrency and returns a free
    # service instance, or None if at capacity (card retries next cycle).
    slot_manager = state.get("slot_manager")
    card_id = str(card.get("id", ""))
    service: AgentServiceProtocol | None = None
    if slot_manager is not None and hasattr(slot_manager, "acquire"):
        service = slot_manager.acquire(
            performer_stage, card_id, config=state.get("config"),
        )
        if service is None and hasattr(slot_manager, "is_at_capacity") and slot_manager.is_at_capacity(performer_stage):
            # At capacity — card waits. Return without changing phase so
            # the next poll cycle retries.
            logger.info(
                "dispatch_performer.at_capacity",
                performer_stage=performer_stage,
                card_id=card_id,
            )
            return state
        # If slot_manager returned None but NOT at capacity:
        # - Stage has no pool (unknown) → fall through to legacy
        # - Stage has pool with max=0 (disabled) → don't fall through
        #   (skip the role, same as if not configured)
        if service is None:
            pool = slot_manager.pools.get(performer_stage) if hasattr(slot_manager, "pools") else None
            if pool is not None and pool.max_concurrency <= 0:
                service = None  # disabled — will be skipped below
            else:
                service = performer_services.get(performer_stage)
    else:
        # Legacy path: single service per stage (backward compatible)
        service = performer_services.get(performer_stage)

    # Fallback to legacy agent_service only when performer_services is empty
    # (backward compatibility with pre-019 configurations).
    if service is None and not performer_services:
        service = state.get("agent_service")

    if service is None:
        # No service configured for this stage -- skip to the next role.
        logger.info(
            "dispatch_performer.no_service_for_stage",
            performer_stage=performer_stage,
        )
        updates = _advance_stage(state)
        for key, value in updates.items():
            state[key] = value  # type: ignore[literal-required]
        return state

    # 048: If we acquired a slot from the SlotManager, we must release it
    # on any early error return (health check, workspace, transport failure)
    # to prevent slot leaks.
    _acquired_via_slot_mgr = (
        slot_manager is not None
        and hasattr(slot_manager, "release")
        and service is not None
    )

    def _release_slot_on_error() -> None:
        if _acquired_via_slot_mgr:
            slot_manager.release(performer_stage, card_id)

    # --- Health check with retry (033) ---
    card_id = str(card.get("id", ""))

    config = state.get("config")
    max_attempts = 3
    backoff_base = 1.0
    if config is not None and hasattr(config, "health_check"):
        max_attempts = config.health_check.max_attempts
        backoff_base = config.health_check.backoff_seconds

    health: dict[str, Any] = {}
    health_status = "unknown"
    from time import monotonic as _monotonic
    _t0 = _monotonic()

    for attempt in range(1, max_attempts + 1):
        try:
            health = await service.check_health()
            health_status = str(health.get("status", "unknown"))
        except asyncio.CancelledError:
            raise
        except Exception:
            health_status = "unreachable"

        if health_status not in {"unknown", "unreachable"}:
            if attempt > 1:
                logger.info(
                    "dispatch_performer.health_check_retry_succeeded",
                    performer_stage=performer_stage,
                    attempt=attempt,
                    elapsed_seconds=round(_monotonic() - _t0, 1),
                )
            break

        if attempt < max_attempts:
            delay = backoff_base * (2 ** (attempt - 1))
            await asyncio.sleep(delay)

    state["agent_health_status"] = health_status

    if health_status in {"unknown", "unreachable"}:
        logger.warning(
            "dispatch_performer.health_check_retries_exhausted",
            health_status=health_status,
            performer_stage=performer_stage,
            attempts=max_attempts,
            elapsed_seconds=round(_monotonic() - _t0, 1),
        )
        _release_slot_on_error()
        state["phase"] = "idle"
        return state

    if health_status == "error":
        health_reason = health.get("reason", "") if isinstance(health, dict) else ""
        state["phase"] = "blocked"
        state["open_questions"] = [
            f"Performer health check failed for stage {performer_stage!r} "
            f"(status: {health_status}). "
            + (f"Reason: {health_reason}" if health_reason else
               "Check performer logs for details.")
        ]
        _release_slot_on_error()
        return state

    # --- Workspace setup (011) ---
    workspace_manager = state.get("workspace_manager")
    workspace_info = None

    if workspace_manager is not None:
        try:
            workspace_info = await workspace_manager.prepare(card)
            state["workspace_path"] = workspace_info.path
            state["workspace_branch"] = workspace_info.branch
        except WorkspaceSetupError as exc:
            logger.error(
                "workspace_setup_failed.card_blocked",
                card_id=card_id,
                performer_stage=performer_stage,
                error=str(exc),
            )
            try:
                await github.move_card(card_id, "BLOCKED")
            except Exception:
                logger.warning("move_card_to_blocked_failed", card_id=card_id)
            state["workspace_path"] = None
            state["workspace_branch"] = None
            _release_slot_on_error()
            state["phase"] = "blocked"
            state["open_questions"] = [str(exc)]
            return state
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            reason = (
                f"Workspace setup failed unexpectedly ({type(exc).__name__}). "
                "Check coordinare logs for details."
            )
            logger.error(
                "workspace_setup_unexpected_error.card_blocked",
                card_id=card_id,
                performer_stage=performer_stage,
                error=f"{type(exc).__name__}: {exc}",
            )
            try:
                await github.move_card(card_id, "BLOCKED")
            except Exception:
                logger.warning("move_card_to_blocked_failed", card_id=card_id)
            state["workspace_path"] = None
            state["workspace_branch"] = None
            _release_slot_on_error()
            state["phase"] = "blocked"
            state["open_questions"] = [reason]
            return state

        # Validate required workspace fields before dispatching.
        if workspace_info is not None:
            required: list[tuple[str, str]] = [
                ("repo_url", workspace_info.repo_url),
                ("branch", workspace_info.branch),
            ]
            if workspace_info.path is not None:
                required.append(("github_token", workspace_info.github_token))
            missing = [field for field, value in required if not value]
            if missing:
                reason = (
                    f"Workspace context incomplete -- missing required fields: "
                    f"{', '.join(missing)}"
                )
                logger.error(
                    "dispatch_performer.incomplete_workspace",
                    card_id=card_id,
                    performer_stage=performer_stage,
                    missing=missing,
                )
                try:
                    await github.move_card(card_id, "BLOCKED")
                except Exception:
                    logger.warning("move_card_to_blocked_failed", card_id=card_id)
                if workspace_info.path is not None:
                    try:
                        await workspace_manager.teardown(workspace_info.path)
                    except Exception:
                        logger.warning(
                            "workspace_teardown_failed.after_incomplete_workspace",
                            card_id=card_id,
                        )
                state["workspace_path"] = None
                state["workspace_branch"] = None
                _release_slot_on_error()
                state["phase"] = "blocked"
                state["open_questions"] = [reason]
                return state

    # --- Build card context with persona instructions ---
    personas = load_personas_hot(state.get("config_path"), state.get("config"))
    card_context: dict[str, Any] = dict(card)

    role = _persona_role_for_stage(performer_stage)
    if role is not None:
        card_context["persona_instructions"] = get_effective_instructions(role, personas)

    # Include relay feedback when present (reviewer / QA feedback loops).
    relay_feedback: list[dict[str, Any]] | None = state.get("relay_feedback")  # type: ignore[assignment]
    if relay_feedback:
        card_context["relay_feedback"] = relay_feedback

    # 020: Include architecture plan in dispatch payload for downstream roles (FR-007).
    # The plan_path is set on the card by monitor_performer when the architect
    # returns plan_committed.
    plan_path = card.get("plan_path")
    if plan_path and performer_stage != "architecting":
        # Include plan path reference; downstream performers read from branch.
        card_context["architecture_plan_path"] = plan_path

    # Pass the performer role so the performer can gate behavior on it.
    card_context["role"] = performer_stage

    # 036: Include GitHub API URL so the performer connects to the same instance.
    config = state.get("config")
    if config is not None and hasattr(config, "github_api_url"):
        card_context["github_api_url"] = config.github_api_url

    # 037: Include per-role backend and model in dispatch payload.
    if config is not None and hasattr(config, "performers") and role is not None:
        role_config = getattr(config.performers, role, None)
        if role_config is not None:
            card_context["backend"] = role_config.backend
            if role_config.model is not None:
                card_context["model"] = role_config.model

    # --- Dispatch ---
    try:
        await github.move_card(card_id, "IN_PROGRESS")
        result = await service.dispatch_card(card_context, workspace_info=workspace_info)
    except TransportError as exc:
        reason = f"Transport failure during dispatch: {type(exc).__name__}"
        logger.warning(
            "dispatch_performer.transport_error",
            card_id=card_id,
            performer_stage=performer_stage,
            exc_type=type(exc).__name__,
        )
        # Reset stale error state from a previous card so this card gets
        # its full retry budget.
        if state.get("system_error_notified"):
            state["system_error_count"] = 0
            state["system_error_notified"] = False
        state["system_error_count"] = state.get("system_error_count", 0) + 1
        state["system_error_last_at"] = datetime.now(UTC)
        state["system_error_reason"] = reason
        _release_slot_on_error()
        state["phase"] = "system_error"
        card["previous_status"] = card.get("status", "TODO")
        card["status"] = "IN_PROGRESS"
        state["current_card"] = card
        # Tear down workspace to avoid leaking temp directories.
        if workspace_manager is not None and workspace_info is not None and workspace_info.path is not None:
            try:
                await workspace_manager.teardown(workspace_info.path)
            except Exception:
                logger.warning(
                    "workspace_teardown_failed.after_transport_error",
                    card_id=card_id,
                )
        state["workspace_path"] = None
        state["workspace_branch"] = None
        return state
    except PermanentGitHubError as exc:
        logger.error(
            "permanent_service_failure.card_blocked",
            card_id=card_id,
            performer_stage=performer_stage,
            error=str(exc),
        )
        try:
            await github.move_card(card_id, "BLOCKED")
        except Exception:
            logger.warning("move_card_to_blocked_failed", card_id=card_id)
        # Tear down workspace to avoid leaking temp directories.
        if workspace_manager is not None and workspace_info is not None and workspace_info.path is not None:
            try:
                await workspace_manager.teardown(workspace_info.path)
            except Exception:
                logger.warning("workspace_teardown_failed.after_permanent_error", card_id=card_id)
        state["workspace_path"] = None
        state["workspace_branch"] = None
        _release_slot_on_error()
        state["phase"] = "blocked"
        state["open_questions"] = [f"Permanent service failure: {exc}"]
        return state

    if result.get("status") == "error":
        reason = str(result.get("reason", "Performer returned an error on dispatch."))
        logger.error(
            "dispatch_performer.performer_error",
            card_id=card_id,
            performer_stage=performer_stage,
            reason=reason,
        )
        # Tear down workspace to avoid leaking temp directories.
        if workspace_manager is not None and workspace_info is not None and workspace_info.path is not None:
            try:
                await workspace_manager.teardown(workspace_info.path)
            except Exception:
                logger.warning("workspace_teardown_failed.after_dispatch_error", card_id=card_id)
        state["workspace_path"] = None
        state["workspace_branch"] = None
        # Reset stale error state from a previous card.
        if state.get("system_error_notified"):
            state["system_error_count"] = 0
            state["system_error_notified"] = False
        state["system_error_count"] = state.get("system_error_count", 0) + 1
        state["system_error_last_at"] = datetime.now(UTC)
        state["system_error_reason"] = f"Performer dispatch failed ({performer_stage}): {reason}"
        _release_slot_on_error()
        state["phase"] = "system_error"
        return state

    # --- Success ---
    # 034: Reset token counters when dispatching the first role for a new card.
    lifecycle = list(state.get("lifecycle_sequence") or [])
    if lifecycle and performer_stage == lifecycle[0]:
        state["card_tokens_total"] = 0
        state["card_cost_estimate"] = 0.0
        state["card_budget_alert_sent"] = False
        from coordinare.metrics import METRICS
        METRICS.card_cost_estimate_dollars.set(0)

    # Clear relay_feedback so it isn't re-sent to subsequent roles.
    state["relay_feedback"] = []  # type: ignore[typeddict-unknown-key]
    state["agent_dispatch"] = result
    state["agent_dispatch_at"] = datetime.now(UTC)
    state["performer_events"] = []
    state["performer_metrics"] = None
    card["previous_status"] = card.get("status", "TODO")
    card["status"] = "IN_PROGRESS"
    state["current_card"] = card
    state["phase"] = "monitoring_performer"
    state["system_error_count"] = 0
    state["system_error_last_at"] = None
    state["system_error_notified"] = False
    state["system_error_reason"] = None
    return state

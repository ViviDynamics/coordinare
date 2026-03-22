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
    from coordinare.graph.nodes.monitor_performer import _advance_stage

    card: dict[str, Any] | None = state.get("current_card")
    github = state.get("github_service")
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

    # Resolve the service for this stage.
    service: AgentServiceProtocol | None = performer_services.get(performer_stage)

    # Fallback to legacy agent_service only when performer_services is empty
    # (backward compatibility with pre-019 configurations).  When
    # performer_services is populated, a missing entry means the role should
    # be skipped — not silently run on the legacy service.
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

    # --- Health check ---
    card_id = str(card.get("id", ""))

    try:
        health = await service.check_health()
        health_status = str(health.get("status", "unknown"))
    except Exception:
        health_status = "unreachable"

    state["agent_health_status"] = health_status

    if health_status in {"unknown", "unreachable"}:
        logger.warning(
            "dispatch_performer.agent_unreachable",
            health_status=health_status,
            performer_stage=performer_stage,
        )
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
        state["phase"] = "system_error"
        return state

    # --- Success ---
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

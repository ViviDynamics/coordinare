from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog

from coordinare.services.github import PermanentGitHubError
from coordinare.transport.base import TransportError
from coordinare.workspace import WorkspaceSetupError

if TYPE_CHECKING:
    from coordinare.graph.state import CoordinareState

logger = structlog.get_logger(__name__)


async def dispatch_card(state: CoordinareState) -> CoordinareState:
    card = state.get("current_card")
    github = state.get("github_service")
    agent = state.get("agent_service")
    if not isinstance(card, dict) or github is None or agent is None:
        state["phase"] = "idle"
        return state

    try:
        health = await agent.check_health()
        health_status = str(health.get("status", "unknown"))
    except Exception:
        health_status = "unreachable"

    state["agent_health_status"] = health_status
    if health_status in {"unknown", "unreachable"}:
        # Performer is unreachable (not yet started, transport timeout, or
        # transient exception).  This is transient — stay idle and retry next
        # cycle rather than blocking the card with an error that requires human
        # attention.
        logger.warning(
            "dispatch_card.agent_unreachable",
            health_status=health_status,
        )
        state["phase"] = "idle"
        return state
    if health_status == "error":
        state["phase"] = "blocked"
        state["open_questions"] = [
            f"Agent health check failed (status: {health_status}). "
            "Cannot dispatch work until the agent is reachable."
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
                card_id=str(card.get("id", "")),
                error=str(exc),
            )
            try:
                await github.move_card(str(card.get("id", "")), "BLOCKED")
            except Exception:
                logger.warning(
                    "move_card_to_blocked_failed",
                    card_id=str(card.get("id", "")),
                )
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
                card_id=str(card.get("id", "")),
                error=f"{type(exc).__name__}: {exc}",
            )
            try:
                await github.move_card(str(card.get("id", "")), "BLOCKED")
            except Exception:
                logger.warning(
                    "move_card_to_blocked_failed",
                    card_id=str(card.get("id", "")),
                )
            state["workspace_path"] = None
            state["workspace_branch"] = None
            state["phase"] = "blocked"
            state["open_questions"] = [reason]
            return state

        # Validate required workspace fields before dispatching (FR-006).
        # workspace_path and github_token are intentionally optional for Kubernetes
        # transport: path=None (performer manages its own workspace via K8s Secrets)
        # and github_token="" (auth is injected into the container separately via
        # K8s Secrets).  When github_token="" the performer's GitHub API helpers will
        # raise GitHubAPIError(401) immediately rather than silently looping, so the
        # session will surface an error state quickly rather than running to watchdog.
        # Require github_token only when path is not None (subprocess/local transport).
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
                    f"Workspace context incomplete — missing required fields: "
                    f"{', '.join(missing)}"
                )
                logger.error(
                    "dispatch_card.incomplete_workspace",
                    card_id=str(card.get("id", "")),
                    missing=missing,
                )
                try:
                    await github.move_card(str(card.get("id", "")), "BLOCKED")
                except Exception:
                    logger.warning(
                        "move_card_to_blocked_failed",
                        card_id=str(card.get("id", "")),
                    )
                # Tear down the prepared workspace so temp dirs don't leak.
                if workspace_info.path is not None:
                    try:
                        await workspace_manager.teardown(workspace_info.path)
                    except Exception:
                        logger.warning(
                            "workspace_teardown_failed.after_incomplete_workspace",
                            card_id=str(card.get("id", "")),
                        )
                state["workspace_path"] = None
                state["workspace_branch"] = None
                state["phase"] = "blocked"
                state["open_questions"] = [reason]
                return state

    card_id = str(card.get("id", ""))
    try:
        await github.move_card(card_id, "IN_PROGRESS")
        result = await agent.dispatch_card(card, workspace_info=workspace_info)
    except TransportError as exc:
        # Network / transport failure — transient, route through retry logic.
        reason = f"Transport failure during dispatch: {type(exc).__name__}"
        logger.warning("dispatch_card.transport_error", card_id=card_id, exc_type=type(exc).__name__)
        state["system_error_count"] = state.get("system_error_count", 0) + 1
        state["system_error_last_at"] = datetime.now(UTC)
        state["system_error_reason"] = reason
        state["phase"] = "system_error"
        # Card was moved to IN_PROGRESS on GitHub before the transport error;
        # keep in-memory state consistent so dashboards and check_board agree.
        card["previous_status"] = card.get("status", "TODO")
        card["status"] = "IN_PROGRESS"
        state["current_card"] = card
        # Tear down any workspace that was prepared before the error so retries
        # don't leak temp directories on disk.
        if workspace_manager is not None and workspace_info is not None:
            try:
                await workspace_manager.teardown(workspace_info.path)
            except Exception:
                logger.warning("workspace_teardown_failed.after_transport_error", card_id=card_id)
        state["workspace_path"] = None
        state["workspace_branch"] = None
        return state
    except PermanentGitHubError as exc:
        logger.error(
            "permanent_service_failure.card_blocked",
            card_id=card_id,
            error=str(exc),
        )
        try:
            await github.move_card(card_id, "BLOCKED")
        except Exception:
            logger.warning("move_card_to_blocked_failed", card_id=card_id)
        state["phase"] = "blocked"
        state["open_questions"] = [f"Permanent service failure: {exc}"]
        return state

    if result.get("status") == "error":
        reason = str(result.get("reason", "Performer returned an error on dispatch."))
        logger.error("dispatch_card.performer_error", card_id=card_id, reason=reason)
        state["system_error_count"] = state.get("system_error_count", 0) + 1
        state["system_error_last_at"] = datetime.now(UTC)
        state["system_error_reason"] = f"Agent dispatch failed: {reason}"
        state["phase"] = "system_error"
        return state

    state["agent_dispatch"] = result
    state["agent_dispatch_at"] = datetime.now(UTC)
    state["performer_events"] = []
    state["performer_metrics"] = None
    card["previous_status"] = card.get("status", "TODO")
    card["status"] = "IN_PROGRESS"
    state["current_card"] = card
    state["phase"] = "monitoring_agent"
    state["system_error_count"] = 0
    state["system_error_last_at"] = None
    state["system_error_notified"] = False
    state["system_error_reason"] = None
    return state

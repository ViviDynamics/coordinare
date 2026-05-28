"""Spec 074 — classify_scope graph node.

Sits between board pickup and ``dispatch_performer`` to compute a per-card
``PersonaScope`` via the classifier service.  No-op when:

  - ``config.persona_scope.enabled`` is False (default per FR-010), or
  - there is no active card session, or
  - the active card has no PR yet (no diff to classify), or
  - the classifier returns ``None`` (timeout / parse failure / no PR files).

On ``None`` the session's ``persona_scope`` is left unset and downstream
dispatch falls back to full-depth for all personas (FR-006).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from coordinare.services import persona_classifier

if TYPE_CHECKING:
    from coordinare.graph.state import CoordinareState

logger = structlog.get_logger(__name__)


def _scope_enabled(config: Any) -> bool:
    persona_scope_cfg = getattr(config, "persona_scope", None)
    if persona_scope_cfg is None:
        return False
    return bool(getattr(persona_scope_cfg, "enabled", False))


def _classifier_model(config: Any) -> str:
    conducting = getattr(config, "conducting", None)
    if conducting is None:
        return ""
    return str(getattr(conducting, "model", "") or "")


async def classify_scope_node(state: CoordinareState) -> CoordinareState:
    """Optionally compute per-persona scope for the active card.

    Wrapped in a top-level try/except so that any unexpected failure
    (state shape, session lookup, classifier crash) is swallowed and the
    graph proceeds without a persona_scope — FR-006 fallback ensures the
    card still flows through dispatch at full depth.
    """
    try:
        config = state.get("config")
        if not _scope_enabled(config):
            return state

        current_card = state.get("current_card")
        card_id = current_card.get("id") if isinstance(current_card, dict) else None
        if not card_id or not isinstance(current_card, dict):
            return state

        active_sessions = state.get("active_sessions") or {}
        session = active_sessions.get(card_id)
        if session is None:
            return state

        if not current_card.get("pr_url"):
            # No PR yet — nothing to classify; first dispatch is always full.
            return state

        conducting_backend = state.get("conducting_backend")
        github_service = state.get("github_service")
        workspace_path = session.get("workspace_path")

        scope = await persona_classifier.classify(
            session=session,
            card=current_card,
            conducting_backend=conducting_backend,
            config=config,
            github_service=github_service,
            workspace_path=workspace_path,
            classifier_model=_classifier_model(config),
        )

        if scope is None:
            return state

        session["persona_scope"] = scope
        return state
    except Exception as exc:  # pragma: no cover — defense in depth
        logger.warning(
            "classify_scope.node_error",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return state

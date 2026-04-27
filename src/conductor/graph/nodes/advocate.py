"""Advocate scan LangGraph node (007)."""
from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from coordinare.services.persona_service import get_effective_instructions, load_personas_hot

if TYPE_CHECKING:
    from coordinare.graph.state import CoordinareState

logger = structlog.get_logger(__name__)


async def advocate_scan(state: CoordinareState) -> CoordinareState:
    """Run advocate scan before board check.

    If advocate_service is None (disabled), returns state unchanged.
    Otherwise calls scan_and_respond and updates advocate_history.
    """
    # Multi-session fanout runs this node N times concurrently.  The preflight
    # in _invoke_multi_session runs advocate_scan once on self._state first and
    # sets this flag so per-session invocations are no-ops.
    if state.get("_advocate_scan_done"):  # type: ignore[typeddict-item]
        return state

    advocate_service = state.get("advocate_service")
    if advocate_service is None:
        return state

    processed_ids: set[str] = state.get("advocate_history") or set()  # type: ignore[assignment]

    # Inject advocate persona instructions with hot-reload (018-performer-personas).
    personas = load_personas_hot(state.get("config_path"), state.get("config"))
    persona_instructions = get_effective_instructions("advocate", personas)

    try:
        updated_ids = await advocate_service.scan_and_respond(
            processed_ids, persona_instructions=persona_instructions
        )
        state["advocate_history"] = updated_ids
        logger.info("advocate_scan_complete", processed_count=len(updated_ids - processed_ids))
    except Exception as exc:
        logger.error("advocate_scan.failed", error=str(exc))

    return state

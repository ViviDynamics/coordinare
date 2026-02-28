"""Advocate scan LangGraph node (007)."""
from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from coordinare.graph.state import CoordinareState

logger = structlog.get_logger(__name__)


async def advocate_scan(state: CoordinareState) -> CoordinareState:
    """Run advocate scan before board check.

    If advocate_service is None (disabled), returns state unchanged.
    Otherwise calls scan_and_respond and updates advocate_history.
    """
    advocate_service = state.get("advocate_service")
    if advocate_service is None:
        return state

    processed_ids: set[str] = state.get("advocate_history") or set()  # type: ignore[assignment]

    try:
        updated_ids = await advocate_service.scan_and_respond(processed_ids)
        state["advocate_history"] = updated_ids
        logger.info("advocate_scan_complete", processed_count=len(updated_ids - processed_ids))
    except Exception as exc:
        logger.error("advocate_scan.failed", error=str(exc))

    return state

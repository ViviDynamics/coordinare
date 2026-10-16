from __future__ import annotations

from typing import Any

import structlog

from coordinare.services.stats_measures import MAX_REASON_CHARS

logger = structlog.get_logger(__name__)

BOARD_CHECK_KIND = "stats.board_check"


async def poll_board_observed(state: Any, provider: Any) -> dict[str, Any]:
    """Observe real upstream polls, preserving callers' cache and retry behavior."""
    succeeded: bool | None = None
    reason = ""
    try:
        board: dict[str, Any] = await provider.poll_board()
        succeeded = True
    except Exception as exc:
        succeeded = False
        reason = (str(exc) or type(exc).__name__)[:MAX_REASON_CHARS]
        raise
    finally:
        store = state.get("event_store")
        if store is not None and succeeded is not None:
            try:
                store.emit({
                    "symphony": state.get("current_symphony") or "",
                    "source": "daemon", "kind": BOARD_CHECK_KIND,
                    "level": "info" if succeeded else "error",
                    "message": reason, "data": {"success": succeeded},
                })
            except Exception as exc:
                logger.warning("stats.board_check_record_failed", error_type=type(exc).__name__)
    return board

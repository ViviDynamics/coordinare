from __future__ import annotations

from typing import Any

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
        reason = str(exc) or type(exc).__name__
        raise
    finally:
        store = state.get("event_store")
        if store is not None and succeeded is not None:
            store.emit({
                "symphony": state.get("current_symphony") or "",
                "source": "daemon", "kind": BOARD_CHECK_KIND,
                "level": "info" if succeeded else "error",
                "message": reason, "data": {"success": succeeded},
            })
    return board

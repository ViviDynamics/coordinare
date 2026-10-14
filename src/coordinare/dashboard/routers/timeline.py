"""147: the card pipeline timeline route. Read-only.

``GET /api/cards/{symphony}/{card}/timeline`` is the one read behind the card detail's tracker
and its lifecycle list. The symphony is part of the address and is enforced: a card id that
belongs to another symphony, or to none the daemon knows, is 404 under this name. Every JSON
route is under ``/api/``, so the dashboard authentication covers it. File reads run on a
worker thread.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from fastapi.responses import JSONResponse

from coordinare.dashboard.card_pipeline import assemble_timeline, transcript_root
from coordinare.lib.redaction import redact_state_value

if TYPE_CHECKING:
    from fastapi import FastAPI

    from coordinare.dashboard.context import DashboardContext

_NO_STORE = {"Cache-Control": "no-store"}
_NOT_FOUND = "no card with that id in this symphony"


def _redacted(payload: dict[str, Any]) -> dict[str, Any]:
    """Free text leaves the server through the same guard the state uses (203)."""
    cleaned = redact_state_value(payload)
    return cleaned if isinstance(cleaned, dict) else {}


def register(app: FastAPI, ctx: DashboardContext) -> None:
    daemon = ctx.daemon

    @app.get("/api/cards/{symphony}/{card}/timeline")
    async def card_timeline(symphony: str, card: str) -> JSONResponse:
        """One card's pipeline: its stages, runs, lifecycle events, tokens and cost (147)."""
        root = transcript_root(daemon)
        payload = await asyncio.to_thread(assemble_timeline, daemon, root, symphony=symphony, card_id=card)
        if payload is None:
            return JSONResponse({"error": "not_found", "detail": _NOT_FOUND}, status_code=404, headers=_NO_STORE)
        return JSONResponse(_redacted(payload), headers=_NO_STORE)

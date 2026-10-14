"""The History page's run listing (spec 150). One read-only, paginated route.

``GET /api/history/runs`` lists runs from the transcripts and the token ledger (see
``coordinare.services.run_history``). The scope is the symphonies this daemon runs: a symphony
filter names one of them or is refused with 404, and no other symphony's folder is read. The
route is under ``/api/``, so the dashboard authentication covers it. Disk reads run on a worker
thread.
"""

from __future__ import annotations

import asyncio
from datetime import date
from typing import TYPE_CHECKING, Annotated, Any, Literal

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from coordinare.services import run_history
from coordinare.services.run_history import (
    HISTORY_DEFAULT_LIMIT,
    HISTORY_MAX_LIMIT,
    HistoryFilter,
    HistoryQuery,
    HistoryQueryError,
    HistorySymphonyError,
    UsageSource,
)
from coordinare.services.run_transcript import TranscriptCollector
from coordinare.services.token_usage import price_table_from_config

if TYPE_CHECKING:
    from coordinare.dashboard.context import DashboardContext
    from coordinare.services.token_usage import TokenLedger

_NO_STORE = {"Cache-Control": "no-store"}
_NOT_CONFIGURED = "transcripts are not configured on this daemon"


class HistoryParams(BaseModel):
    """The query of one History page. Every filter is optional and is applied on the server."""

    symphony: str | None = None
    card: str | None = None
    stage: str | None = None
    outcome: Literal["completed", "failed", "unknown"] | None = None
    # ISO days (UTC). Read as text so a bad day is a 400 with a reason, not a validation dump.
    since: str | None = None
    until: str | None = None
    limit: int = Field(HISTORY_DEFAULT_LIMIT, ge=1, le=HISTORY_MAX_LIMIT)
    cursor: str | None = None


def _empty(reason: str) -> JSONResponse:
    body: dict[str, Any] = {
        "available": False, "reason": reason, "rows": [], "next_cursor": None,
        "partial": False, "scanned": 0, "examined": 0,
    }
    return JSONResponse(body, headers=_NO_STORE)


def _ledger(daemon: Any) -> TokenLedger | None:
    """The token ledger when transcription is on (134), else None: tokens are then not tracked."""
    collector = daemon.state.get("transcript_collector")
    if isinstance(collector, TranscriptCollector) and collector.enabled:
        return collector.usage
    return None


def _day(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        message = f"not an ISO day: {raw!r}"
        raise HistoryQueryError(message) from exc


def _query_of(params: HistoryParams, visible: frozenset[str]) -> HistoryQuery:
    filters = HistoryFilter(
        symphony=params.symphony or None,
        card=params.card or None,
        stage=params.stage or None,
        outcome=params.outcome,
        since=_day(params.since),
        until=_day(params.until),
    )
    return HistoryQuery(filters=filters, visible=visible, limit=params.limit, cursor=params.cursor or None)


def _register_listing(app: FastAPI, ctx: DashboardContext) -> None:
    daemon = ctx.daemon

    @app.get("/api/history/runs")
    async def history_runs(params: Annotated[HistoryParams, Query()]) -> JSONResponse:
        """One page of runs, newest first, filtered by symphony, card, stage, outcome and date."""
        collector = daemon.state.get("transcript_collector")
        if not isinstance(collector, TranscriptCollector):
            return _empty(_NOT_CONFIGURED)
        visible = frozenset(daemon.state.get("symphony_configs") or {})
        usage = UsageSource(
            ledger=_ledger(daemon),
            table=price_table_from_config(getattr(daemon.state.get("config"), "cost_tracking", None)),
        )
        try:
            query = _query_of(params, visible)
            body = await asyncio.to_thread(run_history.list_history, collector.root, query, usage)
        except HistorySymphonyError as exc:
            return JSONResponse({"error": "not_found", "detail": str(exc)}, status_code=404, headers=_NO_STORE)
        except HistoryQueryError as exc:
            return JSONResponse({"error": "bad_request", "detail": str(exc)}, status_code=400, headers=_NO_STORE)
        return JSONResponse({"available": True, **body}, headers=_NO_STORE)


def register(app: FastAPI, ctx: DashboardContext) -> None:
    _register_listing(app, ctx)

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Annotated

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

from coordinare.lib.redaction import redact_mapping, redact_state_value
from coordinare.services.card_lifecycle import CardLifecycleLog
from coordinare.services.event_store_query import EventQueryTooBroad
from coordinare.services.persona_history import persona_history_root
from coordinare.services.run_statistics import RunStatistics
from coordinare.services.run_transcript import TranscriptCollector
from coordinare.services.stats_params import StatsQuery
from coordinare.services.token_usage import price_table_from_config

if TYPE_CHECKING:
    from coordinare.dashboard.context import DashboardContext

NO_STORE = {"Cache-Control": "no-store"}
StatsQueryParams = Annotated[StatsQuery, Query()]


def register(app: FastAPI, ctx: DashboardContext) -> None:
    collector = ctx.daemon.state.get("transcript_collector")
    event_store = ctx.daemon.state.get("event_store")
    service = RunStatistics(
        collector.root
        if isinstance(collector, TranscriptCollector) and collector.enabled
        else None,
        event_path=event_store.path if event_store is not None else None,
    )

    @app.get("/api/stats")
    async def query_stats(params: StatsQueryParams) -> JSONResponse:
        state = ctx.daemon.state
        visible = frozenset(state.get("symphony_configs") or {})
        log = state.get("card_lifecycle")
        snapshot = log.to_snapshot() if isinstance(log, CardLifecycleLog) else {}
        boards = {
            name: getattr(sym, "board_snapshot", None)
            for name, sym in (state.get("symphony_states") or {}).items()
            if name in visible
        }
        service.table = price_table_from_config(getattr(state.get("config"), "cost_tracking", None))
        service.persona_root = persona_history_root(state.get("config"))
        try:
            body = await service.query(params, visible=visible, lifecycle=snapshot, boards=boards)
        except LookupError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404, headers=NO_STORE)
        except EventQueryTooBroad as exc:
            return JSONResponse({"error": str(exc)}, status_code=422, headers=NO_STORE)
        except (sqlite3.Error, OSError):
            return JSONResponse(
                {"error": "statistics history unavailable"}, status_code=503, headers=NO_STORE,
            )
        if params.agent:
            scope = frozenset({params.symphony}) if params.symphony else visible
            body["runtime"] = service.persona_runtime(state, params.agent, scope)
        return JSONResponse(redact_mapping(redact_state_value(body)), headers=NO_STORE)

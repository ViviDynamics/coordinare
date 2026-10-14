"""Run transcript routes for the dashboard transcript viewer (spec 133). Read-only.

Every JSON route is under ``/api/``, so the dashboard authentication covers it. File
reads run on a worker thread, so a slow disk never stalls the event loop. A missing run
is 404, a refused path (a link, a folder, several names) is 403, and an address that is
not in the writer's sanitized form is 404 before any disk is touched.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Any

import structlog
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from coordinare.dashboard.assets import _page_html
from coordinare.services import run_viewer
from coordinare.services.run_transcript import TranscriptCollector
from coordinare.services.run_viewer import (
    PAGE_DEFAULT_LIMIT,
    PAGE_MAX_LIMIT,
    SEARCH_DEFAULT_LIMIT,
    SEARCH_MAX_LIMIT,
    RunLookupError,
    RunRef,
    RunRefusedError,
)

if TYPE_CHECKING:
    from pathlib import Path

    from coordinare.dashboard.context import DashboardContext

_log = structlog.get_logger(__name__)

_NO_STORE = {"Cache-Control": "no-store"}
_NOT_CONFIGURED = "transcripts are not configured on this daemon"


class RecordsQuery(BaseModel):
    """The query of one records page. ``seq`` or ``at`` positions the page on an event."""

    after: int = Field(0, ge=0)
    limit: int = Field(PAGE_DEFAULT_LIMIT, ge=1, le=PAGE_MAX_LIMIT)
    types: str = ""
    errors: bool = False
    seq: int | None = Field(None, ge=0)
    at: str | None = None


class SearchQuery(BaseModel):
    """The query of one search. Matches are looked for in every record, from ``after`` on."""

    q: str = ""
    types: str = ""
    errors: bool = False
    after: int = Field(0, ge=0)
    limit: int = Field(SEARCH_DEFAULT_LIMIT, ge=1, le=SEARCH_MAX_LIMIT)


def _transcript_root(daemon: Any) -> Path | None:
    """The transcripts root, read from the daemon's state. None when there is no collector."""
    collector = daemon.state.get("transcript_collector")
    if not isinstance(collector, TranscriptCollector):
        return None
    return collector.root


def _error_response(exc: Exception) -> JSONResponse:
    if isinstance(exc, RunRefusedError):
        return JSONResponse({"error": "refused", "detail": str(exc)}, status_code=403, headers=_NO_STORE)
    return JSONResponse({"error": "not_found", "detail": str(exc)}, status_code=404, headers=_NO_STORE)


def _parse_types(raw: str) -> frozenset[str]:
    names = [part.strip() for part in raw.split(",") if part.strip()]
    return run_viewer.known_types(names)


def _parse_at(raw: str) -> datetime | None:
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _read_records(
    root: Path, ref: RunRef, params: RecordsQuery, when: datetime | None,
) -> dict[str, Any]:
    """One page, opened on the file. Positioning by seq or time moves the start line first."""
    record_filter = run_viewer.RecordFilter(types=_parse_types(params.types), errors=params.errors)
    path = run_viewer.run_file(root, ref)
    with run_viewer.open_run(path, ref) as handle:
        focus: int | None = None
        start = params.after
        if params.seq is not None:
            focus = run_viewer.line_for_seq(handle, params.seq)
        elif when is not None:
            focus = run_viewer.line_for_time(handle, when)
        if focus is not None:
            start = max(0, focus - 1)
        page = run_viewer.read_page(
            handle, after=start, limit=params.limit,
            types=record_filter.types, errors=record_filter.errors,
        )
    return {
        "after": start,
        "next_after": page.next_after,
        "focus_line": focus,
        "types": sorted(record_filter.types),
        "records": page.records,
    }


def _scan_matches(root: Path, ref: RunRef, params: SearchQuery) -> dict[str, Any]:
    record_filter = run_viewer.RecordFilter(types=_parse_types(params.types), errors=params.errors)
    path = run_viewer.run_file(root, ref)
    with run_viewer.open_run(path, ref) as handle:
        page = run_viewer.search(
            handle, query=params.q, record_filter=record_filter, after=params.after, limit=params.limit,
        )
    return {"query": params.q, "next_after": page.next_after, "matches": page.records}


def _register_listings(app: FastAPI, ctx: DashboardContext) -> None:
    daemon = ctx.daemon

    @app.get("/api/card-runs/{card}")
    async def card_runs(card: str) -> JSONResponse:
        """The runs for one card under every symphony, newest first (the card detail's list)."""
        root = _transcript_root(daemon)
        if root is None:
            return JSONResponse({"available": False, "reason": _NOT_CONFIGURED, "runs": [], "truncated": False},
                                headers=_NO_STORE)
        try:
            listing = await asyncio.to_thread(run_viewer.list_runs, root, symphony=None, card=card)
        except RunLookupError as exc:
            return _error_response(exc)
        return JSONResponse({"available": True, **listing}, headers=_NO_STORE)

    @app.get("/api/runs/{symphony}/{card}")
    async def symphony_card_runs(symphony: str, card: str) -> JSONResponse:
        """The runs for one card under one symphony, newest first."""
        root = _transcript_root(daemon)
        if root is None:
            return JSONResponse({"available": False, "reason": _NOT_CONFIGURED, "runs": [], "truncated": False},
                                headers=_NO_STORE)
        try:
            listing = await asyncio.to_thread(run_viewer.list_runs, root, symphony=symphony, card=card)
        except RunLookupError as exc:
            return _error_response(exc)
        return JSONResponse({"available": True, **listing}, headers=_NO_STORE)

    @app.get("/api/run-lookup/{run}")
    async def run_lookup(run: str) -> JSONResponse:
        """Where one run lives (symphony and card), so a link that names only the run can open it."""
        root = _transcript_root(daemon)
        if root is None:
            return JSONResponse({"error": "not_found", "detail": _NOT_CONFIGURED}, status_code=404,
                                headers=_NO_STORE)
        try:
            ref = await asyncio.to_thread(run_viewer.resolve_run, root, run)
        except RunLookupError as exc:
            return _error_response(exc)
        return JSONResponse({"symphony": ref.symphony, "card": ref.card, "run": ref.run}, headers=_NO_STORE)


def _register_run_reads(app: FastAPI, ctx: DashboardContext) -> None:
    _register_run_detail(app, ctx)
    _register_run_records(app, ctx)
    _register_run_full_field(app, ctx)


def _register_run_detail(app: FastAPI, ctx: DashboardContext) -> None:
    daemon = ctx.daemon

    @app.get("/api/runs/{symphony}/{card}/{run}")
    async def run_detail(symphony: str, card: str, run: str) -> JSONResponse:
        """One run's header and summary: stage, agent, model, endpoint, persona version, tokens, outcome, PR."""
        root = _transcript_root(daemon)
        if root is None:
            return JSONResponse({"error": "not_found", "detail": _NOT_CONFIGURED}, status_code=404,
                                headers=_NO_STORE)
        ref = RunRef(symphony=symphony, card=card, run=run)
        try:
            detail = await asyncio.to_thread(run_viewer.run_detail, root, ref)
        except (RunLookupError, RunRefusedError) as exc:
            return _error_response(exc)
        return JSONResponse(detail, headers=_NO_STORE)



def _register_run_records(app: FastAPI, ctx: DashboardContext) -> None:
    daemon = ctx.daemon

    @app.get("/api/runs/{symphony}/{card}/{run}/records")
    async def run_records(
        symphony: str, card: str, run: str, params: Annotated[RecordsQuery, Query()],
    ) -> JSONResponse:
        """One page of records, after a line number. ``seq`` or ``at`` positions the page first."""
        root = _transcript_root(daemon)
        if root is None:
            return JSONResponse({"error": "not_found", "detail": _NOT_CONFIGURED}, status_code=404,
                                headers=_NO_STORE)
        when = None
        if params.at is not None:
            when = _parse_at(params.at)
            if when is None:
                return JSONResponse({"error": "bad_request", "detail": "at is not an ISO timestamp"},
                                    status_code=400, headers=_NO_STORE)
        ref = RunRef(symphony=symphony, card=card, run=run)
        try:
            body = await asyncio.to_thread(_read_records, root, ref, params, when)
        except (RunLookupError, RunRefusedError) as exc:
            return _error_response(exc)
        return JSONResponse(body, headers=_NO_STORE)

    @app.get("/api/runs/{symphony}/{card}/{run}/search")
    async def run_search(symphony: str, card: str, run: str, params: Annotated[SearchQuery, Query()]) -> JSONResponse:
        """Matches over the full text of every record, from a line onward. Streamed, never loaded whole."""
        root = _transcript_root(daemon)
        if root is None:
            return JSONResponse({"error": "not_found", "detail": _NOT_CONFIGURED}, status_code=404,
                                headers=_NO_STORE)
        ref = RunRef(symphony=symphony, card=card, run=run)
        try:
            body = await asyncio.to_thread(_scan_matches, root, ref, params)
        except (RunLookupError, RunRefusedError) as exc:
            return _error_response(exc)
        return JSONResponse(body, headers=_NO_STORE)



def _register_run_full_field(app: FastAPI, ctx: DashboardContext) -> None:
    daemon = ctx.daemon

    @app.get("/api/runs/{symphony}/{card}/{run}/records/{line}/{field}")
    async def run_full_field(symphony: str, card: str, run: str, line: int, field: str) -> JSONResponse:
        """One field of one record, in full. Tool output and long text are fetched here on demand."""
        root = _transcript_root(daemon)
        if root is None or line < 1:
            return JSONResponse({"error": "not_found", "detail": _NOT_CONFIGURED}, status_code=404,
                                headers=_NO_STORE)
        ref = RunRef(symphony=symphony, card=card, run=run)

        def _read() -> dict[str, Any]:
            path = run_viewer.run_file(root, ref)
            with run_viewer.open_run(path, ref) as handle:
                return run_viewer.full_field(handle, line=line, field_name=field)

        try:
            body = await asyncio.to_thread(_read)
        except (RunLookupError, RunRefusedError) as exc:
            return _error_response(exc)
        return JSONResponse(body, headers=_NO_STORE)


def _register_downloads(app: FastAPI, ctx: DashboardContext) -> None:
    daemon = ctx.daemon

    @app.get("/api/runs/{symphony}/{card}/{run}/download.jsonl")
    async def download_jsonl(symphony: str, card: str, run: str) -> Response:
        """The stored file, as it is on disk (already redacted), streamed."""
        root = _transcript_root(daemon)
        if root is None:
            return JSONResponse({"error": "not_found", "detail": _NOT_CONFIGURED}, status_code=404,
                                headers=_NO_STORE)
        ref = RunRef(symphony=symphony, card=card, run=run)

        def _open() -> Any:
            return run_viewer.open_run(run_viewer.run_file(root, ref), ref)

        try:
            handle = await asyncio.to_thread(_open)
        except (RunLookupError, RunRefusedError) as exc:
            return _error_response(exc)
        return StreamingResponse(
            run_viewer.iter_bytes(handle),
            media_type="application/x-ndjson",
            headers={"Content-Disposition": f'attachment; filename="{run}.jsonl"', **_NO_STORE},
        )

    @app.get("/api/runs/{symphony}/{card}/{run}/download.md")
    async def download_markdown(symphony: str, card: str, run: str) -> Response:
        """The run as Markdown, generated while it streams. Every value is fenced."""
        root = _transcript_root(daemon)
        if root is None:
            return JSONResponse({"error": "not_found", "detail": _NOT_CONFIGURED}, status_code=404,
                                headers=_NO_STORE)
        ref = RunRef(symphony=symphony, card=card, run=run)

        def _open() -> Any:
            return run_viewer.open_run(run_viewer.run_file(root, ref), ref)

        try:
            handle = await asyncio.to_thread(_open)
        except (RunLookupError, RunRefusedError) as exc:
            return _error_response(exc)
        return StreamingResponse(
            (chunk.encode("utf-8", "surrogatepass") for chunk in run_viewer.iter_markdown(handle, ref)),
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{run}.md"', **_NO_STORE},
        )


def _register_run_pages(app: FastAPI, ctx: DashboardContext) -> None:
    _assistant_on = ctx.assistant_on

    @app.get("/runs/{run}", response_class=HTMLResponse)
    async def dashboard_run_by_id(run: str) -> HTMLResponse:
        """133: a run named by its id alone (activity links). The client resolves it to the full address."""
        return HTMLResponse(_page_html(_assistant_on))

    @app.get("/runs/{symphony}/{card}/{run}", response_class=HTMLResponse)
    async def dashboard_run(symphony: str, card: str, run: str) -> HTMLResponse:
        """133: the transcript viewer for one run. The client checks the address and loads it."""
        return HTMLResponse(_page_html(_assistant_on))


def register(app: FastAPI, ctx: DashboardContext) -> None:
    _register_listings(app, ctx)
    _register_run_reads(app, ctx)
    _register_downloads(app, ctx)
    _register_run_pages(app, ctx)

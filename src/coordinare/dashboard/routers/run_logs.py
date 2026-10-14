"""Full run log routes (132): one run's complete log, paged as text or downloaded whole.

The routes sit behind the dashboard's authentication like every other ``/api`` route.
The run id is validated by the run log module (one safe path segment), and a link or a
hard link is refused there, so a request never reaches a file outside the root. Pages
are read with ``pread`` and a download is streamed, so a large log is never held in
memory.
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Annotated

from fastapi import Query
from fastapi.responses import PlainTextResponse, StreamingResponse

from coordinare.services.run_log import (
    RunLogCollector,
    RunLogRefusedError,
    iter_run_log_file,
    read_run_log_window,
    run_log_size,
)

if TYPE_CHECKING:
    from fastapi import FastAPI
    from starlette.responses import Response

    from coordinare.dashboard.context import DashboardContext

# The largest page a client may ask for. A download is the whole file, streamed.
MAX_PAGE_BYTES = 1024 * 1024
# The smallest page: a UTF-8 character is at most four bytes, so every page makes progress.
MIN_PAGE_BYTES = 4
DEFAULT_PAGE_BYTES = 64 * 1024
_NOT_FOUND_TEXT = "No full log is kept for this run.\n"


def _not_found() -> PlainTextResponse:
    return PlainTextResponse(_NOT_FOUND_TEXT, status_code=404)


def register(app: FastAPI, ctx: DashboardContext) -> None:
    daemon = ctx.daemon

    @app.get("/api/runs/{run_id}/log")
    async def run_log(
        run_id: str,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=MIN_PAGE_BYTES, le=MAX_PAGE_BYTES)] = DEFAULT_PAGE_BYTES,
        download: bool = False,
    ) -> Response:
        """A run's full log: a page of text at ``offset``, or the whole file with ``download=1``.

        A page carries ``X-Log-Size`` (the file's size) and, when more follows,
        ``X-Log-Next-Offset``. A run whose log was never collected, or whose id is not
        a safe name, or whose file is a link, is a 404.
        """
        collector = daemon.state.get("run_log_collector")
        if not isinstance(collector, RunLogCollector) or not collector.enabled:
            return _not_found()
        root = collector.root
        try:
            if download:
                # The file is opened only when the stream starts, so a download that is
                # never read holds no descriptor.
                size = await asyncio.to_thread(run_log_size, root, run_id)
                if size is None:
                    return _not_found()
                return StreamingResponse(
                    iter_run_log_file(root, run_id),
                    media_type="text/plain; charset=utf-8",
                    headers={
                        "Content-Length": str(size),
                        "Content-Disposition": f'attachment; filename="{run_id}.log"',
                    },
                )
            window = await asyncio.to_thread(
                read_run_log_window, root, run_id, offset=offset, limit=limit,
            )
        except RunLogRefusedError:
            return _not_found()
        if window is None:
            return _not_found()
        headers = {"X-Log-Size": str(window.size)}
        if window.next_offset is not None:
            headers["X-Log-Next-Offset"] = str(window.next_offset)
        return PlainTextResponse(window.text, headers=headers)

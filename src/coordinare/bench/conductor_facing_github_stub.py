"""A GitHub GraphQL boundary that satisfies coordinare's own startup (issue #224).

The Helm chart's smoke test could verify everything the chart is responsible for —
objects created, config parsed, credential authenticating, state volume mounted —
but not that the daemon reports itself **healthy**. Coordinare's startup resolves a
GitHub Project *before* the health server starts, so with no resolvable board there
is no endpoint to probe, and spec 147's SC-001 had to be narrowed to say so.

Pointing a test at a live board was the wrong fix: it would poll real cards and
could act on them. Coordinare already supports a configurable API base for GitHub
Enterprise (``github_graphql_url`` / ``github_api_url``, wired at
``__main__.py:797``), so the honest fix is to answer the two queries startup
actually makes.

Distinct from ``fake_github_server`` next door, which is **performer**-facing: it
answers the REST calls a performer makes while doing work. This one answers what
*coordinare* asks before it will start at all. Same idea, opposite side of the
boundary.

Deliberately minimal. It is not a GitHub emulator, and growing it into one would
make the smoke test depend on a second implementation of GitHub's semantics.
"""

from __future__ import annotations

import contextlib
from typing import Any

from aiohttp import web

#: What coordinare's ``initialize()`` looks for. The names must match the lanes the
#: board poller maps, or the daemon starts and then finds a board it cannot read.
STATUS_OPTIONS = [
    "Backlog",
    "Ready",
    "In progress",
    "In review",
    "Blocked",
    "Done",
]

PROJECT_ID = "PVT_stub_project"
STATUS_FIELD_ID = "PVTSSF_stub_status"


def _find_project_response(title: str) -> dict[str, Any]:
    return {
        "data": {
            "repositoryOwner": {
                "projectV2": {"id": PROJECT_ID, "title": title},
            },
        },
    }


def _project_fields_response() -> dict[str, Any]:
    return {
        "data": {
            "node": {
                "fields": {
                    "nodes": [
                        {
                            "id": STATUS_FIELD_ID,
                            "name": "Status",
                            "options": [
                                {"id": f"opt_{name.lower().replace(' ', '_')}", "name": name}
                                for name in STATUS_OPTIONS
                            ],
                        },
                    ],
                },
            },
        },
    }


def _empty_board_response() -> dict[str, Any]:
    """A board with no cards.

    An empty board is the right default for a smoke test: the daemon starts,
    reports healthy, and dispatches nothing. A board with cards would have it try
    to do work, which is not what is being verified and would need a great deal
    more of GitHub to be faked.
    """
    return {
        "data": {
            "node": {
                "items": {
                    "nodes": [],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                },
            },
        },
    }


class CoordinareFacingGitHubStub:
    """Answers the GraphQL queries coordinare makes before it will serve health."""

    def __init__(self, *, project_title: str = "smoke", host: str = "0.0.0.0", port: int = 8099):
        self._project_title = project_title
        self._host = host
        self._port = port
        self._runner: web.AppRunner | None = None
        #: Every query received, so a test can assert what startup actually asked
        #: for rather than assuming.
        self.queries: list[str] = []

    async def _graphql(self, request: web.Request) -> web.Response:
        payload = await request.json()
        query = str(payload.get("query", ""))
        self.queries.append(query)

        if "repositoryOwner" in query:
            return web.json_response(_find_project_response(self._project_title))
        if "fields(first:" in query.replace(" ", "") or "ProjectV2SingleSelectField" in query:
            return web.json_response(_project_fields_response())
        if "items(" in query.replace(" ", ""):
            return web.json_response(_empty_board_response())

        # Unknown query: say so rather than returning an empty success, which
        # would surface later as a confusing parse failure far from the cause.
        return web.json_response(
            {"errors": [{"message": f"stub does not implement: {query[:120]}"}]},
            status=200,
        )

    async def _rest_catch_all(self, request: web.Request) -> web.Response:
        return web.json_response({})

    async def start(self) -> str:
        """Start serving; returns the base URL."""
        app = web.Application()
        app.router.add_post("/graphql", self._graphql)
        app.router.add_route("*", "/{tail:.*}", self._rest_catch_all)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._port)
        await site.start()
        return f"http://{self._host}:{self._port}"

    async def stop(self) -> None:
        """Teardown-safe: never raises, so a failing test still cleans up."""
        if self._runner is not None:
            with contextlib.suppress(Exception):  # best effort
                await self._runner.cleanup()
            self._runner = None


async def _main() -> None:  # pragma: no cover - container entrypoint
    import asyncio
    import os

    stub = CoordinareFacingGitHubStub(
        project_title=os.environ.get("STUB_PROJECT_TITLE", "smoke"),
        port=int(os.environ.get("STUB_PORT", "8099")),
    )
    url = await stub.start()
    print(f"coordinare-facing GitHub stub listening on {url}", flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":  # pragma: no cover
    import asyncio

    asyncio.run(_main())

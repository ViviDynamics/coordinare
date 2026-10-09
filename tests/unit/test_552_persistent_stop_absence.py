from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from coordinare.config import PerformerEndpointConfig
from coordinare.daemon import CoordinareDaemon, _compute_session_eligibilities
from coordinare.graph.nodes.monitor_pr import monitor_pr
from coordinare.graph.state import initial_state
from coordinare.services.http_performer_service import HTTPPerformerService
from coordinare.services.owned_writers import stop_owned_writers
from coordinare.session import create_session_from_card, session_to_state
from coordinare.transport.http_transport import PerformerHTTPClient


def performer_app():
    from performer.server.job_runner import JobRunner
    from performer.server.routes import register_routes

    app = FastAPI()
    app.state.runner = JobRunner(AsyncMock())
    register_routes(app)
    return app


def owner(client):
    return HTTPPerformerService(
        PerformerEndpointConfig(
            id="worker",
            roles=["implementing", "documenting"],
            mode="persistent",
            endpoint="http://performer",
            image="performer:base",
        ),
        client=PerformerHTTPClient("http://performer", client=client),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_at", ["cancel", "get"])
async def test_actual_performer_job_not_found_confirms_persistent_stop(missing_at):
    from performer.server.job_runner import JobRunner
    from performer.server.models import JobStatus

    app = performer_app()
    if missing_at == "get":
        app.state.runner._status = JobStatus(
            job_id="old", state="succeeded", started_at=datetime.now(UTC),
        )

        @app.middleware("http")
        async def restart_after_cancel(request, call_next):
            response = await call_next(request)
            if request.url.path.endswith("/cancel"):
                app.state.runner = JobRunner(AsyncMock())
            return response

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://performer",
    ) as client:
        assert await owner(client).stop_session_confirmed("old") is True


@pytest.mark.asyncio
async def test_missing_old_job_never_cancels_a_different_current_performer_job():
    from performer.server.models import JobStatus

    app = performer_app()
    app.state.runner._status = JobStatus(
        job_id="new", state="running", started_at=datetime.now(UTC),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://performer",
    ) as client:
        assert await owner(client).stop_session_confirmed("old") is True
    assert app.state.runner.current_job_id == "new"
    assert app.state.runner.get("new").state == "running"


@pytest.mark.asyncio
async def test_actual_missing_persistent_worker_releases_paused_capacity_and_resumes_on_todo():
    app = performer_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://performer",
    ) as client:
        daemon = CoordinareDaemon(AsyncMock(), poll_interval_seconds=1, max_cycles=1)
        paused = {
            "current_card": {"id": "paused", "status": "IN_PROGRESS"},
            "phase": "monitoring_performer",
            "performer_stage": "implementing",
            "agent_dispatch": {"session_id": "old", "performer_id": "worker"},
        }
        sibling = {"current_card": {"id": "sibling", "status": "TODO"}, "phase": "dispatching"}
        daemon.state.update(
            active_sessions={"paused": paused, "sibling": sibling},
            board_snapshot={"BACKLOG": ["paused"], "TODO": ["sibling"]},
            performer_services_by_id={"worker": owner(client)},
        )
        await daemon._reconcile_board_pauses()
        assert paused["agent_dispatch"] == {}
        assert paused["board_paused"] and paused["phase"] == "blocked"
        assert _compute_session_eligibilities(daemon.state, daemon.state["active_sessions"], 1)[
            "sibling"
        ].eligible
        daemon.state["board_snapshot"] = {"TODO": ["paused", "sibling"]}
        await daemon._reconcile_board_pauses()
        assert not paused["board_paused"] and paused["phase"] == "dispatching"


@pytest.mark.asyncio
async def test_closed_pr_missing_persistent_documenter_releases_guard_and_capacity():
    app = performer_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://performer",
    ) as client:
        state = initial_state()
        card = {
            "id": "card",
            "status": "IN_REVIEW",
            "pr_node_id": "PR1",
            "pr_url": "https://github.com/acme/repo/pull/1",
        }
        session = create_session_from_card(card)
        session.update(
            phase="monitoring_pr",
            documenting_side={
                "status": "running",
                "writer_active": True,
                "session_id": "old",
                "job_id": "old",
            },
        )
        sibling = {"current_card": {"id": "sibling", "status": "TODO"}, "phase": "dispatching"}
        state.update(active_sessions={"card": session, "sibling": sibling}, active_card_id="card")
        session_to_state(session, state)
        state.update(
            github_service=SimpleNamespace(
                get_pr_review_context=AsyncMock(return_value={"state": "CLOSED", "reviews": []}),
                move_card=AsyncMock(),
            ),
            performer_services={"documenting": owner(client)},
        )
        await monitor_pr(state)
        assert state["phase"] == "blocked" and not state["board_paused"]
        assert state["documenting_side"]["session_id"] is None
        assert not state["documenting_side"]["writer_active"]
        assert "closed without merging" in state["system_error_reason"]
        assert _compute_session_eligibilities(state, state["active_sessions"], 1)[
            "sibling"
        ].eligible


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_at", ["cancel", "get"])
@pytest.mark.parametrize(
    "status, body",
    [
        (404, {"detail": "Not Found"}),
        (404, {"detail": "job missing"}),
        (404, {"detail": ["job not found"]}),
        (401, {"detail": "job not found"}),
        (403, {"detail": "job not found"}),
        (500, {"detail": "job not found"}),
    ],
)
async def test_wrong_route_auth_and_server_errors_retain_owned_persistent_identity(
    missing_at, status, body,
):
    def respond(request):
        if missing_at == "get" and request.method == "POST":
            return httpx.Response(200, json={"honored": True})
        return httpx.Response(status, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        session = {
            "performer_stage": "implementing",
            "agent_dispatch": {"session_id": "old", "performer_id": "worker"},
        }
        state = {"performer_services_by_id": {"worker": owner(client)}}
        assert await stop_owned_writers(state, "card", session, reason="board paused") is False
        assert session["agent_dispatch"]["session_id"] == "old"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [httpx.ConnectError, httpx.ReadTimeout])
async def test_transport_failures_retain_owned_persistent_identity(failure):
    def respond(request):
        raise failure("unavailable", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        session = {
            "performer_stage": "implementing",
            "agent_dispatch": {"session_id": "old", "performer_id": "worker"},
        }
        assert (
            await stop_owned_writers(
                {"performer_services_by_id": {"worker": owner(client)}},
                "card",
                session,
                reason="board paused",
            )
            is False
        )
        assert session["agent_dispatch"]["session_id"] == "old"


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_at", ["cancel", "get"])
@pytest.mark.parametrize("body", ["<html>Not Found</html>", '["job not found"]'])
async def test_unrecognized_missing_route_response_retains_owned_identity(missing_at, body):
    def respond(request):
        if missing_at == "get" and request.method == "POST":
            return httpx.Response(200, json={"honored": True})
        return httpx.Response(404, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        session = {
            "performer_stage": "implementing",
            "agent_dispatch": {"session_id": "old", "performer_id": "worker"},
        }
        assert (
            await stop_owned_writers(
                {"performer_services_by_id": {"worker": owner(client)}},
                "card",
                session,
                reason="board paused",
            )
            is False
        )
        assert session["agent_dispatch"]["session_id"] == "old"

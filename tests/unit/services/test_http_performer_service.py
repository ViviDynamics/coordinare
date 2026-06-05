"""Unit tests for HTTPPerformerService (spec 056, T027)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from coordinare.models.performer_endpoint import PerformerEndpointConfig
from coordinare.services import http_performer_service as hps_mod
from coordinare.services import performer_lifecycle as lifecycle
from coordinare.services.http_performer_service import HTTPPerformerService
from coordinare.services.performer_lifecycle import StartedContainer
from coordinare.transport.http_transport import (
    PerformerHTTPClient,
)
from coordinare.workspace import WorkspaceInfo


def _persistent_config(**overrides: object) -> PerformerEndpointConfig:
    base: dict[str, object] = {
        "id": "perf-p1",
        "mode": "persistent",
        "roles": ["implementing"],
        "image": "performer:base",
        "endpoint": "http://127.0.0.1:8080",
    }
    base.update(overrides)
    return PerformerEndpointConfig.model_validate(base)


def _ephemeral_config(**overrides: object) -> PerformerEndpointConfig:
    base: dict[str, object] = {
        "id": "perf-e1",
        "mode": "ephemeral",
        "roles": ["implementing"],
        "image": "performer:base",
    }
    base.update(overrides)
    return PerformerEndpointConfig.model_validate(base)


def _client(handler) -> PerformerHTTPClient:
    return PerformerHTTPClient(
        "http://127.0.0.1:8080",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def _workspace() -> WorkspaceInfo:
    return WorkspaceInfo(
        path=None,
        repo_url="https://github.com/x/y",
        branch="main",
        github_token="ghs_xxx",
    )


def _card() -> dict[str, object]:
    return {
        "id": "card-1",
        "role": "implementer",
        "backend": "claude_code",
        "persona_instructions": "be excellent",
    }


# ---------------------------- health -----------------------------------------


@pytest.mark.asyncio
async def test_check_health_persistent_maps_idle() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "availability": "idle",
                "capabilities": {"backends": [], "tool_flags": []},
                "auth_enabled": False,
            },
        )

    svc = HTTPPerformerService(_persistent_config(), client=_client(handler))
    health = await svc.check_health()
    assert health["status"] == "idle"
    await svc.aclose()


@pytest.mark.asyncio
async def test_check_health_ephemeral_predispatch_returns_idle() -> None:
    svc = HTTPPerformerService(_ephemeral_config())
    assert (await svc.check_health()) == {"status": "idle", "availability": "idle"}


@pytest.mark.asyncio
async def test_check_health_starting_maps_unknown() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "availability": "starting",
                "capabilities": {"backends": [], "tool_flags": []},
                "auth_enabled": False,
            },
        )

    svc = HTTPPerformerService(_persistent_config(), client=_client(handler))
    health = await svc.check_health()
    assert health["status"] == "unknown"
    await svc.aclose()


# ---------------------------- dispatch (persistent) --------------------------


@pytest.mark.asyncio
async def test_dispatch_persistent_returns_session_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/jobs"
        return httpx.Response(
            202,
            json={
                "accepted": True,
                "job_id": "job-123",
                "started_at": "2026-04-28T00:00:00Z",
            },
        )

    svc = HTTPPerformerService(_persistent_config(), client=_client(handler))
    result = await svc.dispatch_card(_card(), _workspace())
    assert result["status"] == "ok"
    assert result["session_id"] == "job-123"
    assert result["accepted"] is True


@pytest.mark.asyncio
async def test_dispatch_409_busy_returns_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"accepted": False, "reason": "busy"})

    svc = HTTPPerformerService(_persistent_config(), client=_client(handler))
    result = await svc.dispatch_card(_card(), _workspace())
    assert result["status"] == "error"
    assert "busy" in result["reason"]


@pytest.mark.asyncio
async def test_dispatch_missing_workspace_context_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("should not dispatch")

    svc = HTTPPerformerService(_persistent_config(), client=_client(handler))
    result = await svc.dispatch_card({"id": "c1"}, None)
    assert result["status"] == "error"
    assert "repo_url" in result["reason"] or "branch" in result["reason"]


# ---------------------------- dispatch (ephemeral) ---------------------------


@pytest.mark.asyncio
async def test_dispatch_ephemeral_starts_container_and_dispatches(monkeypatch) -> None:
    started_calls: list[str] = []
    stop_calls: list[str] = []

    async def fake_start(config, **kwargs):
        started_calls.append(config.id)
        return StartedContainer(container_id="ctr-1", endpoint="http://127.0.0.1:55555")

    async def fake_wait_ready(endpoint, auth_token, *, timeout, performer_id, **_):
        return None

    async def fake_stop(container_id, **_):
        stop_calls.append(container_id)

    monkeypatch.setattr(lifecycle, "start_ephemeral", fake_start)
    monkeypatch.setattr(lifecycle, "wait_ready", fake_wait_ready)
    monkeypatch.setattr(lifecycle, "stop", fake_stop)
    monkeypatch.setattr(hps_mod.performer_lifecycle, "start_ephemeral", fake_start)
    monkeypatch.setattr(hps_mod.performer_lifecycle, "wait_ready", fake_wait_ready)
    monkeypatch.setattr(hps_mod.performer_lifecycle, "stop", fake_stop)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            202,
            json={
                "accepted": True,
                "job_id": "job-eph-1",
                "started_at": "2026-04-28T00:00:00Z",
            },
        )

    svc = HTTPPerformerService(_ephemeral_config(), client=_client(handler))

    async def _noop_log_poll(container_id: str, job_id: str) -> None:
        return

    monkeypatch.setattr(svc, "_poll_container_logs", _noop_log_poll)
    result = await svc.dispatch_card(_card(), _workspace())

    assert result["status"] == "ok"
    # 076 (T016): session_id is a coordinare-allocated UUID, distinct from
    # the job-runner's job_id (which is still surfaced separately).
    assert result["job_id"] == "job-eph-1"
    assert result["session_id"] != result["job_id"]
    assert len(result["session_id"]) == 36  # UUID4 length
    assert started_calls == ["perf-e1"]
    # stop NOT yet called — only on terminal state.
    assert stop_calls == []


@pytest.mark.asyncio
async def test_check_status_terminal_cleans_up_ephemeral(monkeypatch) -> None:
    stop_calls: list[str] = []

    async def fake_start(config, **kwargs):
        return StartedContainer(container_id="ctr-9", endpoint="http://127.0.0.1:55555")

    async def fake_wait_ready(*args, **kwargs):
        return None

    async def fake_stop(container_id, **_):
        stop_calls.append(container_id)

    monkeypatch.setattr(hps_mod.performer_lifecycle, "start_ephemeral", fake_start)
    monkeypatch.setattr(hps_mod.performer_lifecycle, "wait_ready", fake_wait_ready)
    monkeypatch.setattr(hps_mod.performer_lifecycle, "stop", fake_stop)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/jobs":
            return httpx.Response(
                202,
                json={
                    "accepted": True,
                    "job_id": "job-T",
                    "started_at": "2026-04-28T00:00:00Z",
                },
            )
        # GET /jobs/{id}
        return httpx.Response(
            200,
            json={
                "job_id": "job-T",
                "state": "succeeded",
                "started_at": "2026-04-28T00:00:00Z",
                "finished_at": "2026-04-28T00:01:00Z",
                "result": {"success": True, "summary": "done"},
            },
        )

    svc = HTTPPerformerService(_ephemeral_config(), client=_client(handler))

    async def _noop_log_poll(container_id: str, job_id: str) -> None:
        return

    monkeypatch.setattr(svc, "_poll_container_logs", _noop_log_poll)
    result = await svc.dispatch_card(_card(), _workspace())
    # 076 (T016): check_status takes coordinare's session_id, not the
    # job-runner's job_id.  The two are now distinct values.
    status = await svc.check_status(result["session_id"])

    # Non-JSON summary falls through to plain-text fallback: always "error"
    # so monitor_performer can terminate the session (it doesn't recognise "ok").
    assert status["status"] == "error"
    assert stop_calls == ["ctr-9"]


# ---------------------------- relay feedback ---------------------------------


@pytest.mark.asyncio
async def test_relay_feedback_buffers() -> None:
    svc = HTTPPerformerService(_persistent_config())
    result = await svc.relay_feedback({"comments": [{"body": "x"}]})
    assert result == {"status": "ok", "buffered": True}


# ---------------------------- mode / auth_token properties -------------------


def test_mode_property_returns_config_mode() -> None:
    svc = HTTPPerformerService(_persistent_config())
    assert svc.mode == "persistent"

    svc2 = HTTPPerformerService(_ephemeral_config())
    assert svc2.mode == "ephemeral"


def test_auth_token_none_returns_none() -> None:
    svc = HTTPPerformerService(_persistent_config())
    assert svc._auth_token() is None


def test_auth_token_with_value_returns_secret() -> None:
    cfg = _persistent_config(auth_token="supersecret")
    svc = HTTPPerformerService(cfg)
    assert svc._auth_token() == "supersecret"


# ---------------------------- _ensure_client / aclose ------------------------


@pytest.mark.asyncio
async def test_ensure_client_creates_client_from_endpoint() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "availability": "idle",
                "capabilities": {"backends": [], "tool_flags": []},
                "auth_enabled": False,
            },
        )

    # Use real config, no injected client — _ensure_client creates one lazily.
    import httpx as _httpx

    cfg = _persistent_config()
    svc = HTTPPerformerService(cfg)
    # Replace transport after construction to intercept real HTTP.
    svc._persistent_client = PerformerHTTPClient(
        "http://127.0.0.1:8080",
        client=_httpx.AsyncClient(transport=_httpx.MockTransport(handler)),
    )
    health = await svc.check_health()
    assert health["status"] == "idle"
    await svc.aclose()


@pytest.mark.asyncio
async def test_aclose_closes_internal_client() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "availability": "idle",
                "capabilities": {"backends": [], "tool_flags": []},
                "auth_enabled": False,
            },
        )

    svc = HTTPPerformerService(_persistent_config(), client=_client(handler))
    await svc.check_health()
    # Since we passed injected client, aclose must be a no-op for svc._client.
    await svc.aclose()


# ---------------------------- check_health error branches --------------------


@pytest.mark.asyncio
async def test_check_health_auth_error_returns_error_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "unauthorized"})

    svc = HTTPPerformerService(_persistent_config(), client=_client(handler))
    health = await svc.check_health()
    assert health["status"] == "error"
    await svc.aclose()


@pytest.mark.asyncio
async def test_check_health_unreachable_returns_unreachable_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    svc = HTTPPerformerService(_persistent_config(), client=_client(handler))
    health = await svc.check_health()
    assert health["status"] == "unreachable"
    await svc.aclose()


@pytest.mark.asyncio
async def test_check_health_draining_maps_to_draining() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "availability": "draining",
                "capabilities": {"backends": [], "tool_flags": []},
                "auth_enabled": False,
            },
        )

    svc = HTTPPerformerService(_persistent_config(), client=_client(handler))
    health = await svc.check_health()
    assert health["status"] == "draining"
    assert health["availability"] == "draining"
    await svc.aclose()


# ---------------------------- dispatch ephemeral error paths -----------------


@pytest.mark.asyncio
async def test_dispatch_ephemeral_start_failure_returns_error(monkeypatch) -> None:
    async def fake_start(config, **kwargs):
        raise lifecycle.LifecycleError("no image")

    monkeypatch.setattr(hps_mod.performer_lifecycle, "start_ephemeral", fake_start)

    svc = HTTPPerformerService(_ephemeral_config())
    result = await svc.dispatch_card(_card(), _workspace())
    assert result["status"] == "error"
    assert "container start failed" in result["reason"]


@pytest.mark.asyncio
async def test_dispatch_ephemeral_readiness_timeout_returns_error(monkeypatch) -> None:
    async def fake_start(config, **kwargs):
        return StartedContainer(container_id="ctr-rt", endpoint="http://127.0.0.1:59999")

    async def fake_wait_ready(*args, **kwargs):
        raise lifecycle.ReadinessTimeoutError("not ready")

    async def fake_stop(container_id, **_):
        pass

    monkeypatch.setattr(hps_mod.performer_lifecycle, "start_ephemeral", fake_start)
    monkeypatch.setattr(hps_mod.performer_lifecycle, "wait_ready", fake_wait_ready)
    monkeypatch.setattr(hps_mod.performer_lifecycle, "stop", fake_stop)

    svc = HTTPPerformerService(_ephemeral_config())
    result = await svc.dispatch_card(_card(), _workspace())
    assert result["status"] == "error"
    assert "readiness timeout" in result["reason"]


@pytest.mark.asyncio
async def test_dispatch_transport_error_returns_error(monkeypatch) -> None:
    async def fake_start(config, **kwargs):
        return StartedContainer(container_id="ctr-te", endpoint="http://127.0.0.1:59998")

    async def fake_wait_ready(*args, **kwargs):
        return None

    async def fake_stop(container_id, **_):
        pass

    monkeypatch.setattr(hps_mod.performer_lifecycle, "start_ephemeral", fake_start)
    monkeypatch.setattr(hps_mod.performer_lifecycle, "wait_ready", fake_wait_ready)
    monkeypatch.setattr(hps_mod.performer_lifecycle, "stop", fake_stop)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    svc = HTTPPerformerService(_ephemeral_config(), client=_client(handler))
    result = await svc.dispatch_card(_card(), _workspace())
    assert result["status"] == "error"


# ---------------------------- check_status paths -----------------------------


@pytest.mark.asyncio
async def test_check_status_non_terminal_returns_state_without_cleanup() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "job_id": "job-R",
                "state": "running",
                "started_at": "2026-04-28T00:00:00Z",
            },
        )

    svc = HTTPPerformerService(_persistent_config(), client=_client(handler))
    result = await svc.check_status("job-R")
    # Non-terminal → {"status": "working", "job_state": "running", ...}
    assert result["status"] == "working"
    assert result["job_state"] == "running"
    await svc.aclose()


@pytest.mark.asyncio
async def test_check_status_terminal_persistent_does_not_stop_container() -> None:
    stop_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/jobs":
            return httpx.Response(
                202,
                json={
                    "accepted": True,
                    "job_id": "job-P",
                    "started_at": "2026-04-28T00:00:00Z",
                },
            )
        return httpx.Response(
            200,
            json={
                "job_id": "job-P",
                "state": "succeeded",
                "started_at": "2026-04-28T00:00:00Z",
                "finished_at": "2026-04-28T00:01:00Z",
            },
        )

    svc = HTTPPerformerService(_persistent_config(), client=_client(handler))
    await svc.dispatch_card(_card(), _workspace())
    result = await svc.check_status("job-P")

    # No result in response → terminal-no-result fallback
    assert result["status"] == "error"
    assert "succeeded" in result["reason"]
    assert stop_calls == []  # persistent mode: no container to stop
    await svc.aclose()


@pytest.mark.asyncio
async def test_check_health_unknown_availability_returns_unknown_status() -> None:
    """Availability value outside the known set maps to 'unknown' status (line 128)."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "availability": "unknown",
                "capabilities": {"backends": [], "tool_flags": []},
                "auth_enabled": False,
            },
        )

    svc = HTTPPerformerService(_persistent_config(), client=_client(handler))
    health = await svc.check_health()
    assert health["status"] == "unknown"
    await svc.aclose()


@pytest.mark.asyncio
async def test_check_status_terminal_with_mismatched_job_id() -> None:
    """Terminal result where current_job_id != session_id — no current_job_id reset."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "job_id": "job-other",
                "state": "succeeded",
                "started_at": "2026-04-28T00:00:00Z",
                "finished_at": "2026-04-28T00:01:00Z",
            },
        )

    svc = HTTPPerformerService(_persistent_config(), client=_client(handler))
    result = await svc.check_status("job-other")
    # Terminal with no result → error fallback; _active_jobs is empty (persistent mode)
    assert result["status"] == "error"
    assert "succeeded" in result["reason"]
    assert svc._active_jobs == {}
    await svc.aclose()


# ---------------------------- workspace with github_token --------------------


@pytest.mark.asyncio
async def test_dispatch_includes_github_token_in_secrets() -> None:
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json
        payloads.append(_json.loads(request.content))
        return httpx.Response(
            202,
            json={
                "accepted": True,
                "job_id": "job-gh",
                "started_at": "2026-04-28T00:00:00Z",
            },
        )

    svc = HTTPPerformerService(_persistent_config(), client=_client(handler))
    ws = WorkspaceInfo(
        path=None,
        repo_url="https://github.com/x/y",
        branch="feat",
        github_token="ghs_test_token",
    )
    await svc.dispatch_card(_card(), ws)

    assert payloads, "no POST /jobs payload captured"
    assert "GITHUB_TOKEN" in payloads[0].get("secrets", {})
    await svc.aclose()


# ---------------------------- _ensure_client edge cases ----------------------


@pytest.mark.asyncio
async def test_ensure_client_no_endpoint_returns_unknown_health() -> None:
    """_ensure_client raises TransportError when _endpoint is None; check_health catches it."""
    svc = HTTPPerformerService(_persistent_config())
    svc._persistent_endpoint = None  # force None — bypasses persistent config's endpoint
    # The TransportError from _ensure_client is caught by check_health's broad handler
    result = await svc.check_health()
    assert result["status"] in {"unknown", "error"}
    await svc.aclose()


# ---------------------------- _cleanup_ephemeral edge cases ------------------


@pytest.mark.asyncio
async def test_cleanup_ephemeral_job_closes_client(monkeypatch) -> None:
    """_cleanup_ephemeral_job stops the container and closes the per-job client."""
    closed: list[str] = []
    stopped: list[str] = []

    class FakeClient:
        async def aclose(self) -> None:
            closed.append("closed")

    async def fake_stop(container_id: str, **_: object) -> None:
        stopped.append(container_id)

    monkeypatch.setattr(hps_mod.performer_lifecycle, "stop", fake_stop)

    from coordinare.services.http_performer_service import _EphemeralJob

    svc = HTTPPerformerService(_ephemeral_config())
    job = _EphemeralJob(
        container_id="ctr-clean",
        endpoint="http://127.0.0.1:9999",
        client=FakeClient(),  # type: ignore[arg-type]
    )

    await svc._cleanup_ephemeral_job(job)

    assert stopped == ["ctr-clean"]
    assert closed == ["closed"]


@pytest.mark.asyncio
async def test_cleanup_ephemeral_job_by_id_removes_from_active_jobs(monkeypatch) -> None:
    """_cleanup_ephemeral_job_by_id pops the job from _active_jobs and cleans it up."""
    stopped: list[str] = []

    class FakeClient:
        async def aclose(self) -> None:
            pass

    async def fake_stop(container_id: str, **_: object) -> None:
        stopped.append(container_id)

    monkeypatch.setattr(hps_mod.performer_lifecycle, "stop", fake_stop)

    from coordinare.services.http_performer_service import _EphemeralJob

    svc = HTTPPerformerService(_ephemeral_config())
    svc._active_jobs["job-xyz"] = _EphemeralJob(
        container_id="ctr-xyz",
        endpoint="http://127.0.0.1:9999",
        client=FakeClient(),  # type: ignore[arg-type]
    )

    await svc._cleanup_ephemeral_job_by_id("job-xyz")

    assert stopped == ["ctr-xyz"]
    assert "job-xyz" not in svc._active_jobs


@pytest.mark.asyncio
async def test_cleanup_ephemeral_job_by_id_cancels_poll_task(monkeypatch) -> None:
    """_cleanup_ephemeral_job_by_id cancels the associated log poll task."""
    monkeypatch.setattr(hps_mod.performer_lifecycle, "stop", AsyncMock())

    from coordinare.services.http_performer_service import _EphemeralJob

    svc = HTTPPerformerService(_ephemeral_config())

    # Plant a never-completing task to simulate a running poll loop.
    async def _hang() -> None:
        await asyncio.sleep(3600)

    task: asyncio.Task[None] = asyncio.create_task(_hang())
    svc._log_poll_tasks["job-abc"] = task
    svc._active_jobs["job-abc"] = _EphemeralJob(
        container_id="ctr-abc",
        endpoint="http://127.0.0.1:9999",
        client=AsyncMock(),  # type: ignore[arg-type]
    )

    await svc._cleanup_ephemeral_job_by_id("job-abc")
    await asyncio.sleep(0)  # let the event loop propagate the cancellation

    assert task.cancelled()
    assert "job-abc" not in svc._log_poll_tasks
    assert "job-abc" not in svc._active_jobs


@pytest.mark.asyncio
async def test_aclose_cancels_poll_tasks(monkeypatch) -> None:
    """aclose() cancels all in-flight log poll tasks before tearing down containers."""
    monkeypatch.setattr(hps_mod.performer_lifecycle, "stop", AsyncMock())

    from coordinare.services.http_performer_service import _EphemeralJob

    svc = HTTPPerformerService(_ephemeral_config())

    async def _hang() -> None:
        await asyncio.sleep(3600)

    task1: asyncio.Task[None] = asyncio.create_task(_hang())
    task2: asyncio.Task[None] = asyncio.create_task(_hang())
    svc._log_poll_tasks["job-1"] = task1
    svc._log_poll_tasks["job-2"] = task2
    svc._active_jobs["job-1"] = _EphemeralJob("ctr-1", "http://127.0.0.1:9001", AsyncMock())  # type: ignore[arg-type]
    svc._active_jobs["job-2"] = _EphemeralJob("ctr-2", "http://127.0.0.1:9002", AsyncMock())  # type: ignore[arg-type]

    await svc.aclose()
    await asyncio.sleep(0)  # let the event loop propagate cancellations

    assert task1.cancelled()
    assert task2.cancelled()
    assert not svc._log_poll_tasks
    assert not svc._active_jobs


# ---------------------------- dispatch with workspace_info=None --------------


@pytest.mark.asyncio
async def test_dispatch_persistent_workspace_none_uses_card_urls() -> None:
    """dispatch_card with workspace_info=None falls back to repo_url/branch in card context."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            202,
            json={"accepted": True, "job_id": "job-noworkspace", "started_at": "2026-04-28T00:00:00Z"},
        )

    card = {**_card(), "repo_url": "https://github.com/x/y", "branch": "feat-1"}
    svc = HTTPPerformerService(_persistent_config(), client=_client(handler))
    result = await svc.dispatch_card(card, None)  # workspace_info=None
    assert result["status"] == "ok"
    assert result["job_id"] == "job-noworkspace"
    await svc.aclose()


# ---------------------------- ephemeral stale client teardown ----------------


@pytest.mark.asyncio
async def test_dispatch_ephemeral_tracks_job_in_active_jobs(monkeypatch) -> None:
    """Each ephemeral dispatch creates an independent entry in _active_jobs."""
    call_count = 0

    async def fake_start(config, **kwargs):
        nonlocal call_count
        call_count += 1
        return StartedContainer(
            container_id=f"ctr-{call_count}",
            endpoint=f"http://127.0.0.1:{55557 + call_count}",
        )

    async def fake_wait_ready(endpoint, auth_token, *, timeout, performer_id, **_):
        return None

    job_counter = 0

    class FakePostClient:
        async def post_job(self, payload):
            nonlocal job_counter
            job_counter += 1
            return type("Resp", (), {"accepted": True, "job_id": f"job-{job_counter}"})()

        async def aclose(self) -> None:
            pass

    monkeypatch.setattr(hps_mod.performer_lifecycle, "start_ephemeral", fake_start)
    monkeypatch.setattr(hps_mod.performer_lifecycle, "wait_ready", fake_wait_ready)
    monkeypatch.setattr(hps_mod, "PerformerHTTPClient", lambda *a, **kw: FakePostClient())

    svc = HTTPPerformerService(_ephemeral_config())

    async def _noop_log_poll(container_id: str, job_id: str) -> None:
        return

    monkeypatch.setattr(svc, "_poll_container_logs", _noop_log_poll)
    r1 = await svc.dispatch_card(_card(), _workspace())
    r2 = await svc.dispatch_card(_card(), _workspace())

    assert r1["status"] == "ok"
    assert r2["status"] == "ok"
    # Each dispatch tracked independently — no overwrite
    assert r1["job_id"] != r2["job_id"]
    # 076 (T016): _active_jobs is now keyed on coordinare-allocated session_id
    # (not the job-runner's job_id).  The job-runner's job_id lives on the
    # _EphemeralJob value as a sub-field.
    assert r1["session_id"] in svc._active_jobs
    assert r2["session_id"] in svc._active_jobs
    assert svc._active_jobs[r1["session_id"]].job_id == r1["job_id"]
    assert svc._active_jobs[r2["session_id"]].job_id == r2["job_id"]


# ---------------------------------------------------------------------------
# Missing repo_url/branch in _build_job_payload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_card_missing_repo_url_returns_error() -> None:
    """dispatch_card with missing repo_url returns error without raising."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"accepted": True, "job_id": "job-1"})

    svc = HTTPPerformerService(_persistent_config(), client=_client(handler))
    ws = WorkspaceInfo(path=None, repo_url=None, branch="main", github_token="ghs_x")
    result = await svc.dispatch_card(_card(), ws)

    assert result["status"] == "error"
    assert "missing" in result["reason"].lower()
    await svc.aclose()


# ---------------------------------------------------------------------------
# Ephemeral mode: ensure_client with active job
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_client_ephemeral_with_active_job(monkeypatch) -> None:
    """_ensure_client(job_id=...) in ephemeral mode returns per-job client."""
    async def fake_start(config, **kwargs):
        return StartedContainer(
            container_id="container-123",
            endpoint="http://localhost:8080",
        )

    class FakeClient:
        def __init__(self, endpoint: str, **kwargs):
            self.endpoint = endpoint

    async def fake_wait_ready(endpoint, auth_token, *, timeout, performer_id, **_):
        return None

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(202, json={"accepted": True, "job_id": "job-X", "started_at": "2026-04-28T00:00:00Z"})

    monkeypatch.setattr(hps_mod.performer_lifecycle, "start_ephemeral", fake_start)
    monkeypatch.setattr(hps_mod.performer_lifecycle, "wait_ready", fake_wait_ready)
    monkeypatch.setattr(hps_mod, "PerformerHTTPClient", FakeClient)

    svc = HTTPPerformerService(_ephemeral_config(), client=_client(handler))

    async def _noop_log_poll(container_id: str, job_id: str) -> None:
        return

    monkeypatch.setattr(svc, "_poll_container_logs", _noop_log_poll)

    # Dispatch a card to populate _active_jobs
    dispatch_result = await svc.dispatch_card(_card(), _workspace())
    job_id = dispatch_result["job_id"]

    # Now ensure_client with that job_id should return the cached client
    client = svc._ensure_client(job_id=job_id)
    assert client is not None


# ---------------------------------------------------------------------------
# Secrets injection: OPENAI_API_KEY and ANTHROPIC_API_KEY
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_includes_openai_api_key_in_secrets(monkeypatch) -> None:
    """OPENAI_API_KEY from env is injected into job secrets."""
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json
        payloads.append(_json.loads(request.content))
        return httpx.Response(
            202,
            json={
                "accepted": True,
                "job_id": "job-oai",
                "started_at": "2026-04-28T00:00:00Z",
            },
        )

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-openai-key")

    codex_card = {**_card(), "backend": "codex"}
    svc = HTTPPerformerService(_persistent_config(), client=_client(handler))
    await svc.dispatch_card(codex_card, _workspace())

    assert payloads, "no POST /jobs payload captured"
    assert payloads[0].get("secrets", {}).get("OPENAI_API_KEY") == "sk-test-openai-key"
    await svc.aclose()


@pytest.mark.asyncio
async def test_dispatch_includes_anthropic_api_key_in_secrets(monkeypatch) -> None:
    """ANTHROPIC_API_KEY from env is injected into job secrets."""
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json
        payloads.append(_json.loads(request.content))
        return httpx.Response(
            202,
            json={
                "accepted": True,
                "job_id": "job-ant",
                "started_at": "2026-04-28T00:00:00Z",
            },
        )

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key")

    svc = HTTPPerformerService(_persistent_config(), client=_client(handler))
    await svc.dispatch_card(_card(), _workspace())

    assert payloads, "no POST /jobs payload captured"
    assert payloads[0].get("secrets", {}).get("ANTHROPIC_API_KEY") == "sk-ant-test-key"
    await svc.aclose()


@pytest.mark.asyncio
async def test_dispatch_injects_both_api_keys_for_opencode_backends(monkeypatch) -> None:
    """opencode/junie receive both OPENAI_API_KEY and ANTHROPIC_API_KEY when set."""
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json
        payloads.append(_json.loads(request.content))
        return httpx.Response(
            202,
            json={"accepted": True, "job_id": "job-oc", "started_at": "2026-04-28T00:00:00Z"},
        )

    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    for backend in ("opencode", "junie"):
        payloads.clear()
        card = {**_card(), "backend": backend}
        svc = HTTPPerformerService(_persistent_config(), client=_client(handler))
        await svc.dispatch_card(card, _workspace())
        assert payloads, f"no POST /jobs payload captured for {backend}"
        secrets = payloads[0].get("secrets", {})
        assert secrets.get("OPENAI_API_KEY") == "sk-openai-test", f"{backend} missing OPENAI_API_KEY"
        assert secrets.get("ANTHROPIC_API_KEY") == "sk-ant-test", f"{backend} missing ANTHROPIC_API_KEY"
        await svc.aclose()


@pytest.mark.asyncio
async def test_dispatch_claude_code_honors_role_api_key_env_and_base_url(monkeypatch) -> None:
    """claude_code reads api key from custom env var and forwards base_url as ANTHROPIC_BASE_URL."""
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json
        payloads.append(_json.loads(request.content))
        return httpx.Response(
            202,
            json={"accepted": True, "job_id": "job-cc", "started_at": "2026-04-28T00:00:00Z"},
        )

    # api_key_env points at a non-default env var
    monkeypatch.setenv("LITELLM_PROXY_KEY", "sk-proxy-test")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    card = {
        **_card(),
        "backend": "claude_code",
        "api_key_env": "LITELLM_PROXY_KEY",
        "base_url": "https://proxy.internal/v1",
    }
    svc = HTTPPerformerService(_persistent_config(), client=_client(handler))
    await svc.dispatch_card(card, _workspace())

    assert payloads, "no POST /jobs payload captured"
    secrets = payloads[0].get("secrets", {})
    assert secrets.get("ANTHROPIC_API_KEY") == "sk-proxy-test"
    assert secrets.get("ANTHROPIC_BASE_URL") == "https://proxy.internal/v1"
    await svc.aclose()


@pytest.mark.asyncio
async def test_dispatch_codex_honors_role_api_key_env_and_base_url(monkeypatch) -> None:
    """codex reads api key from custom env var and forwards base_url as OPENAI_BASE_URL."""
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json
        payloads.append(_json.loads(request.content))
        return httpx.Response(
            202,
            json={"accepted": True, "job_id": "job-cx", "started_at": "2026-04-28T00:00:00Z"},
        )

    monkeypatch.setenv("LITELLM_PROXY_KEY", "sk-proxy-cx")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    card = {
        **_card(),
        "backend": "codex",
        "api_key_env": "LITELLM_PROXY_KEY",
        "base_url": "https://proxy.internal/v1",
    }
    svc = HTTPPerformerService(_persistent_config(), client=_client(handler))
    await svc.dispatch_card(card, _workspace())

    assert payloads, "no POST /jobs payload captured"
    secrets = payloads[0].get("secrets", {})
    assert secrets.get("OPENAI_API_KEY") == "sk-proxy-cx"
    assert secrets.get("OPENAI_BASE_URL") == "https://proxy.internal/v1"
    await svc.aclose()


@pytest.mark.asyncio
async def test_dispatch_does_not_inject_api_keys_for_unknown_backends(monkeypatch) -> None:
    """Unknown backends receive no provider API keys."""
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json
        payloads.append(_json.loads(request.content))
        return httpx.Response(
            202,
            json={"accepted": True, "job_id": "job-unk", "started_at": "2026-04-28T00:00:00Z"},
        )

    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-should-not-appear")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-appear")

    unknown_card = {**_card(), "backend": "unknown_backend"}
    svc = HTTPPerformerService(_persistent_config(), client=_client(handler))
    await svc.dispatch_card(unknown_card, _workspace())

    assert payloads, "no POST /jobs payload captured"
    secrets = payloads[0].get("secrets", {})
    assert "OPENAI_API_KEY" not in secrets
    assert "ANTHROPIC_API_KEY" not in secrets
    await svc.aclose()


# ---------------------------------------------------------------------------
# Ephemeral dispatch error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_card_ephemeral_transport_error_on_post_job(monkeypatch) -> None:
    """TransportError from post_job in ephemeral mode cleans up container."""
    from coordinare.transport.base import TransportError

    async def fake_start(config, **kwargs):
        return StartedContainer(
            container_id="container-456",
            endpoint="http://localhost:9090",
        )

    async def fake_wait_ready(endpoint, auth_token, *, timeout, performer_id, **_):
        return None

    stopped_containers: list[str] = []

    async def fake_stop(container_id: str, **_):
        stopped_containers.append(container_id)

    class ErrorClient:
        async def post_job(self, payload):
            raise TransportError("connection lost")

        async def aclose(self) -> None:
            pass

    monkeypatch.setattr(hps_mod.performer_lifecycle, "start_ephemeral", fake_start)
    monkeypatch.setattr(hps_mod.performer_lifecycle, "wait_ready", fake_wait_ready)
    monkeypatch.setattr(hps_mod.performer_lifecycle, "stop", fake_stop)
    monkeypatch.setattr(hps_mod, "PerformerHTTPClient", lambda *a, **kw: ErrorClient())

    svc = HTTPPerformerService(_ephemeral_config())
    result = await svc.dispatch_card(_card(), _workspace())

    assert result["status"] == "error"
    assert "container-456" in stopped_containers


@pytest.mark.asyncio
async def test_dispatch_card_ephemeral_not_accepted_cleans_up(monkeypatch) -> None:
    """When performer returns 409 busy in ephemeral mode, container is stopped."""
    async def fake_start(config, **kwargs):
        return StartedContainer(
            container_id="container-789",
            endpoint="http://localhost:8888",
        )

    async def fake_wait_ready(endpoint, auth_token, *, timeout, performer_id, **_):
        return None

    stopped_containers: list[str] = []

    async def fake_stop(container_id: str, **_):
        stopped_containers.append(container_id)

    class BusyClient:
        async def post_job(self, payload):
            return type("Resp", (), {"accepted": False, "reason": "busy", "detail": "slot taken"})()

        async def aclose(self) -> None:
            pass

    monkeypatch.setattr(hps_mod.performer_lifecycle, "start_ephemeral", fake_start)
    monkeypatch.setattr(hps_mod.performer_lifecycle, "wait_ready", fake_wait_ready)
    monkeypatch.setattr(hps_mod.performer_lifecycle, "stop", fake_stop)
    monkeypatch.setattr(hps_mod, "PerformerHTTPClient", lambda *a, **kw: BusyClient())

    svc = HTTPPerformerService(_ephemeral_config())
    result = await svc.dispatch_card(_card(), _workspace())

    assert result["status"] == "error"
    assert "performer busy" in result["reason"]
    assert "container-789" in stopped_containers


# ---------------------------------------------------------------------------
# check_status for ephemeral: unreachable container
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_status_ephemeral_unreachable_cleanup(monkeypatch) -> None:
    """When get_job fails for ephemeral job, cleanup is called."""
    from coordinare.transport.base import TransportError

    stopped_containers: list[str] = []

    async def fake_stop(container_id: str, **_):
        stopped_containers.append(container_id)

    class UnreachableClient:
        async def get_job(self, job_id: str):
            raise TransportError("container unreachable")

        async def aclose(self) -> None:
            pass

    monkeypatch.setattr(hps_mod.performer_lifecycle, "stop", fake_stop)

    svc = HTTPPerformerService(_ephemeral_config())
    # Manually populate _active_jobs to simulate an active ephemeral job
    svc._active_jobs["job-X"] = hps_mod._EphemeralJob(
        container_id="dead-container",
        endpoint="http://dead:9090",
        client=UnreachableClient(),
    )

    # check_status should cleanup on TransportError
    with pytest.raises(TransportError):
        await svc.check_status("job-X")

    # Verify cleanup was called
    assert "dead-container" in stopped_containers
    assert "job-X" not in svc._active_jobs


@pytest.mark.asyncio
async def test_check_status_ensure_client_transport_error() -> None:
    """When _ensure_client raises TransportError for persistent mode (no injected client), it's re-raised."""
    from coordinare.transport.base import TransportError

    config = _persistent_config()
    svc = HTTPPerformerService(config)
    # Manually reset the endpoint to force _ensure_client to fail
    svc._persistent_endpoint = None
    # _ensure_client will raise TransportError when endpoint is None
    with pytest.raises(TransportError):
        await svc.check_status("job-1")


@pytest.mark.asyncio
async def test_dispatch_persistent_payload_error() -> None:
    """When _build_job_payload raises ValueError in persistent mode, no cleanup happens."""
    config = _persistent_config()
    svc = HTTPPerformerService(config)

    # Missing branch in card_context triggers ValueError
    card = {
        "id": "card-1",
        "role": "implementer",
        "backend": "claude_code",
        "persona_instructions": "stub",
        # Missing repo_url and branch
    }

    result = await svc.dispatch_card(card, workspace_info=None)
    assert result["status"] == "error"
    assert "repo_url or branch" in result["reason"]


@pytest.mark.asyncio
async def test_dispatch_persistent_post_job_error(monkeypatch) -> None:
    """When post_job raises exception in persistent mode, no ephemeral cleanup happens."""
    from coordinare.transport.http_transport import PerformerUnreachableError

    class FailingClient:
        async def post_job(self, payload):
            raise PerformerUnreachableError("performer unreachable")

    config = _persistent_config()
    svc = HTTPPerformerService(config, client=FailingClient())

    from coordinare.workspace import WorkspaceInfo

    card = {
        "id": "card-1",
        "role": "implementer",
        "backend": "claude_code",
        "persona_instructions": "stub",
    }
    workspace = WorkspaceInfo(
        path=None,
        repo_url="https://github.com/x/y",
        branch="main",
        github_token="ghs_xxx",
    )

    result = await svc.dispatch_card(card, workspace)
    assert result["status"] == "error"
    assert "unreachable" in result["reason"]


# ---------------------------------------------------------------------------
# T043 (060): call_reset()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_reset_returns_true_on_2xx() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/reset":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404)

    svc = HTTPPerformerService(_persistent_config(), client=_client(handler))
    result = await svc.call_reset()
    assert result is True


@pytest.mark.asyncio
async def test_call_reset_returns_false_on_non_2xx() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/reset":
            return httpx.Response(500)
        return httpx.Response(404)

    svc = HTTPPerformerService(_persistent_config(), client=_client(handler))
    result = await svc.call_reset()
    assert result is False


@pytest.mark.asyncio
async def test_call_reset_returns_false_on_network_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    svc = HTTPPerformerService(_persistent_config(), client=_client(handler))
    result = await svc.call_reset()
    assert result is False


@pytest.mark.asyncio
async def test_call_reset_no_op_for_ephemeral() -> None:
    """call_reset() is a no-op for ephemeral performers — always returns True."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200)

    async def _fake_start(*args, **kwargs):
        from coordinare.services.performer_lifecycle import StartedContainer
        return StartedContainer(container_id="c1", endpoint="http://127.0.0.1:9999")

    async def _fake_wait(*args, **kwargs):
        return "http://127.0.0.1:9999"

    svc = HTTPPerformerService(_ephemeral_config())
    result = await svc.call_reset()
    assert result is True
    assert "/reset" not in calls


@pytest.mark.asyncio
async def test_check_status_terminal_persistent_calls_reset() -> None:
    reset_called: list[bool] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/jobs":
            return httpx.Response(
                202,
                json={"accepted": True, "job_id": "job-PR", "started_at": "2026-04-28T00:00:00Z"},
            )
        if request.url.path == "/reset":
            reset_called.append(True)
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(
            200,
            json={
                "job_id": "job-PR",
                "state": "succeeded",
                "started_at": "2026-04-28T00:00:00Z",
                "finished_at": "2026-04-28T00:01:00Z",
            },
        )

    svc = HTTPPerformerService(_persistent_config(), client=_client(handler))
    await svc.dispatch_card(_card(), _workspace())
    await svc.check_status("job-PR")

    assert reset_called == [True]
    await svc.aclose()


# ---------------------------- env_bootstrap (Option A) -----------------------


def test_build_env_bootstrap_payload_synthesizes_workspace(monkeypatch) -> None:
    """060/Option A: env_bootstrap dispatch builds JobInitPayload from
    BootstrapJobPayload's symphony_org/repo + env_spec_contents, with no
    WorkspaceInfo required."""
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    svc = HTTPPerformerService(
        _persistent_config(), client=_client(lambda r: httpx.Response(204))
    )
    card_context = {
        "job_type": "env_bootstrap",
        "symphony_name": "website",
        "symphony_org": "VividyNamics",
        "symphony_repo": "vivi-website",
        "env_spec_files": ["README.md"],
        "env_spec_contents": {"README.md": "## Setup\nrun `npm install`"},
        "cache_mount_path": "/devenv/website-abc123",
    }

    payload = svc._build_job_payload(card_context, None)

    assert payload.role == "env_bootstrap"
    assert str(payload.repo_url).startswith("https://github.com/VividyNamics/vivi-website")
    assert payload.branch.startswith("env-bootstrap-")
    assert payload.backend == "claude_code"
    assert "/devenv/website-abc123" in payload.persona
    assert "npm install" in payload.persona
    assert "activate.sh" in payload.persona  # 060/Option B: persona must mandate activation script
    assert "GITHUB_TOKEN" in payload.secrets
    assert payload.secrets["GITHUB_TOKEN"].get_secret_value() == "ghs_test"
    assert "ANTHROPIC_API_KEY" in payload.secrets


def test_build_job_payload_forwards_env_cache_path(monkeypatch) -> None:
    """060/Option B: env_cache_path on card_context must flow into JobInitPayload."""
    from coordinare.workspace import WorkspaceInfo

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    svc = HTTPPerformerService(
        _persistent_config(), client=_client(lambda r: httpx.Response(204))
    )
    card_context = {
        "id": "card-1",
        "role": "implementing",
        "backend": "claude_code",
        "persona_instructions": "do the thing",
        "env_cache_path": "/devenv/website-abc123",
    }
    workspace_info = WorkspaceInfo(
        path=None,
        repo_url="https://github.com/org/repo",
        branch="feat/x",
        github_token="ghs_test",
    )
    payload = svc._build_job_payload(card_context, workspace_info)
    assert payload.env_cache_path == "/devenv/website-abc123"


def test_build_job_payload_omits_empty_env_cache_path(monkeypatch) -> None:
    from coordinare.workspace import WorkspaceInfo

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    svc = HTTPPerformerService(
        _persistent_config(), client=_client(lambda r: httpx.Response(204))
    )
    workspace_info = WorkspaceInfo(
        path=None,
        repo_url="https://github.com/org/repo",
        branch="feat/x",
        github_token="ghs_test",
    )
    payload = svc._build_job_payload(
        {"id": "c", "role": "r", "backend": "claude_code"}, workspace_info
    )
    assert payload.env_cache_path is None


def test_build_env_bootstrap_payload_missing_org_raises() -> None:
    svc = HTTPPerformerService(
        _persistent_config(), client=_client(lambda r: httpx.Response(204))
    )
    with pytest.raises(ValueError, match="symphony_org"):
        svc._build_job_payload(
            {"job_type": "env_bootstrap", "symphony_repo": "x"}, None
        )


# ---------------------------- has_live_session (065 Fix 22) ------------------


def test_has_live_session_ephemeral_predispatch_returns_false() -> None:
    svc = HTTPPerformerService(_ephemeral_config())
    assert svc.has_live_session("sess-unknown") is False


def test_has_live_session_ephemeral_active_job_returns_true() -> None:
    from coordinare.services.http_performer_service import _EphemeralJob

    svc = HTTPPerformerService(_ephemeral_config())
    svc._active_jobs["sess-live"] = _EphemeralJob(
        container_id="ctr-1",
        endpoint="http://127.0.0.1:9000",
        client=AsyncMock(),  # type: ignore[arg-type]
    )
    assert svc.has_live_session("sess-live") is True
    assert svc.has_live_session("sess-other") is False


def test_has_live_session_persistent_with_endpoint_returns_true() -> None:
    svc = HTTPPerformerService(_persistent_config())
    assert svc.has_live_session("any-session") is True


def test_has_live_session_persistent_without_endpoint_returns_false() -> None:
    """Persistent config that somehow has no resolved endpoint reports no live session."""
    svc = HTTPPerformerService(_persistent_config())
    svc._persistent_endpoint = None
    assert svc.has_live_session("any-session") is False


def test_has_live_session_injected_client_returns_true() -> None:
    """When tests inject a client, treat it as always-live regardless of mode."""
    svc = HTTPPerformerService(
        _ephemeral_config(), client=_client(lambda r: httpx.Response(204))
    )
    assert svc.has_live_session("anything") is True

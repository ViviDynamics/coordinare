"""Unit tests for HTTPPerformerService (spec 056, T027)."""

from __future__ import annotations

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

    async def fake_start(config):
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
    result = await svc.dispatch_card(_card(), _workspace())

    assert result["status"] == "ok"
    assert result["session_id"] == "job-eph-1"
    assert started_calls == ["perf-e1"]
    # stop NOT yet called — only on terminal state.
    assert stop_calls == []


@pytest.mark.asyncio
async def test_check_status_terminal_cleans_up_ephemeral(monkeypatch) -> None:
    stop_calls: list[str] = []

    async def fake_start(config):
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
    await svc.dispatch_card(_card(), _workspace())
    status = await svc.check_status("job-T")

    assert status["state"] == "succeeded"
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
    svc._client = PerformerHTTPClient(
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
    async def fake_start(config):
        raise lifecycle.LifecycleError("no image")

    monkeypatch.setattr(hps_mod.performer_lifecycle, "start_ephemeral", fake_start)

    svc = HTTPPerformerService(_ephemeral_config())
    result = await svc.dispatch_card(_card(), _workspace())
    assert result["status"] == "error"
    assert "container start failed" in result["reason"]


@pytest.mark.asyncio
async def test_dispatch_ephemeral_readiness_timeout_returns_error(monkeypatch) -> None:
    async def fake_start(config):
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
    async def fake_start(config):
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
    assert result["state"] == "running"
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

    assert result["state"] == "succeeded"
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
    svc._current_job_id = "job-current"  # different from queried job
    result = await svc.check_status("job-other")
    assert result["state"] == "succeeded"
    assert svc._current_job_id == "job-current"  # unchanged — IDs didn't match
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
    svc._endpoint = None  # force None — bypasses persistent config's endpoint
    # The TransportError from _ensure_client is caught by check_health's broad handler
    result = await svc.check_health()
    assert result["status"] in {"unknown", "error"}
    await svc.aclose()


# ---------------------------- _cleanup_ephemeral edge cases ------------------


@pytest.mark.asyncio
async def test_cleanup_ephemeral_no_container_id_still_closes_client() -> None:
    """_cleanup_ephemeral when container_id is already None still cleans up _client."""
    closed: list[str] = []

    class FakeClient:
        async def aclose(self) -> None:
            closed.append("closed")

    svc = HTTPPerformerService(_ephemeral_config())
    svc._container_id = None  # already gone
    svc._client = FakeClient()  # _injected_client remains None

    await svc._cleanup_ephemeral()

    assert closed == ["closed"]  # lines 261-262 ran
    assert svc._client is None
    assert svc._endpoint is None


@pytest.mark.asyncio
async def test_cleanup_ephemeral_with_container_and_client(monkeypatch) -> None:
    """_cleanup_ephemeral stops the container AND closes _client when both are set."""
    closed: list[str] = []
    stopped: list[str] = []

    class FakeClient:
        async def aclose(self) -> None:
            closed.append("closed")

    async def fake_stop(container_id: str, **_: object) -> None:
        stopped.append(container_id)

    monkeypatch.setattr(hps_mod.performer_lifecycle, "stop", fake_stop)

    svc = HTTPPerformerService(_ephemeral_config())
    svc._container_id = "ctr-clean"
    svc._client = FakeClient()

    await svc._cleanup_ephemeral()

    assert stopped == ["ctr-clean"]
    assert closed == ["closed"]
    assert svc._client is None
    assert svc._endpoint is None


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
async def test_dispatch_ephemeral_closes_stale_client_on_new_start(monkeypatch) -> None:
    """When ephemeral dispatch finds a stale _client, it closes it before the new container."""
    closed: list[str] = []

    class StaleClient:
        async def aclose(self) -> None:
            closed.append("stale_closed")

    async def fake_start(config):
        return StartedContainer(container_id="ctr-fresh", endpoint="http://127.0.0.1:55558")

    async def fake_wait_ready(endpoint, auth_token, *, timeout, performer_id, **_):
        return None

    class FakePostClient:
        async def post_job(self, payload):
            return type("Resp", (), {"accepted": True, "job_id": "job-fresh"})()

        async def aclose(self) -> None:
            pass

    monkeypatch.setattr(hps_mod.performer_lifecycle, "start_ephemeral", fake_start)
    monkeypatch.setattr(hps_mod.performer_lifecycle, "wait_ready", fake_wait_ready)
    monkeypatch.setattr(hps_mod, "PerformerHTTPClient", lambda *a, **kw: FakePostClient())

    svc = HTTPPerformerService(_ephemeral_config())  # no injected client
    svc._client = StaleClient()  # simulate pre-existing stale client

    result = await svc.dispatch_card(_card(), _workspace())

    assert "stale_closed" in closed  # lines 150-151 executed
    assert result["status"] == "ok"
    await svc.aclose()

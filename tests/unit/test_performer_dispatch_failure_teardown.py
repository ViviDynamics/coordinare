"""A dispatch that fails after its ephemeral container started stops that container.

A local daemon whose checkout lost files to a /tmp cleanup retried its environment bootstrap every
cycle; each attempt started a performer, failed with FileNotFoundError while building the job
payload, and left the container running (303 in a night). Expected refusals already tore down
in-line; any other failure now does too, exactly once.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from coordinare.models.performer_endpoint import PerformerEndpointConfig
from coordinare.services import http_performer_service as hps
from coordinare.services.http_performer_service import HTTPPerformerService
from coordinare.services.performer_runtime import StartedPerformer
from coordinare.workspace import WorkspaceInfo

CARD = {"id": "card-1", "role": "implementer", "backend": "claude_code", "persona_instructions": "x"}


class Runtime:
    def __init__(self) -> None:
        self.started = 0
        self.stopped: list[str] = []

    async def start_ephemeral(self, config: Any, **kwargs: Any) -> StartedPerformer:
        self.started += 1
        return StartedPerformer(handle=f"cid-{self.started}", endpoint="http://127.0.0.1:9")

    async def stop(self, handle: str, **kwargs: Any) -> None:
        self.stopped.append(handle)

    async def tail_logs(self, handle: str, *, lines: int = 200) -> list[str]:
        return []


class Client:
    def __init__(self, post: Any = None) -> None:
        self.post = post

    async def post_job(self, payload: Any) -> Any:
        if self.post is not None:
            return await self.post(payload)
        return SimpleNamespace(accepted=True, job_id="job-1")

    async def aclose(self) -> None:
        return None


def service(runtime: Runtime, client: Client) -> HTTPPerformerService:
    config = PerformerEndpointConfig(id="perf-1", mode="ephemeral", image="img", roles=["implementer"])
    return HTTPPerformerService(config, client=cast("Any", client), runtime=cast("Any", runtime))


def workspace() -> WorkspaceInfo:
    return WorkspaceInfo(path=None, repo_url="https://github.com/x/y", branch="main", github_token="t")


@pytest.fixture(autouse=True)
def ready(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_wait_ready(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(hps.performer_lifecycle, "wait_ready", fake_wait_ready)


async def test_a_missing_file_while_the_payload_is_built_stops_the_container(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = Runtime()
    svc = service(runtime, Client())

    def missing(*args: Any, **kwargs: Any) -> Any:
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(svc, "_build_job_payload", missing)
    for _ in range(3):
        with pytest.raises(FileNotFoundError):
            await svc.dispatch_card(CARD, workspace())
    assert runtime.stopped == ["cid-1", "cid-2", "cid-3"]
    assert svc._active_jobs == {}


async def test_a_readiness_probe_fault_stops_the_container(monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken(*args: Any, **kwargs: Any) -> None:
        raise OSError("connection reset")

    monkeypatch.setattr(hps.performer_lifecycle, "wait_ready", broken)
    runtime = Runtime()
    with pytest.raises(OSError, match="connection reset"):
        await service(runtime, Client()).dispatch_card(CARD, workspace())
    assert runtime.stopped == ["cid-1"]


async def test_an_unexpected_post_failure_stops_the_container_once() -> None:
    async def bug(payload: Any) -> Any:
        raise RuntimeError("unexpected")

    runtime = Runtime()
    with pytest.raises(RuntimeError, match="unexpected"):
        await service(runtime, Client(bug)).dispatch_card(CARD, workspace())
    assert runtime.stopped == ["cid-1"]


async def test_a_failure_after_acceptance_stops_the_accepted_container(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = Runtime()
    svc = service(runtime, Client())

    def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("bookkeeping failed")

    monkeypatch.setattr(svc, "_remember_transcript_secrets", broken)
    with pytest.raises(RuntimeError, match="bookkeeping failed"):
        await svc.dispatch_card(CARD, workspace())
    assert runtime.stopped == ["cid-1"]
    assert svc._active_jobs == {}


async def test_a_refused_dispatch_is_still_stopped_once_and_returns_its_error() -> None:
    async def busy(payload: Any) -> Any:
        return SimpleNamespace(accepted=False, reason="busy", detail=None)

    runtime = Runtime()
    result = await service(runtime, Client(busy)).dispatch_card(CARD, workspace())
    assert result["status"] == "error"
    assert runtime.stopped == ["cid-1"]


async def test_a_cancelled_dispatch_stops_the_started_container() -> None:
    import asyncio

    posted = asyncio.Event()

    async def hang(payload: Any) -> Any:
        posted.set()
        await asyncio.sleep(60)

    runtime = Runtime()
    task = asyncio.ensure_future(service(runtime, Client(hang)).dispatch_card(CARD, workspace()))
    await asyncio.wait_for(posted.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert runtime.stopped == ["cid-1"]


async def test_a_readiness_timeout_is_refused_once(monkeypatch: pytest.MonkeyPatch) -> None:
    async def slow(*args: Any, **kwargs: Any) -> None:
        raise hps.performer_lifecycle.ReadinessTimeoutError("not ready")

    monkeypatch.setattr(hps.performer_lifecycle, "wait_ready", slow)
    runtime = Runtime()
    result = await service(runtime, Client()).dispatch_card(CARD, workspace())
    assert result["status"] == "error"
    assert runtime.stopped == ["cid-1"]


async def test_a_release_during_a_failing_dispatch_does_not_stop_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    class SlowStop(Runtime):
        async def stop(self, handle: str, **kwargs: Any) -> None:
            self.stopped.append(handle)
            await asyncio.sleep(0.2)  # a release still stopping when the dispatch fails

    runtime = SlowStop()
    svc = service(runtime, Client())
    releases: list[asyncio.Task[Any]] = []

    async def release_then_fail(session_id: str, *args: Any, **kwargs: Any) -> None:
        releases.append(asyncio.ensure_future(svc.release_session(session_id)))
        await asyncio.sleep(0.05)
        raise RuntimeError("header write failed")

    monkeypatch.setattr(svc, "_transcribes", lambda _session: True)
    monkeypatch.setattr(svc, "_write_run_header", release_then_fail)
    with pytest.raises(RuntimeError, match="header write failed"):
        await svc.dispatch_card(CARD, workspace())
    await asyncio.gather(*releases)
    assert runtime.stopped == ["cid-1"]


async def test_a_persistent_performer_has_nothing_to_stop() -> None:
    async def bug(payload: Any) -> Any:
        raise RuntimeError("unexpected")

    runtime = Runtime()
    config = PerformerEndpointConfig(id="perf-p", mode="persistent", image="img", roles=["implementer"],
                                     endpoint="http://127.0.0.1:8080")
    svc = HTTPPerformerService(config, client=cast("Any", Client(bug)), runtime=cast("Any", runtime))
    with pytest.raises(RuntimeError):
        await svc.dispatch_card(CARD, workspace())
    assert runtime.stopped == []

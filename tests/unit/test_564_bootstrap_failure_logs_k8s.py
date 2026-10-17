"""344: the bootstrap log fetch reads the performer's log through the service's
runtime, never through the docker binary.

Under the kubernetes transport the daemon container has no docker, so the docker
fallback recorded ``<docker logs failed: ...>`` in ``logs_tail`` and the performer
pod's traceback was lost with the pod. ``PerformerRuntime.tail_logs`` exists for
exactly this: ``docker logs`` against a container id, the ``pods/log`` endpoint
against a Pod name.
"""
from __future__ import annotations

from typing import Any

import pytest

from coordinare.daemon import (
    _bootstrap_failure_logs,
    _snapshot_container_logs,
)

_POD = "coordinare-performer-default-abc123"
_K8S_LINES = [
    "Traceback (most recent call last):",
    "  File 'env_bootstrap.py', line 42, in bootstrap",
    "TypeError: 'NoneType' object is not iterable",
]


class _Runtime:
    """Kubernetes-shaped runtime: ``tail_logs`` reads the pods/log endpoint."""

    def __init__(self, lines: list[str]) -> None:
        self.lines = lines
        self.calls: list[tuple[str, int]] = []

    async def tail_logs(self, handle: str, *, lines: int = 200) -> list[str]:
        self.calls.append((handle, lines))
        return list(self.lines)[-lines:]


class _Service:
    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime
        self.agent_logs: list[str] = []

    def get_agent_logs(self) -> list[str]:
        return list(self.agent_logs)


class _RedactingService(_Service):
    """HTTPPerformerService-shaped: raw tails pass through ``_redact_tail``."""

    def __init__(self, runtime: Any, secret: str) -> None:
        super().__init__(runtime)
        self._secret = secret
        self.redacted_for: list[str] = []

    def _redact_tail(self, lines: list[str], session_id: str) -> list[str]:
        self.redacted_for.append(session_id)
        return [line.replace(self._secret, "<redacted>") for line in lines]


@pytest.fixture(autouse=True)
def docker_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """344: the kubernetes path must not reach for the docker binary at all."""
    calls: list[str] = []

    async def _forbidden(container_id: str, tail: int) -> list[str]:
        calls.append(container_id)
        raise AssertionError("docker invoked on the kubernetes path")

    import coordinare.daemon as daemon_module

    monkeypatch.setattr(daemon_module, "_docker_logs_lines", _forbidden)
    return calls


@pytest.mark.asyncio
async def test_bootstrap_failure_logs_fetches_via_runtime_tail_logs(
    docker_calls: list[str],
) -> None:
    runtime = _Runtime(_K8S_LINES)
    svc = _Service(runtime)

    logs = await _bootstrap_failure_logs(svc, "job-1", _POD, [])

    assert runtime.calls == [(_POD, 100)]
    assert logs == _K8S_LINES[-60:]
    assert docker_calls == []


@pytest.mark.asyncio
async def test_bootstrap_failure_logs_empty_tail_falls_back_to_agent_logs(
    docker_calls: list[str],
) -> None:
    runtime = _Runtime([])
    svc = _Service(runtime)
    svc.agent_logs = ["agent buffer line"]

    logs = await _bootstrap_failure_logs(svc, "job-1", _POD, [])

    assert logs == ["agent buffer line"]
    assert docker_calls == []


@pytest.mark.asyncio
async def test_snapshot_container_logs_fetches_via_runtime_tail_logs(
    docker_calls: list[str],
) -> None:
    runtime = _Runtime(_K8S_LINES)
    svc = _Service(runtime)

    snap = await _snapshot_container_logs(svc, _POD, [], "job-1")

    assert runtime.calls == [(_POD, 500)]
    assert snap == _K8S_LINES[-500:]
    assert docker_calls == []


@pytest.mark.asyncio
async def test_bootstrap_failure_logs_docker_path_without_runtime() -> None:
    """A service with no runtime seam keeps the legacy docker-direct path."""
    import coordinare.daemon as daemon_module

    class _NoRuntimeService:
        pass

    async def _legacy_docker(container_id: str, tail: int) -> list[str]:
        return ["legacy docker line"]

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(daemon_module, "_docker_logs_lines", _legacy_docker)
        logs = await _bootstrap_failure_logs(_NoRuntimeService(), "job-1", "cid-1", [])

    assert logs == ["legacy docker line"]


@pytest.mark.asyncio
async def test_bootstrap_failure_logs_redacts_runtime_tail(
    docker_calls: list[str],
) -> None:
    """344 review: a raw runtime tail must not leak secrets into structured logs."""
    runtime = _Runtime(_SECRET_LINES)
    svc = _RedactingService(runtime, "ghp_aaa111bbb222ccc333")

    logs = await _bootstrap_failure_logs(svc, "job-1", _POD, [])

    assert svc.redacted_for == ["job-1"]
    assert logs == [
        "Traceback (most recent call last):",
        "bootstrapping with token <redacted>",
        "TypeError: 'NoneType' object is not iterable",
    ]
    assert docker_calls == []


@pytest.mark.asyncio
async def test_bootstrap_failure_logs_redaction_failure_drops_lines(
    docker_calls: list[str],
) -> None:
    """If redaction itself fails, the raw lines are dropped, not logged."""
    runtime = _Runtime(_SECRET_LINES)

    class _BrokenRedactor(_Service):
        def _redact_tail(self, lines: list[str], session_id: str) -> list[str]:
            raise RuntimeError("redactor down")

    svc = _BrokenRedactor(runtime)

    logs = await _bootstrap_failure_logs(svc, "job-1", _POD, [])

    assert logs == []
    assert docker_calls == []


@pytest.mark.asyncio
async def test_snapshot_container_logs_redacts_runtime_tail(
    docker_calls: list[str],
) -> None:
    runtime = _Runtime(_SECRET_LINES)
    svc = _RedactingService(runtime, "ghp_aaa111bbb222ccc333")

    snap = await _snapshot_container_logs(svc, _POD, [], "job-1")

    assert svc.redacted_for == ["job-1"]
    assert snap == [
        "Traceback (most recent call last):",
        "bootstrapping with token <redacted>",
        "TypeError: 'NoneType' object is not iterable",
    ]
    assert docker_calls == []


_SECRET_LINES = [
    "Traceback (most recent call last):",
    "bootstrapping with token ghp_aaa111bbb222ccc333",
    "TypeError: 'NoneType' object is not iterable",
]

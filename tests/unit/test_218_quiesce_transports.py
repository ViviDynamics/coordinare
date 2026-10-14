"""Spec 218 (review round 3): "the bootstrap container is gone" is proven by its own transport, or it is not.

The completion verify reads the cache. It may run only when the bootstrap workload can no longer write.
Each runtime now answers ``confirm_stopped(handle)``, and only a proof of absence returns True:

* Docker: ``docker stop`` succeeded, or ``docker container inspect`` says "No such container" (``--rm``).
  Any other inspect failure, or a container still running, is False.
* Kubernetes: the Pod is deleted and a read of it returns 404. A Pod that still reads is False.
* Anything else (an unknown transport, or no handle) is False. Nothing is confirmed, so it is infra.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from kubernetes.client.exceptions import ApiException

from coordinare.daemon import CoordinareDaemon
from coordinare.services.docker_runtime import DockerRuntime
from coordinare.services.kubernetes_runtime import KubernetesRuntime
from tests.unit.env_mount_stub import mount_config


class _Proc:
    def __init__(self, rc: int, out: bytes = b"", err: bytes = b"") -> None:
        self.returncode = rc
        self._out = out
        self._err = err

    async def wait(self) -> int:
        return self.returncode

    async def communicate(self, **_kwargs: Any) -> tuple[bytes, bytes]:
        return self._out, self._err


def _docker(monkeypatch: pytest.MonkeyPatch, *, stop_rc: int, inspect: _Proc | None = None) -> list[tuple[str, ...]]:
    """Fake docker: ``stop`` answers ``stop_rc``; ``container inspect`` answers ``inspect``."""
    calls: list[tuple[str, ...]] = []

    async def fake(*argv: Any, **_kwargs: Any) -> _Proc:
        calls.append(tuple(str(a) for a in argv))
        if "stop" in argv:
            return _Proc(stop_rc)
        assert inspect is not None, "inspect was not expected"
        return inspect

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)
    return calls


# ---------------------------------------------------------------------------
# DockerRuntime.confirm_stopped
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_docker_stop_that_succeeds_is_confirmed(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _docker(monkeypatch, stop_rc=0)

    assert await DockerRuntime().confirm_stopped("ctr-218") is True
    assert calls[0][:2] == ("docker", "stop") and calls[0][-1] == "ctr-218"


@pytest.mark.asyncio
async def test_docker_real_no_such_container_is_confirmed_removed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The exact text docker prints for a container ``--rm`` already removed."""
    _docker(
        monkeypatch,
        stop_rc=1,
        inspect=_Proc(1, b"", b"Error response from daemon: No such container: ctr-218\n"),
    )

    assert await DockerRuntime().confirm_stopped("ctr-218") is True


@pytest.mark.asyncio
async def test_docker_inspect_error_other_than_missing_is_not_confirmed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A daemon that cannot answer is not proof the container is gone."""
    _docker(
        monkeypatch,
        stop_rc=1,
        inspect=_Proc(1, b"", b"Cannot connect to the Docker daemon at unix:///var/run/docker.sock\n"),
    )

    assert await DockerRuntime().confirm_stopped("ctr-218") is False


@pytest.mark.asyncio
async def test_docker_container_still_running_is_not_confirmed(monkeypatch: pytest.MonkeyPatch) -> None:
    _docker(monkeypatch, stop_rc=1, inspect=_Proc(0, b"true\n"))

    assert await DockerRuntime().confirm_stopped("ctr-218") is False


# ---------------------------------------------------------------------------
# KubernetesRuntime.confirm_stopped (the handle is a Pod name)
# ---------------------------------------------------------------------------
def _k8s(read: Any) -> KubernetesRuntime:
    runtime = KubernetesRuntime.__new__(KubernetesRuntime)
    runtime._namespace = "coordinare"
    runtime._api = MagicMock()
    runtime._api.delete_namespaced_pod = MagicMock(return_value=None)
    runtime._api.read_namespaced_pod = MagicMock(side_effect=read)
    return runtime


@pytest.mark.asyncio
async def test_kubernetes_pod_gone_after_delete_is_confirmed() -> None:
    runtime = _k8s(read=ApiException(status=404))

    assert await runtime.confirm_stopped("bootstrap-pod-218") is True
    runtime._api.delete_namespaced_pod.assert_called()
    assert runtime._api.delete_namespaced_pod.call_args.kwargs["name"] == "bootstrap-pod-218"


@pytest.mark.asyncio
async def test_kubernetes_pod_that_still_reads_is_not_confirmed() -> None:
    runtime = _k8s(read=lambda **_kw: SimpleNamespace(metadata=SimpleNamespace(name="bootstrap-pod-218")))

    assert await runtime.confirm_stopped("bootstrap-pod-218", timeout_s=0) is False


@pytest.mark.asyncio
async def test_kubernetes_read_error_is_not_confirmed() -> None:
    """A 500 or a 401 says the question failed, not that the Pod went away."""
    runtime = _k8s(read=ApiException(status=500))

    assert await runtime.confirm_stopped("bootstrap-pod-218", timeout_s=0) is False


# ---------------------------------------------------------------------------
# The daemon's quiesce: fail closed on every case it cannot prove
# ---------------------------------------------------------------------------
def _daemon() -> CoordinareDaemon:
    daemon = CoordinareDaemon(AsyncMock(), max_cycles=1, sleep_func=AsyncMock())
    daemon._state["coordinare_config"] = mount_config()
    return daemon


@pytest.mark.asyncio
async def test_kubernetes_handle_is_quiet_only_when_the_pod_is_gone() -> None:
    """The brief's case: a Pod name is not a container id, so docker cannot answer. Only the
    runtime's own proof counts."""
    runtime = MagicMock()
    runtime.confirm_stopped = AsyncMock(return_value=False)
    svc = SimpleNamespace(_runtime=runtime)

    assert await _daemon()._quiesce_bootstrap_container(svc, "bootstrap-pod-218", "sym") is False
    runtime.confirm_stopped.assert_awaited_once_with("bootstrap-pod-218")


@pytest.mark.asyncio
async def test_a_real_kubernetes_runtime_with_a_gone_pod_is_quiet() -> None:
    """The daemon takes the runtime's own answer: a Pod that reads 404 is quiet."""
    svc = SimpleNamespace(_runtime=_k8s(read=ApiException(status=404)))

    assert await _daemon()._quiesce_bootstrap_container(svc, "bootstrap-pod-218", "sym") is True


@pytest.mark.asyncio
async def test_a_none_handle_fails_closed() -> None:
    runtime = MagicMock()
    runtime.confirm_stopped = AsyncMock(return_value=True)
    svc = SimpleNamespace(_runtime=runtime)

    assert await _daemon()._quiesce_bootstrap_container(svc, None, "sym") is False
    runtime.confirm_stopped.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_unknown_transport_fails_closed() -> None:
    """A runtime with no ``confirm_stopped`` cannot prove anything."""
    svc = SimpleNamespace(_runtime=object())

    assert await _daemon()._quiesce_bootstrap_container(svc, "handle-218", "sym") is False


@pytest.mark.asyncio
async def test_an_inspect_error_inside_the_runtime_fails_closed() -> None:
    """An exception from the runtime's own check is a failure to prove, not a quiet container."""
    runtime = MagicMock()
    runtime.confirm_stopped = AsyncMock(side_effect=RuntimeError("docker daemon went away"))
    svc = SimpleNamespace(_runtime=runtime)

    assert await _daemon()._quiesce_bootstrap_container(svc, "ctr-218", "sym") is False

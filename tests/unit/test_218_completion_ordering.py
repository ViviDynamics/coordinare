"""Spec 218 (review round 2): the completion verify runs only after the bootstrap container is gone.

The bootstrap performer reports a terminal status while its container may still be running. The
clean verify reads the cache read-only; if a writer were still live, the verdict would race it.
So the success path first stops the container (or confirms it was already removed) and only then
verifies. A container still running fails closed without consuming a bootstrap attempt.

Also: a verify that could not run because docker is unavailable is an infrastructure failure. It is
recorded for the operator and does not burn the 088 breaker.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from coordinare.daemon import CoordinareDaemon
from coordinare.models.env_cache import EnvCacheState
from coordinare.services.docker_runtime import DockerRuntime
from coordinare.services.env_cache import EnvCacheService
from tests.unit.env_mount_stub import mount_config

SVC = SimpleNamespace(devenv_root="/devenv", _config=SimpleNamespace(image="img"), _runtime=DockerRuntime())
CONTAINER = "ctr-218abcdef012"


class _Docker:
    """Answers ``docker stop`` and ``docker container inspect`` from a table, and records order."""

    def __init__(self, *, stop_rc: int, inspect_rc: int, inspect_out: bytes, inspect_err: bytes = b"") -> None:
        self.stop_rc = stop_rc
        self.inspect_rc = inspect_rc
        self.inspect_out = inspect_out
        self.inspect_err = inspect_err
        self.calls: list[tuple[str, ...]] = []

    async def create_subprocess_exec(self, *argv: Any, **_kwargs: Any) -> _Proc:
        self.calls.append(tuple(str(a) for a in argv))
        if "stop" in argv:
            return _Proc(self.stop_rc, b"")
        if "inspect" in argv:
            return _Proc(self.inspect_rc, self.inspect_out, self.inspect_err)
        return _Proc(0, b"")


class _Proc:
    def __init__(self, rc: int, out: bytes, err: bytes = b"") -> None:
        self.returncode = rc
        self._out = out
        self._err = err

    async def communicate(self, **_kwargs: Any) -> tuple[bytes, bytes]:
        return self._out, self._err

    async def wait(self) -> int:
        return self.returncode


def _daemon(monkeypatch: pytest.MonkeyPatch, docker: _Docker, verify: AsyncMock) -> CoordinareDaemon:
    daemon = CoordinareDaemon(AsyncMock(), max_cycles=1, sleep_func=AsyncMock())
    daemon._state["coordinare_config"] = mount_config()
    monkeypatch.setattr(
        "asyncio.create_subprocess_exec", docker.create_subprocess_exec,
    )
    monkeypatch.setattr(daemon, "_verify_env_cache_clean", verify)
    return daemon


async def _complete(daemon: CoordinareDaemon, ec_svc: Any) -> None:
    await daemon._handle_bootstrap_terminal_status(
        SVC, "job-1", "sym", ec_svc, CONTAINER, {"status": "env_bootstrap_complete"}, [],
    )


@pytest.mark.asyncio
async def test_completion_does_not_verify_while_the_container_is_still_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docker = _Docker(stop_rc=1, inspect_rc=0, inspect_out=b"true\n")
    verify = AsyncMock(return_value=(True, None))
    daemon = _daemon(monkeypatch, docker, verify)
    ec_svc = MagicMock()

    await _complete(daemon, ec_svc)

    verify.assert_not_awaited()
    ec_svc.on_bootstrap_complete.assert_called_once()
    args, kwargs = ec_svc.on_bootstrap_complete.call_args
    assert args[1] is False
    assert kwargs.get("infra_failure") is True
    assert "stopped" in kwargs["error"]


@pytest.mark.asyncio
async def test_the_container_is_stopped_before_the_verify_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docker = _Docker(stop_rc=0, inspect_rc=0, inspect_out=b"false\n")
    order: list[str] = []
    verify = AsyncMock(side_effect=lambda *_a, **_k: order.append("verify") or (True, None))
    daemon = _daemon(monkeypatch, docker, verify)
    real = docker.create_subprocess_exec

    async def _recording(*argv: Any, **kwargs: Any) -> Any:
        if "stop" in argv:
            order.append("stop")
        return await real(*argv, **kwargs)

    monkeypatch.setattr("asyncio.create_subprocess_exec", _recording)

    await _complete(daemon, MagicMock())

    assert order == ["stop", "verify"]
    stop_argv = next(c for c in docker.calls if "stop" in c)
    assert CONTAINER in stop_argv


@pytest.mark.asyncio
async def test_a_container_already_removed_counts_as_quiet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--rm`` removes an exited container, so stop and inspect both fail. No writer remains."""
    docker = _Docker(
        stop_rc=1, inspect_rc=1, inspect_out=b"",
        inspect_err=b"Error response from daemon: No such container: ctr-218abcdef012\n",
    )
    verify = AsyncMock(return_value=(True, None))
    daemon = _daemon(monkeypatch, docker, verify)
    ec_svc = MagicMock()

    await _complete(daemon, ec_svc)

    verify.assert_awaited_once()
    assert ec_svc.on_bootstrap_complete.call_args.args[1] is True


@pytest.mark.asyncio
async def test_docker_unavailable_at_verify_is_an_infrastructure_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docker = _Docker(stop_rc=0, inspect_rc=0, inspect_out=b"false\n")
    verify = AsyncMock(return_value=(None, "clean verify could not run: docker gone"))
    daemon = _daemon(monkeypatch, docker, verify)
    ec_svc = MagicMock()

    await _complete(daemon, ec_svc)

    args, kwargs = ec_svc.on_bootstrap_complete.call_args
    assert args[1] is False
    assert kwargs.get("infra_failure") is True
    assert "could not run" in kwargs["error"]


def _ec_state(cache: Any) -> dict[str, Any]:
    state = EnvCacheState(symphony_name="sym", sanitised_name="sym-abc", cache_dir=cache)
    return {"sym": state}


def test_infrastructure_failures_do_not_consume_the_breaker(tmp_path: Any) -> None:
    """Three docker outages leave the attempt budget unspent, so the bootstrap is retried later."""
    svc = EnvCacheService(mount_config(env_bootstrap_max_attempts=3))
    cache = tmp_path / "sym-abc"
    cache.mkdir()
    states = _ec_state(cache)
    ctx = {"env_cache": states}

    for _ in range(3):
        svc.on_bootstrap_complete("sym", False, ctx, error="docker down", infra_failure=True)

    state = states["sym"]
    assert state.bootstrap_attempts == 0
    assert state.bootstrap_exhausted is False
    assert state.last_bootstrap_succeeded is False
    assert "docker down" in (state.last_bootstrap_error or "")


def test_a_real_failure_still_consumes_the_breaker(tmp_path: Any) -> None:
    svc = EnvCacheService(mount_config(env_bootstrap_max_attempts=3))
    cache = tmp_path / "sym-abc"
    cache.mkdir()
    states = _ec_state(cache)
    ctx = {"env_cache": states}

    svc.on_bootstrap_complete("sym", False, ctx, error="verify failed")

    assert states["sym"].bootstrap_attempts == 1

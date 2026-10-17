"""Spec 218 (round 4): cancellation cleans up, a new spec re-arms the infrastructure budget, a docker
daemon outage is infrastructure, and the docker calls end their options before the handle.

* A cancelled daemon task still kills the verify client and removes its container. The cleanup is shielded
  from a second cancel.
* ``docker rm -f`` on a container that is already gone is confirmed removal.
* A new spec SHA re-arms the infrastructure budget at any count, not only when the verdict budget was spent.
* A docker daemon that does not answer is infrastructure (None), not a verdict, because a cheap version probe
  runs before the verify. A toolchain that exits 1 or 125 is still a verdict, or infrastructure for 125.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from coordinare.models.env_cache import EnvCacheState
from coordinare.services import env_cache
from coordinare.services.docker_runtime import DockerRuntime
from coordinare.services.env_cache import rearm_budgets_on_spec_change, verify_env_cache_clean
from tests.unit.env_manifest_fixture import reviewed_cache, verification_output

SVC = SimpleNamespace(devenv_root="/devenv", _config=SimpleNamespace(image="img@sha256:" + "a" * 64))
REAL_EXEC = asyncio.create_subprocess_exec


@pytest.fixture(autouse=True)
def _accepted_verification_sources(monkeypatch):
    """Probe/cancellation tests supply accepted source independently of GitHub I/O."""
    monkeypatch.setattr("coordinare.services.environment_verification.verification_sources",
        AsyncMock(return_value={"tools.plan": "baseline"}))
    monkeypatch.setattr("coordinare.services.environment_verification.source_setup", lambda files: "")


class _Hung:
    """A verify run that never finishes. ``kill`` is recorded."""

    def __init__(self) -> None:
        self.killed = 0
        self.returncode: int | None = None

    async def communicate(self, **_kwargs: Any) -> tuple[bytes, None]:
        await asyncio.sleep(30)
        return b"", None

    def kill(self) -> None:
        self.killed += 1


class _Fake:
    """Records every docker argv. ``answers`` maps a subcommand to (returncode, stderr)."""

    def __init__(self, *, run_rc: int = 0, probe_rc: int = 0, rm: tuple[int, bytes] = (0, b""), hang: bool = False) -> None:
        self.run_rc = run_rc
        self.probe_rc = probe_rc
        self.rm = rm
        self.hang = hang
        self.argv: list[tuple[str, ...]] = []
        self.hung: list[_Hung] = []

    async def create_subprocess_exec(self, *argv: Any, **_kwargs: Any) -> Any:
        call = tuple(str(a) for a in argv)
        self.argv.append(call)
        if call[1:2] == ("version",):
            return _Done(self.probe_rc)
        if call[1:3] == ("rm", "-f"):
            return _Done(self.rm[0], self.rm[1])
        if self.hang:
            proc = _Hung()
            self.hung.append(proc)
            return proc
        return _Done(self.run_rc, output=verification_output())


class _Done:
    def __init__(self, rc: int, err: bytes = b"", *, output: bytes = b"") -> None:
        self.returncode = rc
        self._err = err
        self._output = output

    async def wait(self) -> int:
        return self.returncode

    async def communicate(self, **_kwargs: Any) -> tuple[bytes, bytes]:
        return self._output, self._err


def _state(tmp_path: Path, verify: str = "#!/bin/sh\nexit 0\n") -> dict[str, Any]:
    cache = tmp_path / "sym-abc"
    cache.mkdir()
    (cache / "activate.sh").write_text("ACT=1\n")
    state = EnvCacheState(
        symphony_name="sym", sanitised_name="sym-abc", cache_dir=cache,
        rendered_verify_sh=verify, rendered_activate_sh="ACT=1\n",
    )
    reviewed_cache(state)
    return {"env_cache": {"sym": state}}


def _run_argv(fake: _Fake) -> tuple[str, ...]:
    return next(c for c in fake.argv if c[1:2] == ("run",))


# ---------------------------------------------------------------------------
# 1. cancellation
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_cancelled_verify_kills_and_removes_its_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancelled daemon task used to skip the kill and the removal, leaving the container running."""
    fake = _Fake(hang=True)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake.create_subprocess_exec)

    task = asyncio.ensure_future(verify_env_cache_clean(_state(tmp_path), "sym", SVC, performer_mounts=()))
    for _ in range(200):
        if fake.hung:
            break
        await asyncio.sleep(0.01)
    assert fake.hung, "the verify never reached docker run"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    name = _run_argv(fake)[_run_argv(fake).index("--name") + 1]
    assert fake.hung[0].killed == 1
    removals = [c for c in fake.argv if c[1:3] == ("rm", "-f") and c[-1] == name]
    assert removals, "the verify container was not removed by name after the cancel"


# ---------------------------------------------------------------------------
# 1b. removal of a container that is already gone is confirmed
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_removing_an_already_removed_verify_container_is_confirmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _Fake(rm=(1, b"Error response from daemon: No such container: coordinare-verify-x\n"))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake.create_subprocess_exec)

    assert await env_cache._remove_verify_container("coordinare-verify-x") is True


@pytest.mark.asyncio
async def test_a_failed_removal_is_not_confirmed(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _Fake(rm=(1, b"Cannot connect to the Docker daemon\n"))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake.create_subprocess_exec)

    assert await env_cache._remove_verify_container("coordinare-verify-x") is False


@pytest.mark.asyncio
async def test_docker_argv_ends_its_options_before_the_handle(monkeypatch: pytest.MonkeyPatch) -> None:
    """The handle is an argument, never an option: docker stop and inspect and rm all take `--`."""

    fake = _Fake()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake.create_subprocess_exec)

    await DockerRuntime().confirm_stopped("-dangerous-handle")

    stop = next(c for c in fake.argv if c[1:2] == ("stop",))
    assert stop[stop.index("-dangerous-handle") - 1] == "--"


# ---------------------------------------------------------------------------
# 2. a new spec SHA re-arms the infrastructure budget below the cap
# ---------------------------------------------------------------------------
def test_a_new_spec_sha_rearms_the_infrastructure_budget_below_the_cap() -> None:
    state = EnvCacheState(
        symphony_name="sym", sanitised_name="sym-abc", cache_dir=Path("/tmp/x"),
        readme_sha="old-sha", infra_failures=3,
    )

    rearm_budgets_on_spec_change(state, "new-sha", "sym")

    assert state.infra_failures == 0


def test_the_same_spec_sha_keeps_the_infrastructure_budget() -> None:
    state = EnvCacheState(
        symphony_name="sym", sanitised_name="sym-abc", cache_dir=Path("/tmp/x"),
        readme_sha="same", infra_failures=3,
    )

    rearm_budgets_on_spec_change(state, "same", "sym")

    assert state.infra_failures == 3


def test_a_first_sha_does_not_rearm_anything() -> None:
    state = EnvCacheState(
        symphony_name="sym", sanitised_name="sym-abc", cache_dir=Path("/tmp/x"),
        readme_sha=None, infra_failures=3,
    )

    rearm_budgets_on_spec_change(state, "new", "sym")

    assert state.infra_failures == 3


# ---------------------------------------------------------------------------
# 3. a docker daemon outage is infrastructure, not a verdict
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_an_unreachable_docker_daemon_is_infrastructure_and_the_verify_does_not_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CLI exits 1 with 'failed to connect' when the daemon is down, which looked like a failed verdict."""
    fake = _Fake(probe_rc=1)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake.create_subprocess_exec)

    passed, detail = await verify_env_cache_clean(_state(tmp_path), "sym", SVC, performer_mounts=())

    assert passed is None
    assert "daemon" in detail
    assert not any(c[1:2] == ("run",) for c in fake.argv), "the verify must not run without a daemon"


@pytest.mark.asyncio
async def test_the_probe_runs_before_the_verify(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _Fake()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake.create_subprocess_exec)

    await verify_env_cache_clean(_state(tmp_path), "sym", SVC, performer_mounts=())

    assert fake.argv[0][1:2] == ("version",)
    assert fake.argv[1][1:2] == ("run",)


@pytest.mark.asyncio
async def test_a_toolchain_that_exits_one_is_still_a_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A planted or missing toolchain makes the verify exit 1. That is a verdict, not infrastructure."""
    fake = _Fake(run_rc=1)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake.create_subprocess_exec)

    passed, _detail = await verify_env_cache_clean(_state(tmp_path), "sym", SVC, performer_mounts=())

    assert passed is False

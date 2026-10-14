"""Spec 218 (review round 3): how the verify fails, and how many times an infrastructure failure may repeat.

* A verify TIMEOUT is a real failure. It spends a bootstrap attempt, because a performer that can hang the
  verify must not get unlimited retries. The verify container is removed by its name, so a hung run does
  not keep running.
* ``docker run`` exit 125 (the daemon, or the image, could not start the container) is an infrastructure
  failure, not a verdict on the toolchain.
* Infrastructure failures have their own budget, persisted next to the 088 fields. It escalates the cooldown,
  and at the cap the cache becomes an operator-visible exhausted state with the infrastructure reason. A
  success resets it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from coordinare.models.env_cache import EnvCacheState
from coordinare.services import env_cache
from coordinare.services.env_cache import EnvCacheService, verify_env_cache_clean
from tests.unit.env_mount_stub import mount_config

SVC = SimpleNamespace(devenv_root="/devenv", _config=SimpleNamespace(image="img"))


class _SlowProc:
    """A ``docker run`` whose verify never finishes. ``kill`` is recorded."""

    def __init__(self, owner: _Fake) -> None:
        self._owner = owner
        self.returncode: int | None = None

    async def communicate(self, **_kwargs: Any) -> tuple[bytes, None]:
        await asyncio.sleep(30)
        return b"", None

    def kill(self) -> None:
        self._owner.killed += 1


class _Fake:
    def __init__(self, *, rc: int = 0, hang: bool = False) -> None:
        self.rc = rc
        self.hang = hang
        self.argv: list[tuple[str, ...]] = []
        self.killed = 0

    async def create_subprocess_exec(self, *argv: Any, **_kwargs: Any) -> Any:
        self.argv.append(tuple(str(a) for a in argv))
        if self.hang and "run" in argv:
            return _SlowProc(self)
        return _Done(self.rc)


class _Done:
    def __init__(self, rc: int) -> None:
        self.returncode = rc

    async def wait(self) -> int:
        return self.returncode

    async def communicate(self, **_kwargs: Any) -> tuple[bytes, None]:
        return b"", None


def _state(tmp_path: Path) -> dict[str, Any]:
    cache = tmp_path / "sym-abc"
    cache.mkdir()
    (cache / "activate.sh").write_text("ACT=1\n")
    state = EnvCacheState(
        symphony_name="sym", sanitised_name="sym-abc", cache_dir=cache,
        rendered_verify_sh="#!/bin/sh\nexit 0\n", rendered_activate_sh="ACT=1\n",
    )
    return {"env_cache": {"sym": state}}


@pytest.mark.asyncio
async def test_a_hung_verify_is_a_real_failure_and_its_container_is_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(env_cache, "_VERIFY_TIMEOUT_S", 0.05)
    fake = _Fake(hang=True)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake.create_subprocess_exec)

    passed, detail = await verify_env_cache_clean(_state(tmp_path), "sym", SVC, performer_mounts=())

    assert passed is False, "a timeout must count as a failed verify, not as infrastructure (None)"
    assert "timed out" in detail
    run = next(c for c in fake.argv if "run" in c)
    name = run[run.index("--name") + 1]
    assert name.startswith("coordinare-verify-sym-abc-")
    removals = [c for c in fake.argv if c[:3] == ("docker", "rm", "-f")]
    assert removals and removals[0][-1] == name
    assert fake.killed == 1


@pytest.mark.asyncio
async def test_docker_could_not_start_the_container_is_infrastructure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exit 125 is the docker daemon or the image failing, not the toolchain failing."""
    fake = _Fake(rc=125)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake.create_subprocess_exec)

    passed, detail = await verify_env_cache_clean(_state(tmp_path), "sym", SVC, performer_mounts=())

    assert passed is None
    assert "could not start" in detail


@pytest.mark.asyncio
async def test_a_missing_docker_binary_is_infrastructure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def missing(*_a: Any, **_k: Any) -> Any:
        raise FileNotFoundError("docker")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", missing)

    passed, _detail = await verify_env_cache_clean(_state(tmp_path), "sym", SVC, performer_mounts=())

    assert passed is None


def _svc_with_cache(tmp_path: Path, infra_cap: int = 5) -> tuple[EnvCacheService, dict[str, Any]]:
    svc = EnvCacheService(mount_config(env_bootstrap_max_attempts=3))
    cache = tmp_path / "sym-abc"
    cache.mkdir(exist_ok=True)
    states = {"sym": EnvCacheState(symphony_name="sym", sanitised_name="sym-abc", cache_dir=cache)}
    return svc, {"env_cache": states}


def test_infrastructure_failures_are_capped_and_then_operator_visible(tmp_path: Path) -> None:
    svc, ctx = _svc_with_cache(tmp_path)
    state = ctx["env_cache"]["sym"]
    for _ in range(4):
        svc.on_bootstrap_complete("sym", False, ctx, error="docker down", infra_failure=True)
    assert state.infra_failures == 4
    assert state.bootstrap_exhausted is False

    svc.on_bootstrap_complete("sym", False, ctx, error="docker down", infra_failure=True)

    assert state.infra_failures == 5
    assert state.bootstrap_exhausted is True
    assert state.bootstrap_attempts == 0, "infrastructure failures never spend the verdict budget"
    assert "infrastructure" in (state.last_bootstrap_error or "")
    assert "docker down" in (state.last_bootstrap_error or "")


def test_a_success_resets_the_infrastructure_budget(tmp_path: Path) -> None:
    svc, ctx = _svc_with_cache(tmp_path)
    state = ctx["env_cache"]["sym"]
    for _ in range(3):
        svc.on_bootstrap_complete("sym", False, ctx, error="docker down", infra_failure=True)
    (state.cache_dir / "toolchain").mkdir()
    (state.cache_dir / "toolchain" / "bin").mkdir()
    (state.cache_dir / "toolchain" / "bin" / "ruby").write_text("bin")

    svc.on_bootstrap_complete("sym", True, ctx)

    assert state.infra_failures == 0
    assert state.bootstrap_exhausted is False


def test_real_failures_still_spend_the_verdict_budget(tmp_path: Path) -> None:
    svc, ctx = _svc_with_cache(tmp_path)

    svc.on_bootstrap_complete("sym", False, ctx, error="verify failed")

    assert ctx["env_cache"]["sym"].bootstrap_attempts == 1
    assert ctx["env_cache"]["sym"].infra_failures == 0


def test_the_infrastructure_budget_is_persisted_with_the_088_fields(tmp_path: Path) -> None:
    from coordinare.daemon import _persist_env_cache
    from coordinare.state_store import EnvCacheStateSnapshot

    svc, ctx = _svc_with_cache(tmp_path)
    state = ctx["env_cache"]["sym"]
    svc.on_bootstrap_complete("sym", False, ctx, error="docker down", infra_failure=True)

    reloaded = EnvCacheStateSnapshot.model_validate_json(_persist_env_cache({"sym": state})["sym"].model_dump_json())

    assert reloaded.infra_failures == 1
    assert reloaded.last_infra_failure_at is not None

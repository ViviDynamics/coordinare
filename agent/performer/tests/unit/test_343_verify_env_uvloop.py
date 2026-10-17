"""343: the env-bootstrap verify crashed under uvloop with "TypeError: all environment values must be bytes or str".

The performer server runs under uvicorn, whose default event loop is uvloop. uvloop,
unlike the stdlib event loop, rejects non-str subprocess env values (os.PathLike
included) at spawn time — and run_env_cache_verify put the checkout path (stand.path,
a Path) into the env as COORDINARE_REPO_DIR. These tests pin the boundary: every env
the workspace spawn sites hand to asyncio must be all strings.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from performer import workspace
from performer.workspace import run_env_cache_verify, run_service_readiness

PG = [{"name": "postgres", "kind": "postgres"}]
INF_OK = {"inference_succeeded": True, "inference_services": ["postgres"]}


class _FakeProc:
    returncode = 0

    async def communicate(self) -> tuple[bytes, bytes]:
        return b"", b""

    async def wait(self) -> int:
        return 0


async def _noop_kill(_proc: object) -> None:
    return None


def _capture_spawns(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
    envs: list[dict[str, str]] = []

    async def fake_exec(*_args: object, **kwargs: object) -> _FakeProc:
        env = kwargs.get("env")
        if isinstance(env, dict):
            envs.append(env)
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(workspace, "_kill_cache_process_group", _noop_kill)
    return envs


class _BytesPathLike:
    def __init__(self, raw: bytes) -> None:
        self._raw = raw

    def __fspath__(self) -> bytes:
        return self._raw


def test_stringify_env_resolves_a_bytes_pathlike_to_str() -> None:
    coerced = workspace._stringify_env({"REPO": _BytesPathLike(b"\xff\xfe")})
    assert isinstance(coerced["REPO"], str)
    assert os.fsencode(coerced["REPO"]) == b"\xff\xfe"


def test_verify_with_path_cwd_passes_on_a_uvloop(tmp_path: Path) -> None:
    import uvloop

    cache = tmp_path / "devenv"
    cache.mkdir()
    checkout = tmp_path / "repo"
    checkout.mkdir()
    loop = uvloop.new_event_loop()
    try:
        passed, detail = loop.run_until_complete(
            run_env_cache_verify(str(cache), None, cwd=checkout, verify_text="exit 0\n")
        )
    finally:
        loop.close()
    assert passed is True, detail


@pytest.mark.asyncio
async def test_verify_spawn_env_is_all_str(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    envs = _capture_spawns(monkeypatch)
    cache = tmp_path / "devenv"
    cache.mkdir()
    checkout = tmp_path / "repo"
    checkout.mkdir()

    passed, detail = await run_env_cache_verify(
        str(cache), {"RUBY": "1", "EXTRA": None}, cwd=checkout, verify_text="exit 0\n",
    )

    assert passed is True, detail
    assert envs, "the verify spawn never ran"
    assert envs[-1]["COORDINARE_REPO_DIR"] == str(checkout)
    assert all(isinstance(k, str) and isinstance(v, str) for k, v in envs[-1].items())


@pytest.mark.asyncio
async def test_services_start_spawn_env_is_all_str(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    envs = _capture_spawns(monkeypatch)
    cache = tmp_path / "devenv"
    services = cache / "services"
    services.mkdir(parents=True)
    start = services / "services-start.sh"
    start.write_text("#!/usr/bin/env bash\nexit 0\n")
    start.chmod(0o755)
    health = services / "services-health.sh"
    health.write_text("#!/usr/bin/env bash\nexit 0\n")
    health.chmod(0o755)

    ok, failures = await run_service_readiness(str(cache), {"RUBY": "1"}, PG, INF_OK)

    assert ok is True, failures
    assert envs, "the services-start spawn never ran"
    assert all(isinstance(k, str) and isinstance(v, str) for env in envs for k, v in env.items())

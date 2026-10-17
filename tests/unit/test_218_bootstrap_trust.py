"""Spec 218: the bootstrap performer writes the env cache, so nothing it writes decides success.

The daemon's decisions are (1) the clean-context verify, (2) the activate.sh gate that
mounts a cache into consumers and holds dispatch, and (3) the toolchain floor on completion.
Each test plants what the bootstrap performer could write and checks the daemon does not
take it as proof. ``_FakeDocker`` records every docker argv and every stdin. With
``run_locally`` the stdin is run with ``sh -s`` on the host, which stands in for the
container's ``bash -s``, so the daemon's own script is what decides the result.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from coordinare.daemon import CoordinareDaemon, _persist_env_cache
from coordinare.models.env_cache import EnvCacheState
from coordinare.services import env_cache
from coordinare.services.env_cache import (
    EnvCacheService,
    _cache_dir_is_populated,
    get_env_volume_for_symphony,
    verify_env_cache_clean,
)
from coordinare.state_store import EnvCacheStateSnapshot
from tests.unit.env_manifest_fixture import reviewed_cache, reviewed_metadata, verification_output
from tests.unit.env_mount_stub import mount_config


@pytest.fixture(autouse=True)
def _accepted_source_fixture(monkeypatch):
    from unittest.mock import AsyncMock

    monkeypatch.setattr("coordinare.services.environment_verification.verification_sources",
                        AsyncMock(return_value={"tools.plan": "baseline"}))
    # These tests exercise trusted verifier execution, not source archive materialisation.
    monkeypatch.setattr("coordinare.services.environment_verification.source_setup", lambda files: "")


_REAL_EXEC = asyncio.create_subprocess_exec

# What the daemon rendered at bootstrap time (held in daemon memory / the snapshot).
RENDERED_VERIFY = "#!/bin/sh\necho 'FAIL: ruby 3.4.2 not runnable' >&2\nexit 1\n"
RENDERED_ACTIVATE = "# AUTHORITATIVE activate.sh (daemon-rendered)\n"
SVC = SimpleNamespace(devenv_root="/devenv", _config=SimpleNamespace(image="img@sha256:" + "1" * 64))


def _run_argv(fake: Any) -> tuple[str, ...]:
    """The docker run call: the daemon's probe (docker version) is also recorded."""
    return next(c for c in fake.calls if "run" in c)


class _FakeDocker:
    """Stands in for ``asyncio.create_subprocess_exec`` for the docker calls."""

    def __init__(self, *, run_locally: bool, rc: int = 0, manifest=None) -> None:
        self.run_locally = run_locally
        self.rc = rc
        self.manifest = manifest
        self.calls: list[tuple[str, ...]] = []
        self.stdin_seen: list[bytes | None] = []

    async def create_subprocess_exec(self, *argv: Any, **_kwargs: Any) -> _FakeDockerProc:
        self.calls.append(tuple(str(a) for a in argv))
        return _FakeDockerProc(self)


class _FakeDockerProc:
    def __init__(self, owner: _FakeDocker) -> None:
        self._owner = owner
        self.returncode: int | None = None

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = self._owner.rc
        return self.returncode

    async def communicate(self, **kwargs: Any) -> tuple[bytes, None]:
        data = kwargs.get("input")
        if data is not None:
            self._owner.stdin_seen.append(data)
        if not self._owner.run_locally:
            self.returncode = self._owner.rc
            return verification_output(self._owner.manifest) if self._owner.rc == 0 else b"", None
        proc = await _REAL_EXEC(
            "sh", "-s",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate(input=data)
        self.returncode = proc.returncode
        return out, None


def _install_fake(monkeypatch: pytest.MonkeyPatch, fake: _FakeDocker) -> None:
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake.create_subprocess_exec)


def _cache(tmp_path: Path) -> Path:
    cache = tmp_path / "sym-abc"
    cache.mkdir()
    return cache


def _state(
    cache: Path,
    *,
    verify: str | None = RENDERED_VERIFY,
    activate: str | None = RENDERED_ACTIVATE,
) -> EnvCacheState:
    return reviewed_cache(EnvCacheState(
        symphony_name="sym",
        sanitised_name="sym-abc",
        cache_dir=cache,
        rendered_verify_sh=verify,
        rendered_activate_sh=activate,
        cache_dir_ready=True, last_bootstrap_succeeded=True,
        bootstrap_commit_sha="a" * 40, **{key: value for key, value in reviewed_metadata().items() if key != "environment_manifest_digest"},
    ))


def _ctx(state: EnvCacheState) -> dict[str, Any]:
    return {"env_cache": {"sym": state}}


def _seed_activate(cache: Path, text: str = RENDERED_ACTIVATE) -> None:
    (cache / "activate.sh").write_text(text)


def _honest_toolchain(cache: Path) -> None:
    ruby = cache / ".rbenv" / "versions" / "3.4.2" / "bin"
    ruby.mkdir(parents=True)
    (ruby / "ruby").write_text("#!/bin/sh\n")


# ---------------------------------------------------------------------------
# 1. verify.sh: the daemon's script decides, never the copy in the cache
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_rewritten_verify_sh_that_exits_zero_does_not_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = _cache(tmp_path)
    _seed_activate(cache)
    # The bootstrap performer rewrote the cache copy to pass. It must never run.
    (cache / "verify.sh").write_text("#!/bin/sh\nexit 0\n")
    fake = _FakeDocker(run_locally=True)
    _install_fake(monkeypatch, fake)

    passed, detail = await verify_env_cache_clean(
        _ctx(_state(cache)), "sym", SVC, performer_mounts=(),
    )

    assert passed is False
    assert "FAIL" in detail
    # The daemon's own text went to the container on stdin, and the cache copy was never named.
    assert [data for data in fake.stdin_seen if data is not None] == [RENDERED_VERIFY.encode()]
    assert all("verify.sh" not in arg for call in fake.calls for arg in call)
    assert "-i" in _run_argv(fake)
    assert "-s" in _run_argv(fake)


@pytest.mark.asyncio
async def test_clean_verify_pipes_the_daemon_script_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The honest run: the cache is mounted read-only and the stdin is the rendered text."""
    cache = _cache(tmp_path)
    _seed_activate(cache)
    fake = _FakeDocker(run_locally=False, rc=0)
    _install_fake(monkeypatch, fake)

    passed, _detail = await verify_env_cache_clean(
        _ctx(_state(cache)), "sym", SVC, performer_mounts=(),
    )

    assert passed is True
    assert [data for data in fake.stdin_seen if data is not None] == [RENDERED_VERIFY.encode()]
    argv = _run_argv(fake)
    assert f"{cache}:/devenv/sym-abc:ro" in argv
    assert argv[-1] == "-s"


@pytest.mark.asyncio
async def test_verify_without_a_daemon_rendered_script_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A legacy snapshot, or a write the daemon refused, leaves no script on record. The cache
    copy must not stand in for it, so the verify fails rather than passing as degraded."""
    cache = _cache(tmp_path)
    _seed_activate(cache)
    (cache / "verify.sh").write_text("#!/bin/sh\nexit 0\n")
    fake = _FakeDocker(run_locally=False, rc=0)
    _install_fake(monkeypatch, fake)

    passed, _detail = await verify_env_cache_clean(
        _ctx(_state(cache, verify=None)), "sym", SVC, performer_mounts=(),
    )

    assert passed is False
    assert fake.calls == []


@pytest.mark.asyncio
async def test_completion_fails_when_the_clean_verify_cannot_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """218: a verify that could not run is not a pass on the completion path."""
    daemon = CoordinareDaemon(AsyncMock(), max_cycles=1, sleep_func=AsyncMock())
    monkeypatch.setattr(
        daemon, "_verify_env_cache_clean", AsyncMock(return_value=(None, "docker down")),
    )

    ok, err, _infra = await daemon._verify_bootstrap_in_clean_context("sym", SVC, "job-1")

    assert ok is False
    assert err is not None
    assert "docker down" in err


# ---------------------------------------------------------------------------
# 2. activate.sh: the cache copy must equal the daemon's text to mount or gate
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "rewritten",
    ["# attacker\nexit 0\n", "", "#!/bin/sh\n. /tmp/evil\n"],
    ids=["rewritten-to-pass", "emptied", "sourced-attacker-file"],
)
@pytest.mark.asyncio
async def test_rewritten_activate_sh_does_not_make_the_cache_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rewritten: str,
) -> None:
    cache = _cache(tmp_path)
    _seed_activate(cache, rewritten)
    state = _state(cache)
    fake = _FakeDocker(run_locally=False, rc=0)
    _install_fake(monkeypatch, fake)

    assert env_cache._cache_dir_has_activate(
        cache, performer_mounts=(), expected_activate=state.rendered_activate_sh,
    ) is False
    assert get_env_volume_for_symphony(
        "sym", {"sym": state}, is_bootstrap=False, performer_mounts=(),
    ) is None
    passed, _detail = await verify_env_cache_clean(_ctx(state), "sym", SVC, performer_mounts=())
    assert passed is False
    assert fake.calls == []


@pytest.mark.asyncio
async def test_activate_sh_matching_the_rendered_text_is_ready(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    _seed_activate(cache)
    state = _state(cache)

    assert env_cache._cache_dir_has_activate(
        cache, performer_mounts=(), expected_activate=state.rendered_activate_sh,
    ) is True
    result = get_env_volume_for_symphony(
        "sym", {"sym": state}, is_bootstrap=False, performer_mounts=(),
    )
    assert result is not None
    assert result[0].mode == "ro"


@pytest.mark.asyncio
async def test_linked_activate_sh_with_matching_content_is_still_refused(tmp_path: Path) -> None:
    """A link is refused even when the host file it points at has the rendered content."""
    cache = _cache(tmp_path)
    host = tmp_path / "host-activate"
    host.write_text(RENDERED_ACTIVATE)
    os.symlink(host, cache / "activate.sh")

    assert env_cache._cache_dir_has_activate(
        cache, performer_mounts=(), expected_activate=RENDERED_ACTIVATE,
    ) is False


# ---------------------------------------------------------------------------
# 3. populated: a name is not a toolchain
# ---------------------------------------------------------------------------
def _seeds(cache: Path) -> None:
    for name in ("activate.sh", "manifest.json", "verify.sh"):
        (cache / name).write_text("seed\n")


@pytest.mark.parametrize(
    "build",
    [
        lambda cache, tmp: (cache / "dummy").write_text("some bytes\n"),
        lambda cache, tmp: (cache / "dummy").write_text(""),
        lambda cache, tmp: (cache / "toolchain").mkdir(),
        lambda cache, tmp: (cache / "services").mkdir(),
        lambda cache, tmp: os.symlink(tmp, cache / "toolchain"),
    ],
    ids=["regular-file", "empty-file", "empty-dir", "services-only", "link-to-dir"],
)
def test_a_dummy_named_entry_does_not_count_as_populated(
    tmp_path: Path, build: Any,
) -> None:
    cache = _cache(tmp_path)
    _seeds(cache)
    build(cache, tmp_path)
    if (cache / "services").is_dir():
        (cache / "services" / "services.json").write_text("{}")
    assert _cache_dir_is_populated(cache, performer_mounts=()) is False


def test_a_real_toolchain_directory_counts_as_populated(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    _seeds(cache)
    _honest_toolchain(cache)
    assert _cache_dir_is_populated(cache, performer_mounts=()) is True


@pytest.mark.asyncio
async def test_dummy_file_fails_the_bootstrap_completion(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    _seeds(cache)
    (cache / "dummy").write_text("not a toolchain\n")
    state = _state(cache)
    # A nonempty required environment must install material, unlike an accepted empty baseline.
    state.environment_manifest["requirements"] = [{"id": "tool", "description": "Required tool",
        "source": "tools.plan", "checks": ["baseline"], "required": True}]
    state.environment_manifest["empty_reason"] = None
    from coordinare_service_inference.environment.models import EnvironmentManifest

    state.environment_assessment["manifest_digest"] = EnvironmentManifest.model_validate(state.environment_manifest).digest()
    svc = EnvCacheService(MagicMock())

    svc.on_bootstrap_complete("sym", success=True, state=_ctx(state))

    assert state.last_bootstrap_succeeded is False
    assert state.cache_dir_ready is False


# ---------------------------------------------------------------------------
# 4. services-health.sh: the daemon does not consult the performer's health script
# ---------------------------------------------------------------------------
def _rewritten_health(cache: Path, body: str) -> None:
    (cache / "services").mkdir(exist_ok=True)
    (cache / "services" / "services-health.sh").write_text(body)


@pytest.mark.asyncio
async def test_rewritten_health_script_does_not_pass_readiness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A health script rewritten to exit 0 cannot turn a failing toolchain into a pass, and it
    is never named in a docker call or in the script the daemon sends."""
    cache = _cache(tmp_path)
    _seed_activate(cache)
    _rewritten_health(cache, "#!/bin/sh\nexit 0\n")
    fake = _FakeDocker(run_locally=True)
    _install_fake(monkeypatch, fake)

    passed, _detail = await verify_env_cache_clean(
        _ctx(_state(cache)), "sym", SVC, performer_mounts=(),
    )

    assert passed is False
    assert all("services" not in arg for call in fake.calls for arg in call)
    assert b"services" not in fake.stdin_seen[0]


@pytest.mark.asyncio
async def test_daemon_readiness_does_not_consult_services_health(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Residual, documented in the spec: a failing health script does not fail the daemon's
    toolchain verify either. Service liveness is performer-decided (see spec 218)."""
    cache = _cache(tmp_path)
    _seed_activate(cache)
    _rewritten_health(cache, "#!/bin/sh\nexit 1\n")
    fake = _FakeDocker(run_locally=False, rc=0)
    _install_fake(monkeypatch, fake)

    passed, _detail = await verify_env_cache_clean(
        _ctx(_state(cache, verify="#!/bin/sh\nexit 0\n")), "sym", SVC, performer_mounts=(),
    )

    assert passed is True


# ---------------------------------------------------------------------------
# 5. the honest bootstrap, end to end with a fake docker runner
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_honest_bootstrap_passes_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = _cache(tmp_path)
    state = EnvCacheState(
        symphony_name="sym", sanitised_name="sym-abc", cache_dir=cache, bootstrap_commit_sha="a" * 40,
    )
    files = {
        ".ruby-version": "3.4.2\n",
        "Gemfile": 'gem "rails"\n',
        "Gemfile.lock": "BUNDLED WITH\n   2.5.6\n",
    }

    async def fake_get(org: str, repo: str, path: str) -> str | None:
        return files.get(path)

    github = MagicMock()
    github.get_file_content = fake_get
    svc = EnvCacheService(mount_config())
    from coordinare_service_inference.environment.models import (
        CoverageAssessment,
        EnvironmentManifest,
    )

    reviewed_cache(state, files=files)
    manifest = EnvironmentManifest.model_validate(state.environment_manifest)
    assessment = CoverageAssessment.model_validate(state.environment_assessment)
    github.get_repository_paths = AsyncMock(return_value=list(files))
    monkeypatch.setattr("coordinare.services.environment_discovery.discover_and_review",
                        AsyncMock(return_value=(manifest, assessment)))

    # 1. The daemon renders and records its scripts at dispatch.
    await svc._build_manifest_artifacts(
        performer_mounts=(),
        symphony_name="sym",
        repo="sym",
        github_org="org",
        github_service=github,
        readme_contents={"README.md": "app"},
        current_sha="sha1",
        cache_dir=cache,
        cache_mount_path="/devenv/sym-abc",
        llm_chat=None,
        cache_state=state,
    )
    assert state.rendered_verify_sh == (cache / "verify.sh").read_text()
    assert state.rendered_activate_sh == (cache / "activate.sh").read_text()

    # 2. The performer installs the toolchain.
    _honest_toolchain(cache)

    # 3. Completion: the daemon's clean verify passes and the floor holds.
    fake = _FakeDocker(run_locally=False, rc=0, manifest=state.environment_manifest)
    _install_fake(monkeypatch, fake)
    daemon = CoordinareDaemon(AsyncMock(), max_cycles=1, sleep_func=AsyncMock())
    daemon._state["coordinare_config"] = mount_config()
    daemon._state["env_cache"] = _ctx(state)["env_cache"]
    ok, err, _infra = await daemon._verify_bootstrap_in_clean_context("sym", SVC, "job-1")
    assert (ok, err, _infra) == (True, None, False)
    assert [data for data in fake.stdin_seen if data is not None] == [state.rendered_verify_sh.encode()]

    svc.on_bootstrap_complete("sym", success=True, state=daemon._state)
    assert state.last_bootstrap_succeeded is True
    assert state.cache_dir_ready is True

    # 4. A consumer gets the read-only mount.
    result = get_env_volume_for_symphony(
        "sym", {"sym": state}, is_bootstrap=False, performer_mounts=(),
    )
    assert result is not None
    assert result[0].mode == "ro"


def test_rendered_scripts_survive_the_snapshot_round_trip(tmp_path: Path) -> None:
    """The daemon's scripts are persisted, so a restart still verifies the daemon's text."""
    cache = _cache(tmp_path)
    state = _state(cache)

    persisted = _persist_env_cache({"sym": state})["sym"]
    reloaded = EnvCacheStateSnapshot.model_validate_json(persisted.model_dump_json())

    assert reloaded.rendered_verify_sh == RENDERED_VERIFY
    assert reloaded.rendered_activate_sh == RENDERED_ACTIVATE


def test_an_old_snapshot_without_rendered_scripts_loads_closed(tmp_path: Path) -> None:
    """A v34 snapshot has no rendered scripts. It loads with None, which fails closed."""
    cache = _cache(tmp_path)
    snap = EnvCacheStateSnapshot(
        symphony_name="sym", sanitised_name="sym-abc", cache_dir=str(cache),
    )
    assert snap.rendered_verify_sh is None
    assert snap.rendered_activate_sh is None

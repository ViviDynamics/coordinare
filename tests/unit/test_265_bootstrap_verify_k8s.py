"""Spec 265: the daemon's rendered scripts reach the bootstrap performer, and the Kubernetes verdict is the performer's.

The bootstrap pod mounts its own RWX claim on Kubernetes, so the daemon's cache_dir holds no toolchain and the
daemon's docker run would check the wrong directory. These tests pin three things. The rendered text rides the
dispatch and stays out of the unredacted metadata. The daemon's clean verify is skipped on Kubernetes with a
reason, and never falsely passes or fails. A bootstrap passes on Kubernetes only when the performer ran the
daemon's rendered text and it passed (basis ``daemon_rendered``).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from coordinare.daemon import CoordinareDaemon
from coordinare.models.env_cache import (
    ENV_VERIFY_BASIS_DAEMON_RENDERED,
    BootstrapJobPayload,
    EnvCacheState,
)
from coordinare.models.performer_endpoint import PerformerEndpointConfig
from coordinare.services.env_cache import (
    EnvCacheService,
    daemon_sees_performer_cache,
    verify_env_cache_clean,
)
from coordinare.services.http_performer_service import HTTPPerformerService
from coordinare.services.kubernetes_verifier import VerifierConfigurationError

RENDERED_VERIFY = "#!/bin/bash\necho 'FAIL: ruby 3.4.2 not runnable' >&2\nexit 1\n"
RENDERED_ACTIVATE = "# AUTHORITATIVE activate.sh (daemon-rendered)\n"
SVC = SimpleNamespace(devenv_root="/devenv", _config=SimpleNamespace(image="img"))


def _state(cache: Path, *, transport: str, verify: str | None = RENDERED_VERIFY) -> dict[str, Any]:
    ec = EnvCacheState(
        symphony_name="sym",
        sanitised_name="sym-abc",
        cache_dir=cache,
        rendered_verify_sh=verify,
        rendered_activate_sh=RENDERED_ACTIVATE,
        bootstrap_commit_sha="f" * 40,
    )
    return {
        "coordinare_config": SimpleNamespace(agent_transport=transport, performer_image="trusted/global:release"),
        "env_cache": {"sym": ec},
    }


def _no_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any docker call is a failure: on Kubernetes the daemon must not run the clean verify."""

    async def _refuse(*argv: Any, **_kwargs: Any) -> None:
        raise AssertionError(f"the daemon must not run docker on kubernetes: {argv}")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _refuse)


def _daemon(state: dict[str, Any]) -> CoordinareDaemon:
    daemon = CoordinareDaemon(AsyncMock(), max_cycles=1, sleep_func=AsyncMock())
    daemon._state.update(state)
    return daemon


def _bootstrap_context(**extra: Any) -> dict[str, Any]:
    ctx: dict[str, Any] = {
        "job_type": "env_bootstrap",
        "symphony_name": "dettmore",
        "symphony_org": "ViviDynamics",
        "symphony_repo": "dettmore-platform",
        "env_spec_files": ["README.md"],
        "env_spec_contents": {"README.md": "## Setup"},
        "cache_mount_path": "/devenv/dettmore-platform-615891",
    }
    ctx.update(extra)
    return ctx


def _svc() -> HTTPPerformerService:
    config = PerformerEndpointConfig.model_validate({
        "id": "perf-e1", "mode": "ephemeral", "roles": ["env_bootstrap"], "image": "performer:base",
    })
    return HTTPPerformerService(config)


# ---------------------------------------------------------------------------
# The dispatch carries the rendered text, and keeps it out of metadata
# ---------------------------------------------------------------------------
def test_dispatch_model_carries_the_rendered_scripts() -> None:
    payload = BootstrapJobPayload(
        symphony_name="dettmore",
        symphony_org="ViviDynamics",
        symphony_repo="dettmore-platform",
        cache_mount_path="/devenv/dettmore-platform-615891",
        rendered_verify_sh=RENDERED_VERIFY,
        rendered_activate_sh=RENDERED_ACTIVATE,
    )
    dumped = payload.model_dump()
    assert dumped["rendered_verify_sh"] == RENDERED_VERIFY
    assert dumped["rendered_activate_sh"] == RENDERED_ACTIVATE


def test_payload_builder_sends_both_scripts_and_keeps_them_out_of_metadata() -> None:
    ctx = _bootstrap_context(rendered_verify_sh=RENDERED_VERIFY, rendered_activate_sh=RENDERED_ACTIVATE)

    payload = _svc()._build_env_bootstrap_payload(ctx)

    assert payload.bootstrap_scripts == {"verify_sh": RENDERED_VERIFY, "activate_sh": RENDERED_ACTIVATE}
    # The metadata is unredacted, so the scripts must not be copied into it.
    assert "rendered_verify_sh" not in payload.metadata
    assert RENDERED_VERIFY not in json.dumps(payload.metadata)


def test_payload_builder_sends_nothing_when_only_one_script_is_present() -> None:
    """Both or neither. A half pair would make the performer run a script the daemon did not vet."""
    ctx = _bootstrap_context(rendered_verify_sh=RENDERED_VERIFY)

    payload = _svc()._build_env_bootstrap_payload(ctx)

    assert payload.bootstrap_scripts is None


def test_payload_builder_with_no_rendered_scripts_is_unchanged() -> None:
    payload = _svc()._build_env_bootstrap_payload(_bootstrap_context())

    assert payload.bootstrap_scripts is None
    assert payload.env_cache_path == "/devenv/dettmore-platform-615891"


# ---------------------------------------------------------------------------
# The daemon's clean verify on Kubernetes: an explicit, logged skip, never a false verdict
# ---------------------------------------------------------------------------
def test_the_transport_is_read_from_the_config() -> None:
    assert daemon_sees_performer_cache({"coordinare_config": SimpleNamespace(agent_transport="subprocess")})
    assert not daemon_sees_performer_cache({"coordinare_config": SimpleNamespace(agent_transport="kubernetes")})
    # No config is the docker path, as before.
    assert daemon_sees_performer_cache({})


@pytest.mark.asyncio
async def test_kubernetes_clean_verify_refuses_unfrozen_state_without_docker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_docker(monkeypatch)
    state = _state(tmp_path, transport="kubernetes")

    passed, detail = await verify_env_cache_clean(state, "sym", SVC, performer_mounts=())

    # A missing verifier-backed freeze blocks readiness on Kubernetes.
    assert passed is False
    assert "kubernetes" in detail


class _FakeRuntime:
    """A Kubernetes runtime stand-in: records the verifier call and returns the verdict it was given."""

    verifies_clean_room = True

    def __init__(self, verdict: tuple[bool | None, str], *, raises: BaseException | None = None) -> None:
        self.verdict = verdict
        self.raises = raises
        self.calls: list[dict[str, Any]] = []

    async def run_verifier(self, **kwargs: Any) -> tuple[bool | None, str]:
        self.calls.append(kwargs)
        if self.raises is not None:
            raise self.raises
        return self.verdict


class _FakeGitHub:
    """Serves project files at a ref. A path it lacks is None, as the real service reports a missing file."""

    def __init__(self, files: dict[str, str], *, fail: bool = False) -> None:
        self.files = files
        self.fail = fail
        self.refs: list[str] = []

    async def get_file_content(self, owner: str, repo: str, path: str, ref: str = "HEAD") -> str | None:
        if self.fail:
            raise OSError("github down")
        self.refs.append(ref)
        return self.files.get(path)


def _k8s_daemon(
    state_dir: Path,
    *,
    runtime: _FakeRuntime,
    github: _FakeGitHub | None,
    verify: str | None = RENDERED_VERIFY,
    transport: str = "kubernetes",
) -> tuple[CoordinareDaemon, SimpleNamespace]:
    daemon = _daemon(_state(state_dir, transport=transport, verify=verify))
    daemon._state["symphony_configs"] = {
        "sym": SimpleNamespace(
            effective_config=lambda _g: SimpleNamespace(github_org="ViviDynamics", project_name=None),
        ),
    }
    daemon._state["coordinare_config"] = SimpleNamespace(
        agent_transport=transport, global_config=SimpleNamespace(performer_image="trusted/global:release"),
    )
    daemon._state["github_service"] = github
    svc = SimpleNamespace(devenv_root="/devenv", _config=SimpleNamespace(image="coordinare-performer:full"), _runtime=runtime)
    return daemon, svc


@pytest.mark.asyncio
async def test_kubernetes_completion_passes_only_on_the_clean_room_verdict(tmp_path: Path) -> None:
    runtime = _FakeRuntime((True, "COORDINARE_PROVENANCE=" + json.dumps({
        "content_digest": "a" * 64, "stat_digest": "b" * 64, "entry_count": 1, "total_bytes": 100,
        "inputs": {"activate_sh": RENDERED_ACTIVATE, "services_start_sh": None, "services_health_sh": None, "debs": {}},
    })))
    github = _FakeGitHub({"Gemfile": "source 'x'\n", ".ruby-version": "3.4.2\n"})
    daemon, svc = _k8s_daemon(tmp_path, runtime=runtime, github=github)
    daemon._state["env_cache"]["sym"].readme_sha = "a" * 40

    ok, err, infra = await daemon._verify_bootstrap_in_clean_context(
        "sym", svc, "job-1", env_verify_basis=ENV_VERIFY_BASIS_DAEMON_RENDERED,
    )

    assert (ok, err, infra) == (True, None, False)
    call, walk_call = runtime.calls
    assert walk_call["spec"].walk_only
    assert walk_call["spec"].project_files == {}
    assert walk_call["spec"].verify_text == ""
    assert call["spec"].mount_path == "/devenv/sym-abc"
    assert call["spec"].cache_subpath == "sym-abc"
    assert call["spec"].verify_text == RENDERED_VERIFY
    assert call["spec"].project_files == {"Gemfile": "source 'x'\n", ".ruby-version": "3.4.2\n"}
    # The project files come from the bootstrap's commit, at its SHA.
    assert set(github.refs) == {"f" * 40}


@pytest.mark.asyncio
async def test_the_bootstrap_self_report_never_decides_on_kubernetes(tmp_path: Path) -> None:
    """A bootstrap that says it verified, with the clean-room verifier failing, does not pass."""
    runtime = _FakeRuntime((False, "FAIL: ruby 3.4.2 not runnable"))
    daemon, svc = _k8s_daemon(tmp_path, runtime=runtime, github=_FakeGitHub({}))
    daemon._state["env_cache"]["sym"].readme_sha = "b" * 40

    ok, err, infra = await daemon._verify_bootstrap_in_clean_context(
        "sym", svc, "job-1", env_verify_basis=ENV_VERIFY_BASIS_DAEMON_RENDERED,
    )

    assert ok is False
    assert infra is False
    assert "clean-room verifier" in (err or "")
    assert "ruby 3.4.2 not runnable" in (err or "")


@pytest.mark.asyncio
async def test_a_verifier_that_could_not_run_is_infrastructure(tmp_path: Path) -> None:
    runtime = _FakeRuntime((None, "verifier pod could not be created: ApiException"))
    daemon, svc = _k8s_daemon(tmp_path, runtime=runtime, github=_FakeGitHub({}))
    daemon._state["env_cache"]["sym"].readme_sha = "c" * 40

    ok, err, infra = await daemon._verify_bootstrap_in_clean_context("sym", svc, "job-1")

    assert ok is False
    assert infra is True
    assert "could not run" in (err or "")


@pytest.mark.asyncio
async def test_a_github_read_failure_is_infrastructure_not_a_verdict(tmp_path: Path) -> None:
    runtime = _FakeRuntime((True, "PASSED"))
    daemon, svc = _k8s_daemon(tmp_path, runtime=runtime, github=_FakeGitHub({}, fail=True))
    daemon._state["env_cache"]["sym"].readme_sha = "d" * 40

    ok, _err, infra = await daemon._verify_bootstrap_in_clean_context("sym", svc, "job-1")

    assert ok is False
    assert infra is True
    assert runtime.calls == [], "no verifier pod is started without the project files"


@pytest.mark.asyncio
async def test_a_project_path_the_verifier_refuses_fails_without_a_pod(tmp_path: Path) -> None:
    runtime = _FakeRuntime((True, "PASSED"), raises=VerifierConfigurationError("project file path '../x' is not a plain relative path"))
    daemon, svc = _k8s_daemon(tmp_path, runtime=runtime, github=_FakeGitHub({"Gemfile": "x"}))
    daemon._state["env_cache"]["sym"].readme_sha = "e" * 40

    ok, err, infra = await daemon._verify_bootstrap_in_clean_context("sym", svc, "job-1")

    assert (ok, infra) == (False, False)
    assert "configuration failure" in (err or "")
    assert daemon._state["env_cache"]["sym"].bootstrap_exhausted


@pytest.mark.asyncio
async def test_kubernetes_completion_fails_closed_without_the_scripts_on_record(tmp_path: Path) -> None:
    runtime = _FakeRuntime((True, "PASSED"))
    daemon, svc = _k8s_daemon(tmp_path, runtime=runtime, github=_FakeGitHub({}), verify=None)

    ok, _err, _infra = await daemon._verify_bootstrap_in_clean_context("sym", svc, "job-1")

    assert ok is False
    assert runtime.calls == []


@pytest.mark.asyncio
async def test_a_runtime_without_the_verifier_fails_closed(tmp_path: Path) -> None:
    runtime = _FakeRuntime((True, "PASSED"))
    runtime.verifies_clean_room = False
    daemon, svc = _k8s_daemon(tmp_path, runtime=runtime, github=_FakeGitHub({}))

    ok, err, _infra = await daemon._verify_bootstrap_in_clean_context("sym", svc, "job-1")

    assert ok is False
    assert "clean-room verifier pod" in (err or "")


@pytest.mark.asyncio
async def test_kubernetes_success_is_not_downgraded_by_the_daemon_seed_directory(tmp_path: Path) -> None:
    """B3: the daemon's cache_dir holds only the seed files on Kubernetes. A verified success must still stand."""
    cache = tmp_path / "daemon" / "sym-abc"
    cache.mkdir(parents=True)
    for name in ("activate.sh", "manifest.json", "verify.sh"):
        (cache / name).write_text("x")
    service = EnvCacheService.__new__(EnvCacheService)
    # An instance attribute shadows the method, so the service reads no performer mounts from the test machine.
    service.__dict__["_performer_mounts"] = lambda: ()
    service._restart_verified = set()
    service._coordinare_config = None
    ec = EnvCacheState(
        symphony_name="sym", sanitised_name="sym-abc", cache_dir=cache,
        rendered_activate_sh="x", bootstrap_in_flight=True,
    )
    state = {
        "env_cache": {"sym": ec},
        "coordinare_config": SimpleNamespace(agent_transport="kubernetes", performer_image="trusted/global:release"),
    }

    service.on_bootstrap_complete("sym", True, state)

    assert ec.last_bootstrap_succeeded is True
    assert ec.cache_dir_ready is True


@pytest.mark.asyncio
async def test_kubernetes_freeze_refuses_rather_than_freezing_the_seed_directory(tmp_path: Path) -> None:
    cache = tmp_path / "daemon" / "sym-abc"
    cache.mkdir(parents=True)
    service = EnvCacheService.__new__(EnvCacheService)
    ec = EnvCacheState(
        symphony_name="sym", sanitised_name="sym-abc", cache_dir=cache, rendered_activate_sh="x",
    )
    state = {"env_cache": {"sym": ec}, "coordinare_config": SimpleNamespace(agent_transport="kubernetes", performer_image="trusted/global:release")}

    ok, reason = await service.freeze_provenance("sym", state)

    assert ok is False
    assert "kubernetes" in (reason or "")
    assert ec.provenance_digest is None


@pytest.mark.asyncio
async def test_docker_transport_still_runs_the_clean_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The docker transport is unchanged: the daemon's own clean verify decides, whatever basis the performer sent."""
    daemon = _daemon(_state(tmp_path, transport="subprocess"))
    clean = AsyncMock(return_value=(True, ""))
    monkeypatch.setattr(daemon, "_verify_env_cache_clean", clean)

    ok, err, infra = await daemon._verify_bootstrap_in_clean_context(
        "sym", SVC, "job-1", env_verify_basis=ENV_VERIFY_BASIS_DAEMON_RENDERED,
    )

    clean.assert_awaited_once()
    assert (ok, err, infra) == (True, None, False)

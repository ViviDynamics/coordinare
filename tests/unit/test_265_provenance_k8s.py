"""Kubernetes provenance follows the claim, including restart and refusal recovery."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from coordinare.daemon import CoordinareDaemon, _persist_env_cache, _rehydrate_env_cache
from coordinare.graph.nodes import dispatch_performer
from coordinare.models.env_cache import EnvCacheState
from coordinare.services.env_cache import EnvCacheService, verify_env_cache_clean
from coordinare.services.kubernetes_verifier import VerifierSpec, build_verifier_pod_manifest
from tests.unit.services.test_400_forced_regen_single_bootstrap import _cycle, _github
from tests.unit.services.test_env_cache_phase4 import _make_cfg, _seed_state
from tests.unit.test_060_env_cache import _seed_toolchain

ACTIVATE = "export PATH=$DEVENV/bin:$PATH\n"
DIGEST = "a" * 64
STAT = "b" * 64
RESULT_LIMIT = 1024 * 1024
SINGLE_ATTEMPT_BUDGET = 1


def summary(**changes: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "content_digest": DIGEST, "stat_digest": STAT, "entry_count": 4, "total_bytes": 100,
        "inputs": {"activate_sh": ACTIVATE, "services_start_sh": None, "services_health_sh": None,
                   "debs": {"debs/libfoo.deb": "c" * 64}},
    }
    result.update(changes)
    return result


def logs(result: dict[str, Any]) -> str:
    return "COORDINARE_PROVENANCE=" + json.dumps(result) + "\n"


class FakeRuntime:
    verifies_clean_room = True

    def __init__(self, results: list[tuple[bool | None, str]]) -> None:
        self.results = results
        self.pods: list[dict[str, Any]] = []
        self.specs: list[VerifierSpec] = []

    async def run_verifier(self, *, slug: str, spec: VerifierSpec) -> tuple[bool | None, str]:
        self.specs.append(spec)
        self.pods.append(build_verifier_pod_manifest(spec, name="verify-sym", namespace="test"))
        return self.results.pop(0)


def setup(tmp_path: Path, results: list[tuple[bool | None, str]]) -> tuple[Any, ...]:
    runtime = FakeRuntime([(True, "verify passed"), *results] if results else [])
    cfg = SimpleNamespace(agent_transport="kubernetes", global_config=SimpleNamespace(
        performer_endpoints=[], performer_image="trusted/global:release", env_cache_root=tmp_path / "daemon", env_bootstrap_max_attempts=2,
        state_file_path=tmp_path / "state.json",
    ))
    service = EnvCacheService(cfg)
    ec = EnvCacheState(symphony_name="sym", sanitised_name="sym-abc", cache_dir=tmp_path / "daemon" / "sym-abc",
                       rendered_activate_sh=ACTIVATE, rendered_verify_sh="exit 0\n", readme_sha="cache-key",
                       bootstrap_commit_sha="e" * 40)
    performer = SimpleNamespace(_runtime=runtime, _config=SimpleNamespace(image="performer:full"), devenv_root="/devenv")
    sym_cfg = SimpleNamespace(env_bootstrap_performer_id="bootstrap", effective_config=lambda _: SimpleNamespace(
        github_org="org", project_name="repo"))
    state = {"coordinare_config": cfg, "env_cache": {"sym": ec}, "env_cache_service": service,
             "performer_services_by_id": {"bootstrap": performer}, "symphony_configs": {"sym": sym_cfg},
             "github_service": SimpleNamespace(get_file_content=AsyncMock(return_value=None))}
    daemon = CoordinareDaemon(AsyncMock(), max_cycles=1, sleep_func=AsyncMock())
    daemon._state.update(state)
    return service, ec, state, daemon, performer, runtime


async def freeze(service: Any, state: Any, daemon: Any, performer: Any) -> None:
    ok, error, infra = await daemon._verify_bootstrap_in_clean_context("sym", performer, "job")
    assert ok, error
    assert not infra
    ok, error = await service.freeze_provenance("sym", state)
    assert ok, error
    service.on_bootstrap_complete("sym", True, state)


@pytest.mark.asyncio
async def test_freeze_uses_claim_summary_without_a_daemon_manifest(tmp_path: Path) -> None:
    service, ec, state, daemon, performer, runtime = setup(tmp_path, [(True, logs(summary()))])
    await freeze(service, state, daemon, performer)
    assert ec.provenance_digest == DIGEST
    assert ec.provenance_stat_digest == STAT
    assert ec.provenance_inputs == summary()["inputs"]
    assert ec.provenance_entry_count == 4
    assert ec.provenance_total_bytes == 100
    assert not (tmp_path / "env-provenance").exists()
    pod = runtime.pods[0]["spec"]
    assert pod["containers"][0]["volumeMounts"][0]["readOnly"] is True
    assert pod["automountServiceAccountToken"] is False
    assert pod["containers"][0]["stdin"] is False
    assert len(pod["volumes"]) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(("result", "ok", "transient"), [
    ((True, logs(summary())), True, False),
    ((True, logs(summary(content_digest="d" * 64))), False, False),
    ((None, "api unavailable"), False, True),
])
async def test_full_recheck_match_drift_and_infrastructure(
    tmp_path: Path, result: tuple[bool | None, str], ok: bool, transient: bool,
) -> None:
    service, ec, state, daemon, performer, runtime = setup(tmp_path, [(True, logs(summary())), result])
    await freeze(service, state, daemon, performer)
    check = await service.run_full_pass("sym", ec)
    assert check is not None
    assert check.ok is ok
    assert check.transient is transient
    assert ("sym" in service._drift) is (not ok and not transient)
    assert runtime.specs[-1].walk_only is True
    assert runtime.specs[-1].project_files == {}
    assert ec.provenance_digest == DIGEST


@pytest.mark.asyncio
async def test_restart_rechecks_and_builds_payload_from_frozen_inputs(tmp_path: Path) -> None:
    service, ec, state, daemon, performer, runtime = setup(tmp_path, [(True, logs(summary())), (True, logs(summary()))])
    await freeze(service, state, daemon, performer)
    persisted = _persist_env_cache(state["env_cache"])["sym"]
    restored = EnvCacheState.model_validate(ec.model_dump())
    restored.provenance_inputs = None
    snapshot = SimpleNamespace(env_cache={"sym": persisted})
    _rehydrate_env_cache({"env_cache": {"sym": restored}}, snapshot)
    assert restored.provenance_inputs == summary()["inputs"]
    fresh = EnvCacheService(state["coordinare_config"])
    state["env_cache"]["sym"] = restored
    fresh.bind_kubernetes_state(state)
    check, payload = await fresh.resolve_provenance("sym", restored, container_path="/devenv/sym-abc")
    assert check.ok
    assert payload == {"version": 2, "slug": "sym-abc", "cache_path": "/devenv/sym-abc", "digest": DIGEST,
                       "stat_digest": STAT, "max_entries": 1_000_000, "max_bytes": 16 * 1024**3,
                       **summary()["inputs"]}
    assert runtime.specs[-1].walk_only is True
    assert not (tmp_path / "env-provenance").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [
    "no result", "COORDINARE_PROVENANCE={}", "COORDINARE_PROVENANCE=" + "x" * RESULT_LIMIT,
    logs(summary(inputs={**summary()["inputs"], "services_start_sh": "x" * RESULT_LIMIT})),
    logs(summary()) + logs(summary()),
    logs(summary(content_digest="bad")), logs(summary(entry_count=True)),
    logs(summary(total_bytes=-1)), logs(summary(inputs={"activate_sh": "hostile"})),
    logs(summary(entry_count=1_000_001)),
    logs(summary(inputs={**summary()["inputs"], "debs": {"debs/../evil.deb": "c" * 64}})),
])
async def test_malformed_or_oversized_summary_never_freezes(tmp_path: Path, bad: str) -> None:
    service, ec, state, daemon, performer, _runtime = setup(tmp_path, [(True, bad)])
    ok, _reason, _infra = await daemon._verify_bootstrap_in_clean_context("sym", performer, "job")
    if ok:
        ok, _reason = await service.freeze_provenance("sym", state)
    assert not ok
    assert ec.provenance_digest is None
    assert not ec.cache_dir_ready


@pytest.mark.asyncio
async def test_kubernetes_readiness_requires_persisted_verification_and_digest(tmp_path: Path) -> None:
    service, ec, state, daemon, performer, _runtime = setup(tmp_path, [(True, logs(summary()))])
    passed, _ = await verify_env_cache_clean(state, "sym", performer, performer_mounts=())
    assert passed is False
    await freeze(service, state, daemon, performer)
    passed, _ = await verify_env_cache_clean(state, "sym", performer, performer_mounts=())
    assert passed is True
    ec.provenance_digest = None
    passed, _ = await verify_env_cache_clean(state, "sym", performer, performer_mounts=())
    assert passed is False


def test_consumer_refusal_spends_budget_across_successful_rebuilds(tmp_path: Path) -> None:
    service, ec, state, _daemon, _performer, _runtime = setup(tmp_path, [])
    service.mark_runtime_health_failed("sym", state)
    service.mark_runtime_health_failed("sym", state)
    assert ec.bootstrap_attempts == 1
    ec.runtime_health_failed = False
    service.on_bootstrap_complete("sym", True, state)
    assert ec.bootstrap_attempts == 1
    service.mark_runtime_health_failed("sym", state)
    assert ec.bootstrap_attempts == 2
    assert ec.bootstrap_exhausted


@pytest.mark.asyncio
@pytest.mark.parametrize(("result", "repair"), [
    ((True, logs(summary(stat_digest="d" * 64))), False),
    ((True, logs(summary(content_digest="d" * 64))), True),
    ((None, "api unavailable"), False),
])
async def test_consumer_stat_refusal_rechecks_content_before_spending(
    tmp_path: Path, result: tuple[bool | None, str], repair: bool,
) -> None:
    service, ec, state, daemon, performer, runtime = setup(tmp_path, [(True, logs(summary())), result])
    await freeze(service, state, daemon, performer)
    await service.recheck_consumer_provenance("sym", state)
    assert ec.runtime_health_failed is repair
    assert ec.bootstrap_attempts == int(repair)
    assert runtime.specs[-1].walk_only
    if result[0] is True and not repair:
        assert ec.provenance_stat_digest == "d" * 64


@pytest.mark.asyncio
async def test_dispatch_gates_accept_only_a_frozen_kubernetes_claim(tmp_path: Path) -> None:
    service, ec, state, daemon, performer, _runtime = setup(tmp_path, [(True, logs(summary()))])
    released: list[bool] = []
    ec.last_seen_spec_sha = ec.readme_sha
    terminal = await dispatch_performer._env_readiness_gate(
        state, performer, "card", "implementer", "sym", ec, False, lambda: released.append(True))
    assert terminal is state
    assert released == [True]
    ec.runtime_health_failed = False
    await freeze(service, state, daemon, performer)
    terminal = await dispatch_performer._env_cache_hold(
        state, "card", "implementer", "sym", ec, False, lambda: released.append(True))
    assert terminal is None
    terminal = await dispatch_performer._env_readiness_gate(
        state, performer, "card", "implementer", "sym", ec, False, lambda: released.append(True))
    assert terminal is None


@pytest.mark.asyncio
async def test_project_files_use_a_real_commit_instead_of_the_cache_fingerprint(tmp_path: Path) -> None:
    service, ec, state, daemon, performer, _runtime = setup(tmp_path, [(True, logs(summary()))])
    ec.readme_sha = "synthetic-cache-key:suffix"
    ec.bootstrap_commit_sha = "e" * 40
    github = state["github_service"]
    await freeze(service, state, daemon, performer)
    assert {call.kwargs["ref"] for call in github.get_file_content.call_args_list} == {"e" * 40}


@pytest.mark.asyncio
@pytest.mark.parametrize("manages_services", [True, False])
async def test_kubernetes_bootstrap_pins_all_rendering_reads_to_one_commit(
    tmp_path: Path, manages_services: bool,
) -> None:
    service, ec, state, _daemon, _performer, _runtime = setup(tmp_path, [])
    cfg = state["coordinare_config"]
    cfg.global_config.env_cache = SimpleNamespace(coordinare_manages_services=manages_services)
    ec.cache_dir.mkdir(parents=True)
    sym = state["symphony_configs"]["sym"]
    sym.env_spec_files = ["README.md"]
    sym.test_env = None
    refs: list[str | None] = []

    async def content(_org: str, _repo: str, path: str, *, ref: str | None = None) -> str | None:
        refs.append(ref)
        return "Environment" if path == "README.md" else None

    github = SimpleNamespace(get_branch_head_sha=AsyncMock(return_value="e" * 40), get_file_content=content)
    await service._do_dispatch(symphony_name="sym", symphony_config=sym, github_service=github,
                               eff_config=sym.effective_config(cfg.global_config), current_sha="cache-key",
                               cache_state=ec, dispatch_fn=AsyncMock())
    assert ec.bootstrap_commit_sha == "e" * 40
    assert set(refs) == {"e" * 40}


@pytest.mark.asyncio
@pytest.mark.parametrize("repair_succeeds", [True, False])
async def test_first_runtime_repair_dispatches_with_single_attempt_budget(
    tmp_path: Path, repair_succeeds: bool,
) -> None:
    name = "my-project"
    state, cache, ec = _seed_state(tmp_path, name)
    cfg, sym = _make_cfg(name, tmp_path)
    cfg.global_config.env_bootstrap_max_attempts = SINGLE_ATTEMPT_BUDGET
    service = EnvCacheService(cfg)
    github = _github("sealed-sha")
    ec.readme_sha = await service._current_manifest_sha(name, sym, github, sym.effective_config(cfg.global_config), ec)
    dispatched: list[str] = []

    service.mark_runtime_health_failed(name, state)
    service.mark_runtime_health_failed(name, state)
    await _cycle(service, name, sym, github, state, dispatched)
    assert dispatched == ["bootstrap-env"]
    assert ec.bootstrap_in_flight
    assert not ec.bootstrap_exhausted
    assert ec.bootstrap_attempts == SINGLE_ATTEMPT_BUDGET

    _seed_toolchain(cache)
    service.on_bootstrap_complete(name, repair_succeeds, state)
    if repair_succeeds:
        assert not ec.bootstrap_exhausted
        assert ec.bootstrap_attempts == SINGLE_ATTEMPT_BUDGET
        service.mark_runtime_health_failed(name, state)
    assert ec.bootstrap_exhausted
    await _cycle(service, name, sym, github, state, dispatched)
    assert dispatched == ["bootstrap-env"]


@pytest.mark.asyncio
async def test_runtime_budget_survives_restart_after_a_successful_repair(tmp_path: Path) -> None:
    service, ec, state, _daemon, _performer, _runtime = setup(tmp_path, [])
    service.mark_runtime_health_failed("sym", state)
    ec.runtime_health_failed = False
    service.on_bootstrap_complete("sym", True, state)
    persisted = _persist_env_cache(state["env_cache"])["sym"]
    fresh = EnvCacheState(symphony_name="sym", sanitised_name="sym-abc", cache_dir=ec.cache_dir)
    _rehydrate_env_cache({"env_cache": {"sym": fresh}}, SimpleNamespace(env_cache={"sym": persisted}))
    restarted = EnvCacheService(state["coordinare_config"])
    state["env_cache"]["sym"] = fresh
    restarted.mark_runtime_health_failed("sym", state)
    assert fresh.bootstrap_attempts == 2
    assert fresh.bootstrap_exhausted


@pytest.mark.asyncio
async def test_forced_inflight_cycles_and_restart_do_not_rearm_the_refusal_budget(tmp_path: Path) -> None:
    name = "my-project"
    state, cache, ec = _seed_state(tmp_path, name)
    cfg, sym = _make_cfg(name, tmp_path)
    service = EnvCacheService(cfg)
    github = _github("sealed-sha")
    ec.readme_sha = await service._current_manifest_sha(name, sym, github, sym.effective_config(cfg.global_config), ec)
    dispatched: list[str] = []
    service.mark_runtime_health_failed(name, state)
    await _cycle(service, name, sym, github, state, dispatched)
    await _cycle(service, name, sym, github, state, dispatched)
    assert ec.bootstrap_attempts == 1
    persisted = _persist_env_cache(state["env_cache"])[name]
    fresh = EnvCacheState(symphony_name=name, sanitised_name=ec.sanitised_name, cache_dir=cache)
    _rehydrate_env_cache({"env_cache": {name: fresh}}, SimpleNamespace(env_cache={name: persisted}))
    restarted = EnvCacheService(cfg)
    state["env_cache"][name] = fresh
    await _cycle(restarted, name, sym, github, state, dispatched)
    assert fresh.bootstrap_attempts == 1
    _seed_toolchain(cache)
    restarted.on_bootstrap_complete(name, True, state)
    assert fresh.bootstrap_attempts == 1

    restarted.mark_runtime_health_failed(name, state)
    await _cycle(restarted, name, sym, github, state, dispatched)
    await _cycle(restarted, name, sym, github, state, dispatched)
    _seed_toolchain(cache)
    restarted.on_bootstrap_complete(name, True, state)
    assert fresh.bootstrap_attempts == 2
    restarted.mark_runtime_health_failed(name, state)
    assert fresh.bootstrap_exhausted
    dispatch_count = len(dispatched)
    await _cycle(restarted, name, sym, github, state, dispatched)
    assert len(dispatched) == dispatch_count


@pytest.mark.asyncio
async def test_forced_metadata_failure_cannot_erase_the_refusal_budget(tmp_path: Path) -> None:
    name = "my-project"
    state, _cache, ec = _seed_state(tmp_path, name)
    cfg, sym = _make_cfg(name, tmp_path)
    service = EnvCacheService(cfg)
    github = _github("sealed-sha")
    ec.readme_sha = await service._current_manifest_sha(name, sym, github, sym.effective_config(cfg.global_config), ec)
    fingerprint = ec.readme_sha
    service.mark_runtime_health_failed(name, state)
    github.get_file_blob_sha.return_value = None
    dispatched: list[str] = []
    await _cycle(service, name, sym, github, state, dispatched)
    assert dispatched == []
    assert ec.runtime_health_failed
    assert ec.bootstrap_budget_sha == fingerprint
    github.get_file_blob_sha.return_value = "sealed-sha"
    await _cycle(service, name, sym, github, state, dispatched)
    await _cycle(service, name, sym, github, state, dispatched)
    assert len(dispatched) == 1
    assert ec.bootstrap_attempts == 1


@pytest.mark.asyncio
async def test_kubernetes_consumer_payload_bounds_a_long_symphony_slug(tmp_path: Path) -> None:
    service, ec, state, daemon, performer, _runtime = setup(tmp_path, [(True, logs(summary()))])
    ec.sanitised_name = "symphony-" * 20
    await freeze(service, state, daemon, performer)
    check, payload = await service.resolve_provenance("sym", ec, container_path=f"/devenv/{ec.sanitised_name}")
    assert check.ok
    assert payload is not None
    assert len(payload["slug"]) <= 120
    assert payload["slug"] != ec.sanitised_name

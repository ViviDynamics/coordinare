"""Regression tests for isolated verification and bounded provenance collection."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from coordinare_service_inference.cache_provenance import MAX_SCRIPT_BYTES, RESULT_SENTINEL
from coordinare_service_inference.environment.source_payload import (
    SOURCE_FILE_MAX_BYTES,
    encode_sources,
)

from coordinare.services.env_provenance import DEFAULT_LIMITS
from coordinare.services.kubernetes_provenance import parse_summary
from coordinare.services.kubernetes_runtime import KubernetesRuntime
from coordinare.services.kubernetes_verifier import PROJECT_FILES_MAX_BYTES, VERIFIER_POD_MAX_BYTES
from tests.unit.env_manifest_fixture import verification_output
from tests.unit.test_265_k8s_verifier import (
    CLAIM,
    NAMESPACE,
    _FakeCoreV1,
    _manifest,
    _run,
    _run_in_pod,
)
from tests.unit.test_265_provenance_k8s import ACTIVATE, freeze, logs, setup, summary


def check_log(ec, output=""):
    report = json.loads(verification_output(ec.environment_manifest))
    report["results"][0]["output"] = output
    return json.dumps(report)

POD_MARGIN_BYTES = 32 * 1024
LOG_BOUND_BYTES = 1024 * 1024 + 2000


@pytest.mark.asyncio
@pytest.mark.parametrize("verify_output", [
    "unterminated output",
    "verify\n" + logs(summary(content_digest="f" * 64)),
    "junk" + logs(summary()) + logs(summary(content_digest="f" * 64)),
    logs(summary()) + logs(summary(content_digest="f" * 64)),
])
async def test_only_the_second_pod_can_supply_provenance(tmp_path: Path, verify_output: str) -> None:
    _service, ec, _state, daemon, performer, runtime = setup(tmp_path, [])
    runtime.results = [(True, check_log(ec, verify_output)), (True, logs(summary()))]
    verdict = await daemon._verify_bootstrap_in_clean_context("sym", performer, "job")
    assert verdict == (True, None, False)
    assert len(runtime.specs) == 2
    verify, walk = runtime.specs
    assert not verify.walk_only
    assert verify.activate_text is None
    assert walk.walk_only
    assert walk.verify_text == ""
    assert walk.project_files == {}
    assert walk.activate_text == ACTIVATE
    assert verify.mount_path == walk.mount_path
    assert verify.cache_subpath == walk.cache_subpath
    assert ec.verifier_provenance == summary()
    assert runtime.results == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, None])
async def test_verify_failure_never_starts_a_walk(tmp_path: Path, failure: bool | None) -> None:
    _service, ec, _state, daemon, performer, runtime = setup(tmp_path, [])
    runtime.results = [(failure, "verify failed")]
    ok, _reason, infra = await daemon._verify_bootstrap_in_clean_context("sym", performer, "job")
    assert not ok
    assert infra is (failure is None)
    assert len(runtime.specs) == 1
    assert ec.verifier_provenance is None


def test_midline_sentinel_cannot_hide_a_duplicate() -> None:
    with pytest.raises(ValueError, match="duplicated"):
        parse_summary("junk" + logs(summary()) + logs(summary()), ACTIVATE, DEFAULT_LIMITS)


def test_verify_command_emits_no_walker_result_with_unterminated_output(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "activate.sh").write_text(ACTIVATE)
    manifest = _manifest(mount_path=str(cache), activate_text=ACTIVATE, verify_text='printf "unterminated"\n')
    result = _run_in_pod(manifest, tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "unterminated"
    assert RESULT_SENTINEL not in result.stdout


@pytest.mark.asyncio
async def test_log_api_receives_both_bounds() -> None:
    class BoundedCore(_FakeCoreV1):
        def read_namespaced_pod_log(self, **kwargs: Any) -> str:
            assert kwargs["tail_lines"] > 0
            assert kwargs["limit_bytes"] == LOG_BOUND_BYTES
            return "unterminated"

    runtime = KubernetesRuntime(namespace=NAMESPACE, cache_claim=CLAIM, core_v1=BoundedCore(["Failed"]))
    ok, detail = await _run(runtime)
    assert ok is False
    assert detail == "unterminated"


def _maximum_project(character: str) -> dict[str, str]:
    # Fill the aggregate document using individually admissible source files.
    width = len(json.dumps(character)[1:-1])
    project = {f"part-{index}": character * (SOURCE_FILE_MAX_BYTES // width) for index in range(64)}
    encoded = json.dumps(project, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    excess = len(encoded) - PROJECT_FILES_MAX_BYTES
    if excess > 0:
        project["part-63"] = project["part-63"][:-((excess + width - 1) // width)]
    assert PROJECT_FILES_MAX_BYTES - width < encode_sources(project).size_bytes <= PROJECT_FILES_MAX_BYTES
    return project


def test_maximum_legal_project_and_verify_inputs_fit_with_margin() -> None:
    project = _maximum_project("x")
    pod = _manifest(project_files=project, verify_text="\0" * MAX_SCRIPT_BYTES)
    assert len(json.dumps(pod).encode()) <= VERIFIER_POD_MAX_BYTES - POD_MARGIN_BYTES


@pytest.mark.asyncio
async def test_walk_failure_does_not_freeze_verify_output(tmp_path: Path) -> None:
    _service, ec, _state, daemon, performer, runtime = setup(tmp_path, [])
    runtime.results = [(True, check_log(ec, logs(summary()))), (None, "walk unavailable")]
    ok, _reason, infra = await daemon._verify_bootstrap_in_clean_context("sym", performer, "job")
    assert not ok
    assert infra
    assert len(runtime.specs) == 2
    assert ec.verifier_provenance is None


@pytest.mark.asyncio
async def test_verify_pod_must_be_gone_before_walking(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = KubernetesRuntime(namespace=NAMESPACE, cache_claim=CLAIM, core_v1=_FakeCoreV1(["Succeeded"]))
    _service, ec, _state, daemon, performer, _runtime = setup(tmp_path, [])
    performer._runtime = runtime

    async def still_present(*args: Any, **kwargs: Any) -> bool:
        return False

    monkeypatch.setattr(runtime, "confirm_stopped", still_present)
    ok, reason, infra = await daemon._verify_bootstrap_in_clean_context("sym", performer, "job")
    assert not ok
    assert infra
    assert "removed" in reason
    assert ec.verifier_provenance is None
    assert len(runtime._api.created) == 1


@pytest.mark.asyncio
async def test_runtime_runs_two_hardened_pods_with_same_cache_isolation_in_order(tmp_path: Path) -> None:
    class IsolatedCore(_FakeCoreV1):
        def create_namespaced_pod(self, namespace: str, body: dict[str, Any]) -> None:
            assert len(self.created) == len(self.deleted)
            return super().create_namespaced_pod(namespace, body)

        def read_namespaced_pod_log(self, **kwargs: Any) -> str:
            container = self.created[-1]["body"]["spec"]["containers"][0]
            env = {item["name"]: item["value"] for item in container["env"]}
            if env["WALK_ONLY"] == "0":
                assert env["PROVENANCE_CONFIG"] == ""
                return check_log(ec, "junk" + logs(summary(content_digest="f" * 64)))
            assert len(self.deleted) == 1
            return logs(summary())

    core = IsolatedCore(["Succeeded"])
    _service, ec, _state, daemon, performer, _runtime = setup(tmp_path, [])
    performer._runtime = KubernetesRuntime(namespace=NAMESPACE, cache_claim=CLAIM, core_v1=core)
    assert await daemon._verify_bootstrap_in_clean_context("sym", performer, "job") == (True, None, False)
    assert ec.verifier_provenance == summary()
    assert len(core.created) == len(core.deleted) == 2
    verify, walk = [item["body"]["spec"] for item in core.created]
    assert verify["securityContext"] == {**walk["securityContext"], "fsGroup": 10001}
    assert "fsGroup" not in walk["securityContext"]
    assert verify["containers"][0]["securityContext"] == walk["containers"][0]["securityContext"]
    cache_mounts = [next(mount for mount in spec["containers"][0]["volumeMounts"]
                         if mount["name"] == "devenv-cache") for spec in (verify, walk)]
    assert cache_mounts[0] == cache_mounts[1]
    assert cache_mounts[0]["readOnly"] is True
    scratch = next(mount for mount in verify["containers"][0]["volumeMounts"] if mount["name"] == "verifier-scratch")
    assert scratch["mountPath"] == "/tmp"
    assert not scratch.get("readOnly", False)


@pytest.mark.parametrize("walk_only", [False, True])
def test_maximum_inputs_with_escaping_fit_both_pods(walk_only: bool) -> None:
    script = "\0" * MAX_SCRIPT_BYTES
    pod = _manifest(walk_only=walk_only, activate_text=script, verify_text="" if walk_only else script,
                    project_files={} if walk_only else _maximum_project("\0"))
    assert len(json.dumps(pod).encode()) <= VERIFIER_POD_MAX_BYTES - POD_MARGIN_BYTES


@pytest.mark.asyncio
async def test_permission_error_from_walk_is_a_bootstrap_verdict(tmp_path: Path) -> None:
    _service, ec, _state, daemon, performer, runtime = setup(tmp_path, [])
    reason = "claim PermissionError: make files world-readable and directories world-traversable (mode o+rX); uid 10001, no fsGroup"
    runtime.results = [(True, check_log(ec)), (True, logs({"error": reason, "transient": False}))]
    ok, error, infra = await daemon._verify_bootstrap_in_clean_context("sym", performer, "job")
    assert not ok
    assert not infra
    assert "o+rX" in error
    assert ec.verifier_provenance is None


@pytest.mark.asyncio
async def test_permission_error_during_recheck_is_not_infrastructure(tmp_path: Path) -> None:
    service, ec, state, daemon, performer, _runtime = setup(tmp_path, [
        (True, logs(summary())),
        (True, logs({"error": "PermissionError: repair claim permissions with o+rX", "transient": False})),
    ])
    await freeze(service, state, daemon, performer)
    check = await service.run_full_pass("sym", ec)
    assert check is not None
    assert not check.ok
    assert not check.transient
    assert "o+rX" in check.reason


@pytest.mark.asyncio
async def test_changed_activation_during_verify_cannot_be_frozen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _service, ec, _state, daemon, performer, runtime = setup(tmp_path, [])
    runtime.results = [(True, check_log(ec)), (True, logs(summary()))]
    run = runtime.run_verifier

    async def changing_verify(**kwargs: Any) -> tuple[bool | None, str]:
        result = await run(**kwargs)
        if not kwargs["spec"].walk_only:
            ec.rendered_activate_sh = "changed while verifying"
        return result

    monkeypatch.setattr(runtime, "run_verifier", changing_verify)
    ok, _error, infra = await daemon._verify_bootstrap_in_clean_context("sym", performer, "job")
    assert not ok
    assert infra
    assert runtime.specs[-1].activate_text == ACTIVATE
    assert ec.verifier_provenance is None


def test_project_text_that_is_not_utf8_is_refused() -> None:
    with pytest.raises(UnicodeError):
        _manifest(project_files={"Gemfile": "\ud800"})

"""Regression coverage for the second security review of the claim verifier."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from coordinare_service_inference.cache_provenance import MAX_RESULT_BYTES
from kubernetes.client.rest import ApiException
from pydantic import ValidationError

from coordinare.config import ProjectConfiguration
from coordinare.daemon import _persist_env_cache, _rehydrate_env_cache
from coordinare.graph.nodes.monitor.body import _BodyCtx, phase_651
from coordinare.services.env_provenance import DEFAULT_LIMITS, ProvenanceTransientError
from coordinare.services.kubernetes_provenance import parse_summary
from coordinare.services.kubernetes_runtime import KubernetesRuntime
from coordinare.services.kubernetes_verifier import (
    VERIFIER_LOG_MAX_BYTES,
    VERIFIER_POD_MAX_BYTES,
    VerifierSpec,
    build_verifier_pod_manifest,
)
from tests.unit.test_147_helm_deployment import _render, _rendered_config
from tests.unit.test_265_k8s_verifier import CLAIM, NAMESPACE, _FakeCoreV1, _manifest, _run
from tests.unit.test_265_provenance_k8s import ACTIVATE, freeze, logs, setup, summary

FIXED_ID = 10001
STAT_REFUSAL_LIMIT = 3
POD_SPEC_LIMIT = VERIFIER_POD_MAX_BYTES


@pytest.mark.asyncio
async def test_large_verify_output_keeps_the_final_verdict(tmp_path: Path) -> None:
    cache = tmp_path / "sym"
    cache.mkdir()
    (cache / "activate.sh").write_text(ACTIVATE)
    spec = VerifierSpec(image="trusted", cache_claim=CLAIM, cache_subpath="sym", mount_path=str(cache),
                        verify_text=f'head -c {MAX_RESULT_BYTES + 1} /dev/zero; printf "\\n"\n',
                        project_files={}, activate_text=ACTIVATE)
    container = build_verifier_pod_manifest(spec, name="test", namespace=NAMESPACE)["spec"]["containers"][0]
    env = {**os.environ, **{item["name"]: item["value"] for item in container["env"]},
           "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"]}
    result = subprocess.run(container["command"], env=env, text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    output = result.stdout
    assert len(output) > MAX_RESULT_BYTES

    class LogCore(_FakeCoreV1):
        def read_namespaced_pod_log(self, **kwargs: Any) -> str:
            assert "tail_lines" in kwargs
            return output[-kwargs["limit_bytes"]:]

    core = LogCore(["Succeeded"])
    ok, detail = await _run(KubernetesRuntime(namespace=NAMESPACE, cache_claim=CLAIM, core_v1=core))
    assert ok
    assert len(detail.encode()) <= VERIFIER_LOG_MAX_BYTES
    assert "COORDINARE_PROVENANCE=" not in detail


def test_summary_must_be_the_final_nonempty_line() -> None:
    with pytest.raises(ProvenanceTransientError):
        parse_summary(logs(summary()) + "output after sentinel\n", ACTIVATE, DEFAULT_LIMITS)


@pytest.mark.asyncio
async def test_missing_sentinel_during_recheck_is_transient(tmp_path: Path) -> None:
    service, ec, state, daemon, performer, _runtime = setup(tmp_path, [(True, logs(summary())), (True, "no sentinel")])
    await freeze(service, state, daemon, performer)
    check = await service.run_full_pass("sym", ec)
    assert check is not None and check.transient
    assert "sym" not in service._drift
    assert ec.bootstrap_attempts == 0


@pytest.mark.asyncio
async def test_verifier_uses_runtime_registry_credentials() -> None:
    core = _FakeCoreV1(["Succeeded"])
    runtime = KubernetesRuntime(namespace=NAMESPACE, cache_claim=CLAIM, core_v1=core,
                                image_pull_secrets=["private-registry"])
    await _run(runtime)
    assert core.created[0]["body"]["spec"]["imagePullSecrets"] == [{"name": "private-registry"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["ErrImagePull", "ImagePullBackOff", "InvalidImageName", "CreateContainerConfigError"])
async def test_container_startup_errors_fail_fast(reason: str) -> None:
    class WaitingCore(_FakeCoreV1):
        def read_namespaced_pod(self, name: str, namespace: str) -> Any:
            if name in self.deleted:
                raise ApiException(status=404)
            if self.phases.pop() == "Pending":
                waiting = SimpleNamespace(reason=reason)
                return SimpleNamespace(status=SimpleNamespace(
                    phase="Pending", container_statuses=[SimpleNamespace(state=SimpleNamespace(waiting=waiting))]))
            raise AssertionError("startup error must stop polling immediately")

    core = WaitingCore(["Pending"])
    ok, detail = await _run(KubernetesRuntime(namespace=NAMESPACE, cache_claim=CLAIM, core_v1=core))
    assert ok is None
    assert reason in detail
    assert len(core.deleted) == 1


@pytest.mark.parametrize(("field", "expected"), [
    ("runAsNonRoot", True), ("runAsUser", FIXED_ID), ("runAsGroup", FIXED_ID),
    ("seccompProfile", {"type": "RuntimeDefault"}),
])
def test_pod_security_context(field: str, expected: Any) -> None:
    assert _manifest()["spec"]["securityContext"][field] == expected


@pytest.mark.parametrize(("field", "expected"), [
    ("allowPrivilegeEscalation", False), ("capabilities", {"drop": ["ALL"]}),
    ("readOnlyRootFilesystem", True),
])
def test_container_security_context(field: str, expected: Any) -> None:
    assert _manifest()["spec"]["containers"][0]["securityContext"][field] == expected


def test_service_links_are_disabled() -> None:
    assert _manifest()["spec"]["enableServiceLinks"] is False


def test_tmp_and_home_use_writable_scratch() -> None:
    pod = _manifest()["spec"]
    container = pod["containers"][0]
    scratch = next(m for m in container["volumeMounts"] if m["mountPath"] == "/tmp")
    assert {"name": scratch["name"], "emptyDir": {}} in pod["volumes"]
    env = {item["name"]: item["value"] for item in container["env"]}
    assert env["HOME"] == "/tmp"


@pytest.mark.asyncio
async def test_initial_and_recheck_images_are_daemon_pinned(tmp_path: Path) -> None:
    service, ec, state, daemon, performer, runtime = setup(tmp_path, [(True, logs(summary())), (True, logs(summary()))])
    state["coordinare_config"].global_config.kubernetes_verifier_image = "trusted/verifier:pinned"
    performer._config.image = "symphony/untrusted:latest"
    await freeze(service, state, daemon, performer)
    await service.run_full_pass("sym", ec)
    assert [spec.image for spec in runtime.specs] == ["trusted/verifier:pinned"] * 3


def test_verifier_image_defaults_to_global_performer_image() -> None:
    cfg = ProjectConfiguration(github_org="org", project_name="repo", human_reviewers=["reviewer"], github_token="token",
                               agent_transport="kubernetes", performer_image="trusted/global:release")
    assert cfg.kubernetes_verifier_image == "trusted/global:release"


@pytest.mark.parametrize("image", ["", "bad image", "\nimage"])
def test_verifier_image_rejects_invalid_values(image: str) -> None:
    with pytest.raises(ValidationError, match="kubernetes_verifier_image"):
        ProjectConfiguration(github_org="org", project_name="repo", human_reviewers=["reviewer"], github_token="token",
                             kubernetes_verifier_image=image)


def test_chart_preserves_daemon_verifier_image() -> None:
    config = _rendered_config(_render(config={"kubernetes_verifier_image": "trusted/verifier:release"}))
    assert config["kubernetes_verifier_image"] == "trusted/verifier:release"


def test_serialized_pod_has_an_aggregate_bound() -> None:
    # The final aggregate cap includes metadata beyond bounded script inputs.
    with pytest.raises(ValueError, match=r"pod.*bound"):
        _manifest(image="x" * POD_SPEC_LIMIT)


@pytest.mark.asyncio
async def test_oversized_pod_is_infrastructure_without_creation() -> None:
    core = _FakeCoreV1(["Succeeded"])
    runtime = KubernetesRuntime(namespace=NAMESPACE, cache_claim=CLAIM, core_v1=core)
    spec = VerifierSpec(image="trusted", cache_claim=CLAIM, cache_subpath="sym", mount_path="/devenv/sym",
                        verify_text="x" * POD_SPEC_LIMIT, project_files={})
    ok, detail = await runtime.run_verifier(slug="sym", spec=spec)
    assert ok is None
    assert "bound" in detail
    assert core.created == []


@pytest.mark.asyncio
async def test_repeated_stat_refusals_eventually_spend_bootstrap_budget(tmp_path: Path) -> None:
    results = [(True, logs(summary()))] * (STAT_REFUSAL_LIMIT + 2)
    service, ec, state, daemon, performer, _runtime = setup(tmp_path, results)
    await freeze(service, state, daemon, performer)
    for _ in range(STAT_REFUSAL_LIMIT):
        await service.recheck_consumer_provenance("sym", state)
        assert ec.bootstrap_attempts == 0
    await service.recheck_consumer_provenance("sym", state)
    assert ec.bootstrap_attempts == 1
    assert ec.runtime_health_failed
    assert "stat" in ec.last_bootstrap_error
    # Duplicate signals while repair is requested cannot create more pods or charge twice.
    await service.recheck_consumer_provenance("sym", state)
    assert ec.bootstrap_attempts == 1


def test_chart_defaults_verifier_image_from_daemon_performer_image() -> None:
    config = _rendered_config(_render(config={"performer_image": "trusted/global:release"}))
    assert config["kubernetes_verifier_image"] == "trusted/global:release"


def test_kubernetes_without_any_valid_daemon_image_refuses_configuration() -> None:
    with pytest.raises(ValidationError, match="kubernetes_verifier_image"):
        ProjectConfiguration(github_org="org", human_reviewers=["reviewer"], github_token="token",
                             agent_transport="kubernetes", performer_image="")


@pytest.mark.asyncio
async def test_walker_error_sentinel_is_transient_in_recheck(tmp_path: Path) -> None:
    service, ec, state, daemon, performer, _runtime = setup(tmp_path, [
        (True, logs(summary())), (True, logs({"error": "RecursionError", "transient": True})),
    ])
    await freeze(service, state, daemon, performer)
    check = await service.run_full_pass("sym", ec)
    assert check is not None and check.transient
    assert "sym" not in service._drift


@pytest.mark.asyncio
async def test_stat_refusal_counter_survives_restart(tmp_path: Path) -> None:
    service, ec, state, daemon, performer, _runtime = setup(tmp_path, [(True, logs(summary()))] * 2)
    await freeze(service, state, daemon, performer)
    await service.recheck_consumer_provenance("sym", state)
    persisted = _persist_env_cache(state["env_cache"])["sym"]
    ec.consumer_stat_refusals = 0
    _rehydrate_env_cache(state, SimpleNamespace(env_cache={"sym": persisted}))
    assert ec.consumer_stat_refusals == 1


@pytest.mark.asyncio
async def test_failed_walk_without_sentinel_is_infrastructure(tmp_path: Path) -> None:
    service, ec, state, daemon, performer, _runtime = setup(tmp_path, [(True, logs(summary())), (False, "walk crashed")])
    await freeze(service, state, daemon, performer)
    check = await service.run_full_pass("sym", ec)
    assert check is not None and check.transient
    assert "sym" not in service._drift


@pytest.mark.asyncio
async def test_transient_rechecks_do_not_consume_stat_refusal_allowance(tmp_path: Path) -> None:
    results = [(True, logs(summary()))] + [(True, "missing sentinel")] * (STAT_REFUSAL_LIMIT + 1)
    service, ec, state, daemon, performer, _runtime = setup(tmp_path, results)
    await freeze(service, state, daemon, performer)
    for _ in range(STAT_REFUSAL_LIMIT + 1):
        await service.recheck_consumer_provenance("sym", state)
    assert ec.consumer_stat_refusals == 0
    assert ec.bootstrap_attempts == 0
    assert not ec.runtime_health_failed


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "expected"), [("pr_opened", 0), ("working", 2), ("error", 2)])
async def test_only_successful_consumer_ends_the_stat_refusal_sequence(
    tmp_path: Path, status: str, expected: int,
) -> None:
    _service, ec, state, _daemon, _performer, _runtime = setup(tmp_path, [])
    ec.consumer_stat_refusals = 2
    state["current_symphony"] = "sym"
    ctx = _BodyCtx(card_id="card", stage="implementing", status={"status": status})
    await phase_651(state, ctx)
    assert ec.consumer_stat_refusals == expected

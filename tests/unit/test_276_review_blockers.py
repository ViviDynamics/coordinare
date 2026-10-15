"""Regression tests for verifier restart safety and terminal refusals."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from coordinare_service_inference.cache_provenance import MAX_RESULT_BYTES, MAX_SCRIPT_BYTES
from kubernetes.client.rest import ApiException

from coordinare.models.env_manifest import EnvManifest, ManifestItem
from coordinare.services import kubernetes_verifier
from coordinare.services.activity_log import ActivityLog
from coordinare.services.env_manifest import render_verify_sh
from coordinare.services.env_provenance import DEFAULT_LIMITS, ProvenanceTransientError
from coordinare.services.kubernetes_provenance import parse_summary
from coordinare.services.kubernetes_runtime import (
    MANAGED_BY_LABEL,
    MANAGED_BY_VALUE,
    OWNER_LABEL,
    KubernetesRuntime,
)
from coordinare.services.kubernetes_verifier import (
    VERIFIER_LOG_MAX_BYTES,
    VERIFIER_LOG_TAIL_CHARS,
    VERIFIER_POD_MAX_BYTES,
    VERIFIER_ROLE,
    VERIFIER_ROLE_LABEL,
    VerifierConfigurationError,
)
from coordinare.services.reconciliation import run_kubernetes_orphan_sweep
from tests.unit.test_265_k8s_verifier import (
    CLAIM,
    NAMESPACE,
    _FakeCoreV1,
    _manifest,
    _run,
    _run_in_pod,
)
from tests.unit.test_265_provenance_k8s import ACTIVATE, freeze, logs, setup, summary

OWNER = "this-deployment"
OTHER_NAMESPACE = "another-namespace"
WALK_EXIT_CODE = 23
DIAGNOSTIC_REPETITIONS = VERIFIER_LOG_TAIL_CHARS * 2
SHELL_EXECUTABLE_MODE = 0o755
CLASSIC_TOKEN_CHARS = 36
TOKEN = "ghp_" + "s" * CLASSIC_TOKEN_CHARS
NEAR_LIMIT_DEBS = 10_000


class RestartCluster:
    """Keep running pods across runtime instances, while recording API scope."""

    def __init__(self, labels: dict[str, str]) -> None:
        self.pods = {
            "verifier": (NAMESPACE, labels),
            "controller": (NAMESPACE, {MANAGED_BY_LABEL: MANAGED_BY_VALUE, OWNER_LABEL: OWNER}),
            "foreign-verifier": (NAMESPACE, {**labels, OWNER_LABEL: "another-deployment"}),
            "legacy-verifier": (NAMESPACE, {key: value for key, value in labels.items() if key != OWNER_LABEL}),
            "unmanaged-verifier": (NAMESPACE, {key: value for key, value in labels.items() if key != MANAGED_BY_LABEL}),
            "foreign-namespace": (OTHER_NAMESPACE, labels),
        }
        self.queries: list[dict[str, str]] = []
        self.deleted: list[str] = []

    def list_namespaced_pod(self, **kwargs: str) -> Any:
        self.queries.append(kwargs)
        # Deliberately return nonmatching labels to also exercise defensive checks.
        return SimpleNamespace(items=[
            SimpleNamespace(metadata=SimpleNamespace(name=name, labels=labels), status=SimpleNamespace(phase="Running"))
            for name, (namespace, labels) in self.pods.items() if namespace == kwargs["namespace"]
        ])

    def delete_namespaced_pod(self, name: str, namespace: str, **kwargs: Any) -> None:
        assert self.pods[name][0] == namespace
        del self.pods[name]
        self.deleted.append(name)

    def read_namespaced_pod(self, name: str, namespace: str) -> Any:
        if name not in self.pods:
            raise ApiException(status=404)
        return SimpleNamespace(status=SimpleNamespace(phase="Running"))


@pytest.mark.asyncio
async def test_verifier_creation_labels_deployment_owner() -> None:
    core = _FakeCoreV1(["Succeeded"])
    await _run(KubernetesRuntime(namespace=NAMESPACE, cache_claim=CLAIM, owner=OWNER, core_v1=core))
    labels = core.created[0]["body"]["metadata"]["labels"]
    assert labels[OWNER_LABEL] == OWNER
    assert labels[MANAGED_BY_LABEL] == MANAGED_BY_VALUE
    assert labels[VERIFIER_ROLE_LABEL] == VERIFIER_ROLE


@pytest.mark.asyncio
async def test_verifier_owner_metadata_is_included_in_pod_size_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = _manifest()
    monkeypatch.setattr("coordinare.services.kubernetes_runtime.verifier_pod_name", lambda slug: manifest["metadata"]["name"])
    monkeypatch.setattr(kubernetes_verifier, "VERIFIER_POD_MAX_BYTES", len(json.dumps(manifest).encode()))
    core = _FakeCoreV1(["Succeeded"])
    runtime = KubernetesRuntime(namespace=NAMESPACE, cache_claim=CLAIM, owner=OWNER, core_v1=core)
    with pytest.raises(VerifierConfigurationError, match="byte bound"):
        await _run(runtime)
    assert core.created == []


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", [OWNER, None])
async def test_restart_sweeps_only_owned_verifiers_in_performer_namespace(owner: str | None) -> None:
    labels = {MANAGED_BY_LABEL: MANAGED_BY_VALUE, VERIFIER_ROLE_LABEL: VERIFIER_ROLE, OWNER_LABEL: OWNER}
    cluster = RestartCluster(labels)
    runtime = KubernetesRuntime(namespace=NAMESPACE, cache_claim=CLAIM, owner=owner, core_v1=cluster)
    state: Any = {"active_sessions": {}, "performer_services_by_id": {"bootstrap": SimpleNamespace(_runtime=runtime)}}
    swept = await run_kubernetes_orphan_sweep(state)
    assert swept == (["verifier"] if owner else [])
    assert cluster.deleted == swept
    if owner:
        assert cluster.queries == [{
            "namespace": NAMESPACE,
            "label_selector": f"{MANAGED_BY_LABEL}={MANAGED_BY_VALUE},{VERIFIER_ROLE_LABEL}={VERIFIER_ROLE},{OWNER_LABEL}={OWNER}",
        }]
    else:
        assert cluster.queries == []


@pytest.mark.asyncio
async def test_verifier_startup_sweep_survives_list_failure() -> None:
    api = SimpleNamespace(list_namespaced_pod=lambda **kwargs: (_ for _ in ()).throw(OSError("unavailable")))
    runtime = KubernetesRuntime(namespace=NAMESPACE, owner=OWNER, core_v1=api)
    state: Any = {"performer_services_by_id": {"bootstrap": SimpleNamespace(_runtime=runtime)}}
    assert await run_kubernetes_orphan_sweep(state) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal_stage", ["verify", "walk"])
async def test_configuration_refusal_notifies_and_records_exhaustion_once(tmp_path: Path, refusal_stage: str) -> None:
    service, ec, state, daemon, performer, runtime = setup(tmp_path, [])
    notify = SimpleNamespace(dispatch=AsyncMock())
    activity = ActivityLog()
    state.update(notification_service=notify, activity_log=activity)
    daemon._state.update(state)
    if refusal_stage == "verify":
        state["coordinare_config"].global_config.kubernetes_verifier_image = "bad image"
    else:
        runtime.results = [(True, "verify passed")]
        ec.rendered_activate_sh = "x" * VERIFIER_POD_MAX_BYTES
        performer._runtime = KubernetesRuntime(namespace=NAMESPACE, cache_claim=CLAIM, core_v1=_FakeCoreV1(["Succeeded"]))
    ok, reason, infra = await daemon._verify_bootstrap_in_clean_context("sym", performer, "job")
    assert not ok and not infra
    assert reason is not None and "configuration" in reason
    service.on_bootstrap_complete("sym", False, state, error=reason)
    daemon._verifier_configuration_refusal(ec, reason)
    await asyncio.gather(*service._notify_tasks)
    assert notify.dispatch.await_count == 1
    assert "configuration" in notify.dispatch.call_args.args[0].payload["last_error"]
    assert "configuration" in notify.dispatch.call_args.args[0].payload["summary"]
    entries = [entry for entry in activity.snapshot() if entry["activity_type"] == "env_bootstrap_exhausted"]
    assert len(entries) == 1
    assert "configuration" in entries[0]["text"]
    assert ec.bootstrap_exhausted


@pytest.mark.asyncio
async def test_consumer_configuration_refusal_is_terminal_without_regeneration(tmp_path: Path) -> None:
    service, ec, state, daemon, performer, _runtime = setup(tmp_path, [(True, logs(summary()))])
    await freeze(service, state, daemon, performer)
    notify = SimpleNamespace(dispatch=AsyncMock())
    activity = ActivityLog()
    state.update(notification_service=notify, activity_log=activity)
    state["coordinare_config"].global_config.kubernetes_verifier_image = "bad image"
    attempts_before = ec.bootstrap_attempts
    await service.recheck_consumer_provenance("sym", state)
    await service.recheck_consumer_provenance("sym", state)
    await asyncio.gather(*service._notify_tasks)
    assert ec.bootstrap_exhausted
    assert not ec.runtime_health_failed
    assert not ec.runtime_repair_budget
    assert not ec.cache_dir_ready
    assert ec.bootstrap_attempts == attempts_before
    assert "configuration" in ec.last_bootstrap_error
    assert notify.dispatch.await_count == 1
    assert len(activity.snapshot()) == 1
    assert "sym" not in service._drift


def _walker_output(tmp_path: Path, stdout: str, stderr: str, exit_code: int) -> Any:
    shim = tmp_path / "bin"
    shim.mkdir()
    output = tmp_path / "walker.stdout"
    output.write_text(stdout)
    code = (
        "import pathlib, sys; "
        f"sys.stderr.write({stderr!r}); "
        f"sys.stdout.write(pathlib.Path({str(output)!r}).read_text()); sys.exit({exit_code})"
    )
    python = shim / "python3"
    python.write_text(
        '#!/bin/bash\nif [ "$1" = "-P" ]; then\n'
        f"  exec {shlex.quote(sys.executable)} -c {shlex.quote(code)}\nfi\n"
        f'exec {shlex.quote(sys.executable)} "$@"\n',
    )
    python.chmod(SHELL_EXECUTABLE_MODE)
    return _run_in_pod(_manifest(walk_only=True, activate_text=ACTIVATE), tmp_path, {
        "HOME": str(tmp_path), "PATH": str(shim) + os.pathsep + os.environ["PATH"],
    })


def test_walk_diagnostic_tail_is_bounded_redacted_and_precedes_final_sentinel(tmp_path: Path) -> None:
    output = logs(summary())
    diagnostic = "old noise\n" * DIAGNOSTIC_REPETITIONS + f"ImportError: cache module token={TOKEN}\n"
    result = _walker_output(tmp_path, output, diagnostic, 0)
    assert result.returncode == 0
    assert result.stderr == ""
    assert result.stdout.endswith(output)
    prefix = result.stdout.removesuffix(output)
    assert prefix.startswith("COORDINARE_WALK_DIAGNOSTIC=")
    assert len(prefix.encode()) <= VERIFIER_LOG_TAIL_CHARS
    assert "ImportError" in prefix and TOKEN not in prefix
    assert parse_summary(result.stdout, ACTIVATE, DEFAULT_LIMITS) == summary()


def test_walk_diagnostics_leave_room_for_a_result_at_the_byte_limit(tmp_path: Path) -> None:
    inputs = {**summary()["inputs"], "debs": {f"debs/{index:05d}.deb": "c" * 64 for index in range(NEAR_LIMIT_DEBS)},
              "services_start_sh": ""}
    doc = summary(inputs=inputs, entry_count=NEAR_LIMIT_DEBS)
    spare = MAX_RESULT_BYTES - len(json.dumps(doc).encode())
    assert 0 < spare <= MAX_SCRIPT_BYTES
    inputs["services_start_sh"] = "s" * spare
    assert len(json.dumps(doc).encode()) == MAX_RESULT_BYTES
    result = _walker_output(tmp_path, logs(doc), "old noise\n" * DIAGNOSTIC_REPETITIONS, 0)
    assert result.returncode == 0
    assert len(result.stdout.encode()) <= VERIFIER_LOG_MAX_BYTES
    bounded = result.stdout.encode()[:VERIFIER_LOG_MAX_BYTES].decode()
    assert parse_summary(bounded, ACTIVATE, DEFAULT_LIMITS) == doc


@pytest.mark.asyncio
async def test_missing_sentinel_infrastructure_reason_keeps_walk_diagnostic(tmp_path: Path) -> None:
    result = _walker_output(tmp_path, "", "PermissionError: cannot import cache walker\n", WALK_EXIT_CODE)
    assert result.returncode == WALK_EXIT_CODE
    _service, _ec, _state, daemon, performer, runtime = setup(tmp_path, [(False, result.stdout)])
    ok, reason, infra = await daemon._verify_bootstrap_in_clean_context("sym", performer, "job")
    assert not ok and infra
    assert "PermissionError" in reason
    assert "cannot import cache walker" in reason
    assert len(runtime.specs) == 2


@pytest.mark.parametrize("output", [logs(summary()) + "diagnostic after sentinel\n", logs(summary()) * 2])
def test_diagnostics_do_not_relax_final_unique_sentinel_rule(output: str) -> None:
    with pytest.raises((ValueError, ProvenanceTransientError)):
        parse_summary(output, ACTIVATE, DEFAULT_LIMITS)


def test_verify_wrapper_rejects_failing_pipeline(tmp_path: Path) -> None:
    result = _run_in_pod(_manifest(verify_text="false | cat\n"), tmp_path, {"HOME": str(tmp_path)})
    assert result.returncode != 0


@pytest.mark.parametrize(("check", "expected"), [("false | cat", False), ("printf ok | cat", True)])
def test_rendered_verify_checks_record_pipeline_status(tmp_path: Path, check: str, expected: bool) -> None:
    manifest = EnvManifest(symphony_name="sym", items=[ManifestItem(name="custom", kind="runtime", source="README.md", check=check)])
    verify = render_verify_sh(manifest, cache_mount_path="/devenv/sym")
    result = _run_in_pod(_manifest(verify_text=verify), tmp_path, {"HOME": str(tmp_path)})
    assert (result.returncode == 0) is expected


@pytest.mark.parametrize("exit_code", [0, WALK_EXIT_CODE])
def test_rendered_node_version_checks_producer_status(tmp_path: Path, exit_code: int) -> None:
    shim = tmp_path / "bin"
    shim.mkdir()
    node = shim / "node"
    node.write_text(f"#!/bin/bash\nprintf 'v20.11.0\\n'\nexit {exit_code}\n")
    node.chmod(SHELL_EXECUTABLE_MODE)
    manifest = EnvManifest(symphony_name="sym", items=[ManifestItem(name="node", kind="runtime", version="20.11.0", source=".nvmrc")])
    verify = render_verify_sh(manifest, cache_mount_path="/devenv/sym")
    result = _run_in_pod(_manifest(verify_text=verify), tmp_path, {
        "HOME": str(tmp_path), "PATH": str(shim) + os.pathsep + os.environ["PATH"],
    })
    assert (result.returncode == 0) is (exit_code == 0)

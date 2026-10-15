"""Regression coverage for the Kubernetes verifier security follow ups."""

from __future__ import annotations

import asyncio
import os
import shlex
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from coordinare_service_inference.cache_provenance import MAX_RESULT_BYTES, MAX_SCRIPT_BYTES

from coordinare.daemon import _persist_env_cache, _rehydrate_env_cache
from coordinare.services.env_provenance import DEFAULT_LIMITS
from coordinare.services.kubernetes_provenance import parse_summary
from coordinare.services.kubernetes_runtime import KubernetesRuntime
from coordinare.services.kubernetes_verifier import (
    PROJECT_FILES_MAX_BYTES,
    VERIFIER_LOG_TAIL_CHARS,
    VERIFIER_POD_MAX_BYTES,
    VERIFY_TEXT_MAX_BYTES,
    VerifierSpec,
)
from tests.unit.test_265_k8s_verifier import (
    CLAIM,
    MOUNT,
    NAMESPACE,
    _FakeCoreV1,
    _manifest,
    _run,
    _run_in_pod,
)
from tests.unit.test_265_provenance_k8s import ACTIVATE, freeze, logs, setup, summary

DEB_COUNT = 5000
DIAGNOSTIC_BYTES = MAX_RESULT_BYTES * 2


class InvalidImageCore(_FakeCoreV1):
    def read_namespaced_pod(self, name: str, namespace: str) -> Any:
        pod = super().read_namespaced_pod(name, namespace)
        pod.status.container_statuses = [SimpleNamespace(
            state=SimpleNamespace(waiting=SimpleNamespace(reason="InvalidImageName")),
        )]
        return pod


@pytest.mark.asyncio
@pytest.mark.parametrize(("phase", "expected"), [("Succeeded", True), ("Failed", False)])
async def test_verify_verdict_survives_unavailable_logs(phase: str, expected: bool) -> None:
    class UnreadableLogs(_FakeCoreV1):
        def read_namespaced_pod_log(self, **kwargs: Any) -> str:
            raise OSError("log service unavailable")

    core = UnreadableLogs([phase])
    ok, _detail = await _run(KubernetesRuntime(namespace=NAMESPACE, cache_claim=CLAIM, core_v1=core))
    assert ok is expected
    assert core.deleted == [core.created[0]["body"]["metadata"]["name"]]


@pytest.mark.asyncio
async def test_successful_verify_does_not_read_logs() -> None:
    class UnexpectedLogs(_FakeCoreV1):
        def read_namespaced_pod_log(self, **kwargs: Any) -> str:
            pytest.fail("a successful verify must not depend on the log service")

    core = UnexpectedLogs(["Succeeded"])
    ok, _detail = await _run(KubernetesRuntime(namespace=NAMESPACE, cache_claim=CLAIM, core_v1=core))
    assert ok is True
    assert len(core.deleted) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"image": "bad image"},
    {"verify_text": "x" * (VERIFY_TEXT_MAX_BYTES + 1)},
    {"project_files": {"Gemfile": "x" * PROJECT_FILES_MAX_BYTES}},
    {"verify_text": "\ud800"},
    {"image": "x" * VERIFIER_POD_MAX_BYTES},
])
async def test_invalid_pod_inputs_raise_configuration_refusals_without_creation(changes: dict[str, Any]) -> None:
    core = _FakeCoreV1(["Succeeded"])
    runtime = KubernetesRuntime(namespace=NAMESPACE, cache_claim=CLAIM, core_v1=core)
    spec = VerifierSpec(image="trusted", cache_claim=CLAIM, cache_subpath="sym", mount_path=MOUNT,
                        verify_text="exit 0\n", project_files={})
    with pytest.raises(ValueError, match=r"verifier.*(image|bound|UTF|utf|encode)"):
        await runtime.run_verifier(slug="sym", spec=replace(spec, **changes))
    assert core.created == []


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", ["image", "missing_image", "verify_size", "project_size", "encoding"])
async def test_configuration_refusals_stop_bootstrap_retries(tmp_path: Path, refusal: str) -> None:
    service, ec, state, daemon, performer, _runtime = setup(tmp_path, [])
    core = _FakeCoreV1(["Succeeded"])
    performer._runtime = KubernetesRuntime(namespace=NAMESPACE, cache_claim=CLAIM, core_v1=core)
    expected = "bound"
    if refusal in ("image", "missing_image"):
        cfg = state["coordinare_config"].global_config
        cfg.kubernetes_verifier_image = "bad image" if refusal == "image" else None
        cfg.performer_image = None
        expected = "image"
    elif refusal == "verify_size":
        ec.rendered_verify_sh = "x" * (VERIFY_TEXT_MAX_BYTES + 1)
    elif refusal == "project_size":
        state["github_service"].get_file_content.return_value = "x" * PROJECT_FILES_MAX_BYTES
    else:
        ec.rendered_verify_sh = "\ud800"
        expected = "encode"

    ok, reason, infra = await daemon._verify_bootstrap_in_clean_context("sym", performer, "job")
    assert not ok
    assert not infra
    assert reason is not None and "configuration" in reason and expected in reason
    service.on_bootstrap_complete("sym", ok, state, error=reason, infra_failure=infra)
    assert ec.bootstrap_exhausted
    assert ec.infra_failures == 0
    assert not ec.cache_dir_ready
    assert ec.verifier_provenance is None
    assert core.created == []


@pytest.mark.asyncio
async def test_transient_verify_failure_keeps_the_retry_path(tmp_path: Path) -> None:
    _service, ec, _state, daemon, performer, _runtime = setup(tmp_path, [])
    performer._runtime = KubernetesRuntime(namespace=NAMESPACE, cache_claim=CLAIM,
                                           core_v1=_FakeCoreV1(["Succeeded"], fail_create=True))
    ok, reason, infra = await daemon._verify_bootstrap_in_clean_context("sym", performer, "job")
    assert not ok
    assert infra
    assert reason is not None and "could not be created" in reason
    assert not ec.bootstrap_exhausted


@pytest.mark.asyncio
async def test_successful_walk_still_requires_readable_result_logs(tmp_path: Path) -> None:
    class UnreadableWalk(_FakeCoreV1):
        def read_namespaced_pod_log(self, **kwargs: Any) -> str:
            raise OSError("walk result unavailable")

    _service, ec, _state, daemon, performer, _runtime = setup(tmp_path, [])
    core = UnreadableWalk(["Succeeded"])
    performer._runtime = KubernetesRuntime(namespace=NAMESPACE, cache_claim=CLAIM, core_v1=core)
    ok, _reason, infra = await daemon._verify_bootstrap_in_clean_context("sym", performer, "job")
    assert not ok
    assert infra
    assert ec.verifier_provenance is None
    assert len(core.created) == len(core.deleted) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", ["image", "pod_size"])
async def test_recheck_configuration_refusal_does_not_record_cache_drift(tmp_path: Path, refusal: str) -> None:
    service, ec, state, daemon, performer, _runtime = setup(tmp_path, [(True, logs(summary()))])
    await freeze(service, state, daemon, performer)
    core = _FakeCoreV1(["Succeeded"])
    performer._runtime = KubernetesRuntime(namespace=NAMESPACE, cache_claim=CLAIM, core_v1=core)
    if refusal == "image":
        state["coordinare_config"].global_config.kubernetes_verifier_image = "bad image"
    else:
        ec.rendered_activate_sh = "x" * VERIFIER_POD_MAX_BYTES
    check = await service.run_full_pass("sym", ec)
    assert check is not None and not check.ok and not check.transient
    assert "configuration" in check.reason
    assert "sym" not in service._drift
    assert ec.provenance_digest == summary()["content_digest"]
    assert core.created == []


@pytest.mark.asyncio
async def test_kubernetes_invalid_image_refusal_stops_retries_and_removes_pod(tmp_path: Path) -> None:
    _service, ec, _state, daemon, performer, _runtime = setup(tmp_path, [])
    core = InvalidImageCore(["Pending"])
    performer._runtime = KubernetesRuntime(namespace=NAMESPACE, cache_claim=CLAIM, core_v1=core)
    ok, reason, infra = await daemon._verify_bootstrap_in_clean_context("sym", performer, "job")
    assert not ok and not infra
    assert reason is not None and "configuration" in reason and "InvalidImageName" in reason
    assert ec.bootstrap_exhausted
    assert len(core.created) == len(core.deleted) == 1


@pytest.mark.asyncio
async def test_configuration_refusal_still_requires_confirmed_pod_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _service, ec, _state, daemon, performer, _runtime = setup(tmp_path, [])
    core = InvalidImageCore(["Pending"])
    runtime = KubernetesRuntime(namespace=NAMESPACE, cache_claim=CLAIM, core_v1=core)
    performer._runtime = runtime

    async def removal_unavailable(*args: Any, **kwargs: Any) -> bool:
        return False

    monkeypatch.setattr(runtime, "confirm_stopped", removal_unavailable)
    ok, reason, infra = await daemon._verify_bootstrap_in_clean_context("sym", performer, "job")
    assert not ok and infra
    assert reason is not None and "removed" in reason
    assert ec.verifier_provenance is None
    assert len(core.created) == 1


@pytest.mark.asyncio
async def test_walk_diagnostics_cannot_truncate_a_near_limit_result(tmp_path: Path) -> None:
    inputs = {**summary()["inputs"], "services_start_sh": "s" * MAX_SCRIPT_BYTES,
              "services_health_sh": "h" * MAX_SCRIPT_BYTES,
              "debs": {f"debs/{index:05d}.deb": "c" * 64 for index in range(DEB_COUNT)}}
    doc = summary(inputs=inputs, entry_count=DEB_COUNT)
    output = logs(doc)
    assert len(output.encode()) < MAX_RESULT_BYTES
    assert len(output.encode()) > MAX_RESULT_BYTES - MAX_SCRIPT_BYTES
    result_file = tmp_path / "result.jsonl"
    result_file.write_text(output)
    shim = tmp_path / "bin"
    shim.mkdir()
    code = (
        "import pathlib, sys; "
        f"sys.stderr.write('diagnostic' * {DIAGNOSTIC_BYTES}); "
        f"sys.stdout.write(pathlib.Path({str(result_file)!r}).read_text())"
    )
    python = shim / "python3"
    python.write_text(
        '#!/bin/bash\nif [ "$1" = "-P" ]; then\n'
        f"  exec {shlex.quote(sys.executable)} -c {shlex.quote(code)}\nfi\n"
        f'exec {shlex.quote(sys.executable)} "$@"\n',
    )
    python.chmod(0o755)
    pod = _manifest(walk_only=True, activate_text=ACTIVATE)
    result = _run_in_pod(pod, tmp_path, {"HOME": str(tmp_path), "PATH": str(shim) + os.pathsep + os.environ["PATH"]})
    assert result.returncode == 0, result.stderr

    class BoundedLogs(_FakeCoreV1):
        def read_namespaced_pod_log(self, **kwargs: Any) -> str:
            # Kubernetes combines both streams, and limit_bytes may cut a selected log range.
            combined = result.stderr + result.stdout
            return combined.encode()[:kwargs["limit_bytes"]].decode()

    core = BoundedLogs(["Succeeded"])
    runtime = KubernetesRuntime(namespace=NAMESPACE, cache_claim=CLAIM, core_v1=core)
    spec = VerifierSpec(image="trusted", cache_claim=CLAIM, cache_subpath="sym", mount_path=MOUNT,
                        verify_text="", project_files={}, activate_text=ACTIVATE, walk_only=True)
    ok, detail = await runtime.run_verifier(slug="sym", spec=spec)
    assert ok is True
    assert parse_summary(detail, ACTIVATE, DEFAULT_LIMITS) == doc
    assert result.stdout.endswith(output)
    assert len(result.stdout.removesuffix(output).encode()) <= VERIFIER_LOG_TAIL_CHARS
    assert (tmp_path / "walk.stderr").read_text().startswith("diagnostic")
    assert len(core.deleted) == 1


@pytest.mark.asyncio
async def test_restart_between_verify_and_walk_requires_fresh_verification_without_orphans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SequentialCore(_FakeCoreV1):
        def create_namespaced_pod(self, namespace: str, body: dict[str, Any]) -> None:
            assert len(self.created) == len(self.deleted), "the previous pod must be gone"
            super().create_namespaced_pod(namespace, body)

        def read_namespaced_pod_log(self, **kwargs: Any) -> str:
            return logs(summary())

    _service, ec, state, daemon, performer, _runtime = setup(tmp_path, [])
    ec.bootstrap_in_flight = True
    core = SequentialCore(["Succeeded"])
    performer._runtime = KubernetesRuntime(namespace=NAMESPACE, cache_claim=CLAIM, core_v1=core)
    snapshots: list[Any] = []

    async def restart_before_walk(*args: Any, **kwargs: Any) -> Any:
        assert len(core.created) == len(core.deleted) == 1
        snapshots.append(_persist_env_cache(state["env_cache"])["sym"])
        raise asyncio.CancelledError

    monkeypatch.setattr(daemon, "_verify_bootstrap_claim", restart_before_walk)
    with pytest.raises(asyncio.CancelledError):
        await daemon._verify_bootstrap_in_clean_context("sym", performer, "job")
    assert ec.verifier_provenance is None
    assert len(core.created) == len(core.deleted) == 1

    service, restored, fresh_state, fresh_daemon, fresh_performer, _runtime = setup(tmp_path, [])
    _rehydrate_env_cache(fresh_state, SimpleNamespace(env_cache={"sym": snapshots[0]}))
    assert not restored.bootstrap_in_flight
    assert not restored.cache_dir_ready
    assert restored.last_bootstrap_succeeded is not True
    assert restored.verifier_provenance is None
    assert restored.provenance_digest is None
    assert restored.provenance_inputs is None
    ok, reason = await service.freeze_provenance("sym", fresh_state)
    assert not ok and reason is not None
    fresh_performer._runtime = KubernetesRuntime(namespace=NAMESPACE, cache_claim=CLAIM, core_v1=core)
    await freeze(service, fresh_state, fresh_daemon, fresh_performer)
    assert restored.cache_dir_ready
    assert restored.provenance_digest == summary()["content_digest"]
    assert len(core.created) == len(core.deleted)
    modes = []
    for pod in core.created:
        env = {entry["name"]: entry["value"] for entry in pod["body"]["spec"]["containers"][0]["env"]}
        modes.append(env["WALK_ONLY"])
    assert modes == ["0", "0", "1"]

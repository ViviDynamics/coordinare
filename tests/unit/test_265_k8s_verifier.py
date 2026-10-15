"""Spec 265: the clean-room verifier pod on Kubernetes.

The pod mounts the devenv claim read-only, with no checkout, no credentials and no stdin. Its setup resolves the bundle
path the way bundler will see it, so a cache installed against a vendored Gemfile at the cache root passes without a
reinstall.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from kubernetes.client.rest import ApiException

from coordinare.models.env_manifest import EnvManifest, ManifestItem
from coordinare.models.performer_endpoint import PerformerEndpointConfig
from coordinare.services import kubernetes_runtime
from coordinare.services.env_manifest import render_activate_sh, render_verify_sh
from coordinare.services.http_performer_service import HTTPPerformerService
from coordinare.services.kubernetes_runtime import KubernetesRuntime
from coordinare.services.kubernetes_verifier import (
    PROJECT_FILES_MAX_BYTES,
    VERIFIER_SCRIPT,
    VerifierSpec,
    build_verifier_pod_manifest,
    check_project_files,
    verifier_pod_name,
)

CLAIM = "coordinare-devenv-cache"
NAMESPACE = "coordinare-performers"
MOUNT = "/devenv/dettmore-platform-615891"
PROJECT = {"Gemfile": "source 'https://rubygems.org'\n", ".ruby-version": "3.4.2\n"}


def _manifest(**overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "image": "coordinare-performer:full",
        "cache_claim": CLAIM,
        "cache_subpath": "dettmore-platform-615891",
        "mount_path": MOUNT,
        "verify_text": "exit 0\n",
        "project_files": PROJECT,
    }
    fields.update(overrides)
    return build_verifier_pod_manifest(
        VerifierSpec(**fields), name="coordinare-verify-x-abc12345", namespace=NAMESPACE,
    )


def _container(manifest: dict[str, Any]) -> dict[str, Any]:
    return manifest["spec"]["containers"][0]


# --- pod spec ---------------------------------------------------------------------------------------------------------
def test_the_claim_is_mounted_read_only_at_the_consumer_path_from_the_symphony_subpath() -> None:
    manifest = _manifest()
    mount = _container(manifest)["volumeMounts"][0]
    assert mount == {
        "name": "devenv-cache", "mountPath": MOUNT, "subPath": "dettmore-platform-615891", "readOnly": True,
    }
    claim = manifest["spec"]["volumes"][0]["persistentVolumeClaim"]
    assert claim == {"claimName": CLAIM, "readOnly": True}


def test_the_pod_has_no_checkout_no_credentials_and_no_stdin() -> None:
    manifest = _manifest()
    spec = manifest["spec"]
    assert len(spec["volumes"]) == 2, "read-only claim and scratch only"
    assert spec["automountServiceAccountToken"] is False
    assert _container(manifest)["stdin"] is False
    env_names = {e["name"] for e in _container(manifest)["env"]}
    assert env_names == {"DEVENV", "HOME", "VERIFY_B64", "PROJECT_FILES_B64", "SETUP_PY", "PROVENANCE_CONFIG", "WALK_ONLY"}
    assert "GITHUB_TOKEN" not in env_names


def test_the_pod_has_a_deadline_limits_and_never_restarts() -> None:
    manifest = _manifest()
    assert manifest["spec"]["restartPolicy"] == "Never"
    assert manifest["spec"]["activeDeadlineSeconds"] > 0
    limits = _container(manifest)["resources"]["limits"]
    assert set(limits) == {"cpu", "memory"}


def test_the_verify_runs_with_stdin_closed_from_a_file() -> None:
    assert "bash \"$d/verify.sh\" </dev/null" in VERIFIER_SCRIPT


def test_the_pod_name_is_a_bounded_dns_label() -> None:
    name = verifier_pod_name("Dettmore_Platform " * 20)
    assert len(name) <= 63
    assert name == name.lower()
    assert name.startswith("coordinare-verify-")


# --- project files ----------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("bad", ["../escape", "/abs/Gemfile", "a//b", "a/../b", "", "a/./b"])
def test_a_project_path_that_could_leave_the_checkout_is_refused(bad: str) -> None:
    with pytest.raises(ValueError):
        check_project_files({bad: "x"})


def test_project_files_are_bounded_in_bytes() -> None:
    big = {"Gemfile": "é" * (PROJECT_FILES_MAX_BYTES // 2 + 1)}  # 2 bytes each: over the byte bound
    with pytest.raises(ValueError):
        check_project_files(big)


# --- the in-pod setup, run for real ----------------------------------------------------------------------------------
def _layout(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Garden's layout: a vendored Gemfile at the cache root, a relative BUNDLE_PATH, and a checkout elsewhere."""
    cache = tmp_path / "devenv" / "dettmore-platform-615891"
    (cache / "project" / "bundle").mkdir(parents=True)
    (cache / ".bundle").mkdir(parents=True)
    (cache / ".bundle" / "config").write_text('BUNDLE_PATH: "project/bundle"\n')
    (cache / "Gemfile").write_text("# vendored copy, made by the agent\n")
    checkout = tmp_path / "performer" / "repo"
    checkout.mkdir(parents=True)
    other = tmp_path / "elsewhere"
    other.mkdir()
    return cache, checkout, other


def _run_in_pod(manifest: dict[str, Any], cwd: Path, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Run the pod's command the way the container does: bash -c with the container's environment, from another directory."""
    env = {"PATH": os.environ["PATH"], "HOME": str(cwd)}
    env.update({e["name"]: e["value"] for e in _container(manifest)["env"]})
    env.update(extra_env or {})
    return subprocess.run(
        ["bash", "-c", _container(manifest)["command"][2]],
        env=env, cwd=cwd, capture_output=True, text=True, check=False, timeout=60,
    )


def test_the_setup_resolves_a_relative_bundle_path_against_the_cache_root(tmp_path: Path) -> None:
    """The vendored Gemfile sits at the cache root, so bundler's root is the cache: the relative path resolves there."""
    cache, _checkout, other = _layout(tmp_path)
    verify = (
        'echo "BUNDLE_PATH=$BUNDLE_PATH"; echo "BUNDLE_GEMFILE=$BUNDLE_GEMFILE"; echo "REPO=$COORDINARE_REPO_DIR"\n'
        '[ "$BUNDLE_PATH" = "' + str(cache / "project" / "bundle") + '" ] || exit 3\n'
        '[ -f "$COORDINARE_REPO_DIR/Gemfile" ] || exit 4\n'
        'exit 0\n'
    )
    manifest = _manifest(mount_path=str(cache), verify_text=verify, project_files=PROJECT)

    result = _run_in_pod(manifest, other)

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"BUNDLE_PATH={cache / 'project' / 'bundle'}" in result.stdout
    # The commit's Gemfile is the one bundler is given. The cache's vendored copy is not.
    assert "BUNDLE_GEMFILE=" in result.stdout
    assert str(cache) not in result.stdout.split("BUNDLE_GEMFILE=")[1].splitlines()[0]


def test_the_verifier_runs_from_its_own_checkout_not_the_callers_directory(tmp_path: Path) -> None:
    cache, _checkout, other = _layout(tmp_path)
    verify = 'pwd -P > "$HOME/where"\n[ -f ./Gemfile ] || exit 5\nexit 0\n'
    manifest = _manifest(mount_path=str(cache), verify_text=verify, project_files=PROJECT)

    result = _run_in_pod(manifest, other, extra_env={"HOME": str(other)})

    assert result.returncode == 0, result.stdout + result.stderr
    where = (other / "where").read_text().strip()
    assert Path(where).resolve() != other.resolve()
    assert Path(where).name == "repo"


def test_a_child_reading_stdin_cannot_swallow_the_verify(tmp_path: Path) -> None:
    cache, _checkout, other = _layout(tmp_path)
    verify = 'echo "FAIL: first"; FAILED=1\ncat >/dev/null\nif [ "$FAILED" -ne 0 ]; then exit 1; fi\nexit 0\n'
    manifest = _manifest(mount_path=str(cache), verify_text=verify, project_files=PROJECT)

    result = _run_in_pod(manifest, other)

    assert result.returncode == 1, result.stdout + result.stderr


def test_a_refused_project_path_stops_the_setup_before_the_verify(tmp_path: Path) -> None:
    cache, _checkout, other = _layout(tmp_path)
    bad = _manifest(mount_path=str(cache), verify_text="exit 0\n", project_files={"Gemfile": "x"})
    # Replace the project value the setup reads with a traversal, and check the setup refuses it.
    hostile = base64.b64encode(json.dumps({"../escape": "x"}).encode()).decode()
    result = _run_in_pod(bad, other, extra_env={"PROJECT_FILES_B64": hostile})

    assert result.returncode != 0
    assert "refused project path" in result.stderr


# --- the runner -------------------------------------------------------------------------------------------------------
class _FakeCoreV1:
    def __init__(self, phases: list[str], *, fail_create: bool = False, fail_read: bool = False) -> None:
        self.phases = list(phases)
        self.created: list[dict[str, Any]] = []
        self.deleted: list[str] = []
        self.fail_create = fail_create
        self.fail_read = fail_read

    def create_namespaced_pod(self, namespace: str, body: dict[str, Any]) -> None:
        if self.fail_create:
            raise RuntimeError("api down")
        self.created.append({"namespace": namespace, "body": body})

    def read_namespaced_pod(self, name: str, namespace: str) -> Any:
        if name in self.deleted:
            raise ApiException(status=404)
        if self.fail_read:
            raise RuntimeError("read down")
        phase = self.phases.pop(0) if len(self.phases) > 1 else self.phases[0]
        return SimpleNamespace(status=SimpleNamespace(phase=phase))

    def read_namespaced_pod_log(self, name: str, namespace: str, container: str, tail_lines: int, limit_bytes: int) -> str:
        assert container == "verifier"
        return "========= Bootstrap verification PASSED =========\n"

    def delete_namespaced_pod(self, name: str, namespace: str, grace_period_seconds: int) -> None:
        self.deleted.append(name)


def _runtime(core: _FakeCoreV1, *, claim: str | None = CLAIM) -> KubernetesRuntime:
    return KubernetesRuntime(namespace=NAMESPACE, cache_claim=claim, core_v1=core)


async def _run(runtime: KubernetesRuntime) -> tuple[bool | None, str]:
    spec = VerifierSpec(
        image="coordinare-performer:full", cache_claim="", cache_subpath="dettmore-platform-615891",
        mount_path=MOUNT, verify_text="exit 0\n", project_files=PROJECT,
    )
    return await runtime.run_verifier(slug="dettmore-platform-615891", spec=spec, poll_interval_s=0)


@pytest.mark.asyncio
async def test_a_succeeded_pod_is_a_pass_and_is_removed() -> None:
    core = _FakeCoreV1(["Pending", "Succeeded"])

    ok, detail = await _run(_runtime(core))

    assert ok is True
    assert "PASSED" in detail
    assert len(core.created) == 1
    assert core.created[0]["body"]["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"] == CLAIM
    assert core.deleted == [core.created[0]["body"]["metadata"]["name"]]


@pytest.mark.asyncio
async def test_a_failed_pod_is_a_fail_and_is_removed() -> None:
    core = _FakeCoreV1(["Running", "Failed"])

    ok, _detail = await _run(_runtime(core))

    assert ok is False
    assert len(core.deleted) == 1


@pytest.mark.asyncio
async def test_a_pod_past_the_deadline_is_transient_and_is_removed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kubernetes_runtime, "VERIFIER_DEADLINE_S", 0)
    core = _FakeCoreV1(["Running"])

    ok, detail = await _run(_runtime(core))

    assert ok is None
    assert "timed out" in detail
    assert len(core.deleted) == 1


@pytest.mark.asyncio
async def test_a_pod_that_cannot_be_created_is_infrastructure_not_a_verdict() -> None:
    core = _FakeCoreV1(["Succeeded"], fail_create=True)

    ok, detail = await _run(_runtime(core))

    assert ok is None
    assert "could not be created" in detail


@pytest.mark.asyncio
async def test_a_pod_that_cannot_be_read_is_infrastructure_and_is_still_removed() -> None:
    core = _FakeCoreV1(["Succeeded"], fail_read=True)

    ok, detail = await _run(_runtime(core))

    assert ok is None
    assert "could not be read" in detail
    assert len(core.deleted) == 1


@pytest.mark.asyncio
async def test_no_claim_means_no_verifier_pod_at_all() -> None:
    core = _FakeCoreV1(["Succeeded"])

    ok, detail = await _run(_runtime(core, claim=None))

    assert ok is None
    assert "claim" in detail
    assert core.created == []



# --- the bootstrap persona: the README reaches the agent, and the context says where its work is checked ------------
def _persona(runtime: object, *, verify_provided: bool = True) -> str:
    config = PerformerEndpointConfig.model_validate({
        "id": "perf-e1", "mode": "ephemeral", "roles": ["env_bootstrap"], "image": "coordinare-performer:full",
    })
    svc = HTTPPerformerService(config, runtime=runtime)
    ctx: dict[str, Any] = {
        "job_type": "env_bootstrap",
        "symphony_name": "dettmore",
        "symphony_org": "ViviDynamics",
        "symphony_repo": "dettmore-platform",
        "env_spec_files": ["README.md"],
        "env_spec_contents": {"README.md": "## Environment\n\nVerification runs in CLEANROOM_MARKER.\n"},
        "cache_mount_path": MOUNT,
        "verify_provided": verify_provided,
        "activate_provided": True,
    }
    return svc._build_job_payload(ctx, None).persona


def test_the_readme_reaches_the_bootstrap_persona() -> None:
    persona = _persona(SimpleNamespace(verifies_clean_room=True))
    assert "CLEANROOM_MARKER" in persona


def test_on_kubernetes_the_context_comes_first_and_says_where_work_is_checked() -> None:
    persona = _persona(SimpleNamespace(verifies_clean_room=True))
    assert persona.startswith("VERIFICATION CONTEXT")
    assert "NO checkout of your work" in persona
    assert f"READ-ONLY at {MOUNT}" in persona
    assert f"DEVENV={MOUNT}" in persona
    assert "BUNDLE_GEMFILE" in persona
    assert "stdin closed" in persona
    # The old instruction to decide in the agent's own checkout is gone from the verify wording.
    assert "the run coordinare makes" not in persona


def test_on_docker_the_persona_keeps_the_checkout_wording() -> None:
    persona = _persona(SimpleNamespace())
    assert "VERIFICATION CONTEXT" not in persona
    assert "the run coordinare makes" in persona



# --- garden's layout: the rendered verify passes in the pod against the existing cache, without reinstalling -----------
def _fake(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


def test_the_rendered_verify_passes_on_garden_s_cache_in_the_pod(tmp_path: Path) -> None:
    cache, _checkout, other = _layout(tmp_path)
    ruby_bin = cache / ".rbenv" / "versions" / "3.4.2" / "bin"
    ruby_bin.mkdir(parents=True)
    _fake(ruby_bin / "ruby", "printf 3.4.2\n")
    # bundle: the installed bundle is the one BUNDLE_PATH names, and the Gemfile is the commit's (BUNDLE_GEMFILE).
    _fake(
        ruby_bin / "bundle",
        f'[ "$BUNDLE_PATH" = "{cache / "project" / "bundle"}" ] || {{ echo "wrong BUNDLE_PATH: $BUNDLE_PATH" >&2; exit 7; }}\n'
        '[ -f "$BUNDLE_GEMFILE" ] || exit 8\n'
        "exit 0\n",
    )
    no_gem = tmp_path / "no-gem"
    no_gem.mkdir()
    _fake(no_gem / "gem", "exit 1\n")
    manifest = EnvManifest(
        symphony_name="dettmore",
        items=[
            ManifestItem(name="ruby", kind="runtime", version="3.4.2", source=".ruby-version"),
            ManifestItem(name="bundler", kind="gem", source="Gemfile.lock"),
        ],
    )
    activate = render_activate_sh(manifest, cache_mount_path=str(cache))
    verify = render_verify_sh(manifest, cache_mount_path=str(cache), activate_text=activate)
    pod = _manifest(mount_path=str(cache), verify_text=verify, project_files=PROJECT)
    env_extra = {"PATH": f"{no_gem}{os.pathsep}{os.environ['PATH']}"}

    result = _run_in_pod(pod, other, extra_env=env_extra)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASSED" in result.stdout


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["Evicted", "NodeLost", "DeadlineExceeded"])
async def test_pod_infrastructure_termination_is_transient(reason: str) -> None:
    class InfrastructureCore(_FakeCoreV1):
        def read_namespaced_pod(self, name: str, namespace: str) -> Any:
            if name in self.deleted:
                raise ApiException(status=404)
            return SimpleNamespace(status=SimpleNamespace(phase="Failed", reason=reason))

    core = InfrastructureCore(["Failed"])
    ok, _detail = await _run(_runtime(core))
    assert ok is None
    assert len(core.deleted) == 1

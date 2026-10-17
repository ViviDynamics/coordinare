"""Reviewed environment contracts are the only bootstrap authority."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from performer import main
from performer.models import Score, Stand
from performer.workflows.bootstrap_environment import BootstrapEnvironment


def test_bootstrap_admission_rejects_missing_review(tmp_path):
    payload = SimpleNamespace(role="env_bootstrap", env_cache_path=str(tmp_path), metadata={}, env_provenance=None, bootstrap_scripts=None)
    refused = main._admission_refusal(payload)
    assert refused is not None
    assert refused[0] == "environment_contract_refused"


@pytest.mark.asyncio
async def test_workflow_snapshot_rejects_missing_review(tmp_path):
    score = Score(title="bootstrap", repo_url="https://github.com/a/b", branch="main", env_cache_path=str(tmp_path))
    result = await BootstrapEnvironment(Stand(path=tmp_path, branch="main"), score).run("snapshot")
    assert result["passed"] is False
    assert "review" in result["reason"]


def test_score_preserves_contract_metadata():
    score = Score(title="bootstrap", repo_url="https://github.com/a/b", branch="main", environment_manifest={"schema_version": 1})
    assert score.environment_manifest == {"schema_version": 1}


def contract_score(tmp_path, *, failed=False):
    from coordinare_service_inference.environment.models import EnvironmentManifest

    manifest = EnvironmentManifest.model_validate({
        "schema_version": 1, "symphony_name": "example", "source_commit": "abc",
        "inputs": {"tools.plan": "a" * 64}, "requirements": [],
        "install": [{"id": "install", "argv": ["python3", "-c", "from pathlib import Path; Path('installed').touch(); Path('alien/bin').mkdir(parents=True, exist_ok=True)"]}],
        "checks": [{"id": "check", "argv": ["python3", "-c", "raise SystemExit(1)" if failed else "from pathlib import Path; assert Path('installed').exists()"]}],
        "activation": {"path": ["{cache}/alien/bin"], "env": {"ALIEN_HOME": "{cache}/alien"}},
        "services": [], "empty_reason": "Baseline image suffices", "policy_version": "351-v1",
    })
    review = {"schema_version": 1, "manifest_digest": manifest.digest(), "source_commit": "abc",
              "verdict": "accept", "rationale": "Inspected setup instructions", "sources": ["tools.plan"], "inputs": dict(manifest.inputs), "policy_version": "351-v1"}
    return Score(title="bootstrap", repo_url="https://github.com/a/b", branch="main", role="env_bootstrap",
                 env_cache_path=str(tmp_path), environment_manifest=manifest.model_dump(),
                 environment_assessment=review, environment_manifest_digest=manifest.digest())


@pytest.mark.parametrize("mutation", ["digest", "commit", "verdict", "source"])
def test_contract_rejects_tampered_review(tmp_path, mutation):
    from performer.environment_contract import accepted_manifest

    score = contract_score(tmp_path)
    if mutation == "digest":
        score.environment_manifest["checks"][0]["argv"] = ["true"]
    elif mutation == "commit":
        score.environment_assessment["source_commit"] = "different"
    elif mutation == "verdict":
        score.environment_assessment["verdict"] = "revise"
    else:
        score.environment_assessment["sources"] = ["unread.plan"]
    with pytest.raises(ValueError):
        accepted_manifest(score)


@pytest.mark.asyncio
async def test_workflow_executes_accepted_install_and_skips_inference(tmp_path, monkeypatch):
    score = contract_score(tmp_path)
    stand = Stand(path=tmp_path, branch="main")
    async def legacy(*args, **kwargs):
        pytest.fail("reviewed manifests cannot invoke legacy service inference")
    monkeypatch.setattr(main, "_run_service_inference", legacy)
    environment = BootstrapEnvironment(stand, score)
    assert (await environment.run("snapshot"))["passed"]
    assert (await environment.run("install"))["passed"]
    assert (tmp_path / "installed").exists()
    assert (await environment.run("inference"))["passed"]
    assert (await environment.run("verify"))["passed"]


@pytest.mark.asyncio
async def test_workflow_failed_contract_checks_hold(tmp_path):
    score = contract_score(tmp_path, failed=True)
    environment = BootstrapEnvironment(Stand(path=tmp_path, branch="main"), score)
    result = await environment.run("install")
    assert result["passed"] is False


@pytest.mark.asyncio
async def test_prose_bootstrap_executes_contract_and_cannot_complete_failed_checks(tmp_path, monkeypatch):
    from performer.models import Performance
    from performer.status_paths import bootstrap_path

    async def legacy(*args, **kwargs):
        pytest.fail("legacy inference is forbidden for reviewed contracts")
    monkeypatch.setattr(main, "_run_service_inference", legacy)
    for failed in (False, True):
        score = contract_score(tmp_path, failed=failed)
        perf = Performance(session_id="test", stand=Stand(path=tmp_path, branch="main"), score=score,
                           backend=SimpleNamespace(), role="env_bootstrap")
        response = await bootstrap_path(perf, SimpleNamespace(output=""), None)
        assert response.status == ("error" if failed else "env_bootstrap_complete")
        assert response.report["environment_verification"]["passed"] is (not failed)
        assert response.report["environment_verification"]["manifest_digest"] == score.environment_manifest_digest


@pytest.mark.asyncio
async def test_consumer_activation_uses_manifest_without_ruby_inference(tmp_path, monkeypatch):
    from performer import workspace
    from performer.environment_contract import close_environment, prepare_environment

    score = contract_score(tmp_path)
    score.role = "implementing"
    (tmp_path / "installed").touch()
    (tmp_path / "alien/bin").mkdir(parents=True)
    (tmp_path / "activate.sh").write_text("export RBENV_ROOT=/invented/ruby\n")
    async def legacy(*args, **kwargs):
        pytest.fail("manifest consumers cannot activate or start legacy cache recipes")
    monkeypatch.setattr(workspace, "_activate_env_cache", legacy)
    monkeypatch.setattr(workspace, "_start_env_cache_services", legacy)
    monkeypatch.setattr(workspace, "_extract_env_cache_libs", legacy)
    stand = Stand(path=tmp_path, branch="main")
    try:
        await prepare_environment(score, stand)
        assert stand.cache_env["ALIEN_HOME"] == str(tmp_path / "alien")
        assert workspace.services_healthy_this_job() is True
        assert stand.cache_env["PATH"].split(":")[0] == str(tmp_path / "alien/bin")
    finally:
        await close_environment(stand)


@pytest.mark.asyncio
async def test_frozen_artifacts_cannot_be_modified_by_install(tmp_path):
    from coordinare_service_inference.environment.models import EnvironmentManifest

    score = contract_score(tmp_path)
    score.environment_manifest["install"][0]["argv"] = ["python3", "-c", "from pathlib import Path; Path('verify.sh').write_text('forged')"]
    manifest = EnvironmentManifest.model_validate(score.environment_manifest)
    score.environment_manifest_digest = manifest.digest()
    score.environment_assessment["manifest_digest"] = manifest.digest()
    score.verify_provided = True
    (tmp_path / "verify.sh").write_text("trusted")
    environment = BootstrapEnvironment(Stand(path=tmp_path, branch="main"), score)
    assert (await environment.run("snapshot"))["passed"]
    result = await environment.run("install")
    assert result["passed"] is False
    assert result["integrity_failure"] is True


@pytest.mark.asyncio
async def test_workflow_installs_without_model_and_reports_parent_evidence(tmp_path):
    from performer.workflows.base import WorkflowMetrics
    from performer.workflows.env_bootstrap import EnvBootstrapWorkflow
    from performer.workflows.toolkit import Toolkit

    score = contract_score(tmp_path)
    stand = Stand(path=tmp_path, branch="main")
    toolkit = Toolkit(metrics=WorkflowMetrics(), bootstrap_step_runner=BootstrapEnvironment(stand, score).run)
    result = await EnvBootstrapWorkflow().run(stand, score, toolkit)
    report = result.report["env_bootstrap_run"]
    assert report["status"] == "complete"
    assert report["environment_verification"]["passed"] is True
    assert report["environment_verification"]["manifest_digest"] == score.environment_manifest_digest
    assert toolkit.metrics.agent_turns == 0


@pytest.mark.asyncio
async def test_harness_shell_restores_declared_path_precedence(tmp_path):
    import os
    import subprocess

    from performer.backends._env_policy import build_subprocess_env
    from performer.environment_contract import close_environment, prepare_environment

    score = contract_score(tmp_path)
    score.role = "implementing"
    (tmp_path / "installed").touch()
    declared = tmp_path / "alien/bin"
    declared.mkdir(parents=True)
    image = tmp_path / "image"
    image.mkdir()
    for path, value in ((image, "image"), (declared, "reviewed")):
        binary = path / "unfamiliar"
        binary.write_text(f"#!/bin/sh\necho {value}\n")
        binary.chmod(0o755)
    stand = Stand(path=tmp_path, branch="main")
    try:
        await prepare_environment(score, stand)
        env = build_subprocess_env(cache_env=stand.cache_env, base_env={**os.environ, "PATH": str(image) + ":" + os.environ["PATH"]})
        output = subprocess.check_output(["/bin/bash", "-c", "unfamiliar"], env=env, text=True)
        assert output.strip() == "reviewed"
    finally:
        await close_environment(stand)


def test_review_can_cite_additional_independently_inspected_sources(tmp_path):
    from performer.environment_contract import accepted_manifest

    score = contract_score(tmp_path)
    score.environment_assessment["inputs"]["nested/other.plan"] = "b" * 64
    score.environment_assessment["sources"].append("nested/other.plan")
    assert accepted_manifest(score).digest() == score.environment_manifest_digest


def test_review_requires_independent_input_identity_ledger(tmp_path):
    from performer.environment_contract import accepted_manifest

    score = contract_score(tmp_path)
    score.environment_assessment.pop("inputs")
    with pytest.raises(ValueError, match="source identities"):
        accepted_manifest(score)


@pytest.mark.asyncio
async def test_bootstrap_checkout_is_pinned_to_reviewed_commit(tmp_path, monkeypatch):
    from performer import workspace
    from performer.environment_contract import close_environment

    score = contract_score(tmp_path)
    commands = []
    async def git(argv, **kwargs):
        commands.append(argv)
        return 0, ""
    monkeypatch.setattr(workspace, "_run_git", git)
    original_mkdtemp = workspace.tempfile.mkdtemp
    def temporary(*args, **kwargs):
        if kwargs.get("prefix", "").startswith("performer-"):
            return str(tmp_path)
        return original_mkdtemp(*args, **kwargs)
    monkeypatch.setattr(workspace.tempfile, "mkdtemp", temporary)
    stand = await workspace.clone_repository(score)
    try:
        assert ["git", "fetch", "origin", "abc"] in commands
        assert ["git", "checkout", "--detach", "abc"] in commands
        assert not any("push" in command for command in commands)
    finally:
        await close_environment(stand)


@pytest.mark.asyncio
async def test_consumer_readiness_failure_requests_cache_rebootstrap(tmp_path, monkeypatch):
    import json

    from performer.environment_contract import EnvironmentReadinessError
    from performer.server.models import JobInitPayload

    payload = JobInitPayload(job_id="j", card_id="c", role="implementer", backend="codex", persona="p",
                             repo_url="https://github.com/a/b", branch="main")
    async def dispatch(*args, **kwargs):
        raise EnvironmentReadinessError("required tool check failed")
    monkeypatch.setattr(main, "handle_dispatch", dispatch)
    result = await main._perform_job(payload)
    assert result.success is False
    assert json.loads(result.summary)["env_cache_health_failed"] is True


@pytest.mark.asyncio
async def test_failed_consumer_preflight_removes_clone(tmp_path, monkeypatch):
    from performer import workspace
    from performer.environment_contract import EnvironmentReadinessError

    clone = tmp_path / "clone"
    clone.mkdir()
    score = contract_score(tmp_path)
    score.role = "implementing"
    async def git(*args, **kwargs):
        return 0, ""
    async def failed(*args, **kwargs):
        raise EnvironmentReadinessError("health failed")
    original_mkdtemp = workspace.tempfile.mkdtemp
    def temporary(*args, **kwargs):
        return str(clone) if kwargs.get("prefix", "").startswith("performer-") else original_mkdtemp(*args, **kwargs)
    monkeypatch.setattr(workspace, "_run_git", git)
    monkeypatch.setattr(workspace.tempfile, "mkdtemp", temporary)
    monkeypatch.setattr("performer.environment_contract.prepare_environment", failed)
    with pytest.raises(EnvironmentReadinessError):
        await workspace.clone_repository(score)
    assert not clone.exists()


@pytest.mark.asyncio
async def test_consumer_holds_declared_service_until_job_cleanup(tmp_path):
    from coordinare_service_inference.environment.models import EnvironmentManifest

    from performer.environment_contract import close_environment, prepare_environment

    score = contract_score(tmp_path)
    score.role = "implementing"
    score.coordinare_manages_services = False  # Reviewed lifecycle has no legacy feature toggle.
    (tmp_path / "installed").touch()
    (tmp_path / "alien/bin").mkdir(parents=True)
    marker = tmp_path / "service-live"
    score.environment_manifest["services"] = [{"id": "unfamiliar", "start": {"id": "start", "argv": ["touch", str(marker)]},
        "health": {"id": "health", "argv": ["test", "-f", str(marker)]},
        "stop": {"id": "stop", "argv": ["rm", "-f", str(marker)]}}]
    manifest = EnvironmentManifest.model_validate(score.environment_manifest)
    score.environment_manifest_digest = manifest.digest()
    score.environment_assessment["manifest_digest"] = manifest.digest()
    stand = Stand(path=tmp_path, branch="main")
    await prepare_environment(score, stand)
    try:
        assert marker.exists()
    finally:
        await close_environment(stand)
    assert not marker.exists()


def test_invalid_contract_error_does_not_expose_manifest_values(tmp_path):
    score = contract_score(tmp_path)
    score.environment_manifest["checks"][0]["argv"] = [{"private": "sensitive-input"}]
    payload = SimpleNamespace(role="env_bootstrap", env_cache_path=str(tmp_path), metadata=score.model_dump())
    refused = main._admission_refusal(payload)
    assert refused is not None
    assert "sensitive-input" not in refused[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["implementing", "env_bootstrap"])
async def test_required_scoped_failure_cannot_hide_behind_baseline(tmp_path, role):
    from coordinare_service_inference.environment.models import EnvironmentManifest

    from performer.environment_contract import execute_contract

    score = contract_score(tmp_path)
    score.role = role
    score.environment_manifest["checks"] = [
        {"id": "baseline", "argv": ["true"]},
        {"id": "scoped-failure", "argv": ["false"], "scope": ["implementing"]}]
    manifest = EnvironmentManifest.model_validate(score.environment_manifest)
    score.environment_manifest_digest = manifest.digest()
    score.environment_assessment["manifest_digest"] = manifest.digest()
    (tmp_path / "alien/bin").mkdir(parents=True)
    result = await execute_contract(score, Stand(path=tmp_path, branch="main"))
    assert result["passed"] is False
    scoped = next(item for item in result["environment_verification"]["results"] if item["id"] == "scoped-failure")
    assert scoped["status"] == "fail"
    assert scoped["required"] is True


@pytest.mark.asyncio
async def test_cancelled_proxy_setup_closes_consumer_lifecycle(tmp_path, monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock

    from performer.config import Settings
    from performer.protocol import PerformerMessage

    lifecycle = SimpleNamespace(aclose=AsyncMock())
    stand = Stand(path=tmp_path, branch="main", environment_lifecycle=lifecycle)
    monkeypatch.setattr(main, "clone_repository", AsyncMock(return_value=stand))
    monkeypatch.setattr(main, "_launch_dispatch_proxy", AsyncMock(side_effect=asyncio.CancelledError))
    monkeypatch.setattr(main, "cleanup_stand", lambda stand: None)
    message = PerformerMessage(action="dispatch", payload={"title": "t", "repo_url": "https://github.com/a/b", "branch": "main"})
    with pytest.raises(asyncio.CancelledError):
        await main.handle_dispatch(message, Settings())
    lifecycle.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_known_dispatch_secret_names_redact_unfamiliar_credentials(tmp_path, monkeypatch):
    from coordinare_service_inference.environment.models import EnvironmentManifest

    from performer.environment_contract import execute_contract

    score = contract_score(tmp_path)
    score.environment_manifest["checks"] = [{"id": "print", "argv": ["python3", "-c", "import os; print(os.environ['FIXTURE_DSN'])"]}]
    manifest = EnvironmentManifest.model_validate(score.environment_manifest)
    score.environment_manifest_digest = manifest.digest()
    score.environment_assessment["manifest_digest"] = manifest.digest()
    score = Score.model_validate({**score.model_dump(), "environment_secret_names": ["FIXTURE_DSN"]})
    monkeypatch.setenv("FIXTURE_DSN", "opaque-sensitive-connection-value")
    (tmp_path / "alien/bin").mkdir(parents=True)
    result = await execute_contract(score, Stand(path=tmp_path, branch="main"))
    assert result["passed"] is True
    outputs = "\n".join(item["output"] for item in result["environment_verification"]["results"])
    assert "opaque-sensitive-connection-value" not in outputs
    assert "[REDACTED]" in outputs


@pytest.mark.asyncio
async def test_status_advertises_reviewed_manifest_capability():
    from httpx import ASGITransport, AsyncClient

    from performer.server import create_app

    app = create_app(expected_token=None)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        status = (await client.get("/status")).json()
    assert status["environment_manifest_version"] == 1
    import platform

    assert status["environment_platform"] == platform.system().lower() + "/" + platform.machine().lower()

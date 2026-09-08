"""174: real production toolkit, fixture harness, real cache verifier."""
from __future__ import annotations

import asyncio
import subprocess
from unittest.mock import AsyncMock

import pytest
from performer.backends.base import BackendStatus
from performer.models import Score, Stand
from performer.workflows.adapter import build_production_toolkit
from performer.workflows.base import WorkflowMetrics
from performer.workflows.env_bootstrap import EnvBootstrapWorkflow


@pytest.fixture
def setup(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=t@example.org",
                    "commit", "--allow-empty", "-qm", "fixture"], cwd=repo, check=True)
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "activate.sh").write_text("true\n")
    (cache / "verify.sh").write_text(
        '#!/bin/bash\ntest -f "$(dirname "$0")/installed" || { echo "FAIL missing tool"; exit 1; }\n')
    score = Score(title="fixture", repo_url="https://github.com/o/r", branch="fixture",
                  role="env_bootstrap", workflow="env_bootstrap", backend="codex",
                  env_cache_path=str(cache), verify_provided=True, activate_provided=True,
                  coordinare_manages_services=False, persona_instructions="Install the required tool")
    inference = AsyncMock(return_value={"inference_succeeded": True})
    monkeypatch.setattr("performer.main._run_service_inference", inference)
    return Stand(path=repo, branch="fixture"), score, cache, inference


class Harness:
    def __init__(self, cache, action):
        self.cache, self.action = cache, action
        self.personas = []
        self.stopped = False

    async def start(self, stand, score, **kwargs):
        self.personas.append(score.persona_instructions)
        if self.action == "install" or (self.action == "repair" and len(self.personas) == 2):
            (self.cache / "installed").touch()
        elif self.action == "overwrite":
            (self.cache / "verify.sh").write_text("exit 0\n")
        elif self.action == "delete":
            (self.cache / "activate.sh").unlink()

    def get_status(self):
        return BackendStatus(state="working" if self.action == "hang" else "done")

    def drain_events(self):
        return []

    async def stop(self):
        self.stopped = True


def toolkit(stand, score, harness):
    return build_production_toolkit(score, metrics=WorkflowMetrics(), event_sink=None,
                                    workflow_name="env_bootstrap", stand=stand,
                                    backend_factory=lambda name: harness)


@pytest.mark.parametrize(("action", "status", "turns"), [
    ("install", "complete", 1), ("repair", "complete", 2), ("missing", "error", 2),
    ("overwrite", "error", 1), ("delete", "error", 1),
])
async def test_install_repair_and_integrity(setup, action, status, turns):
    stand, score, cache, inference = setup
    harness = Harness(cache, action)
    tk = toolkit(stand, score, harness)
    result = await EnvBootstrapWorkflow().run(stand, score, tk)
    assert result.report["env_bootstrap_run"]["status"] == status
    assert tk.metrics.agent_turns == turns
    assert tk.metrics.model_calls == 0
    assert "install" in tk.metrics.step_durations_ms
    if action == "repair":
        assert "FAIL missing tool" in harness.personas[-1]
        assert result.report["env_bootstrap_run"]["inference"]["inference_succeeded"] is True
    if action in {"overwrite", "delete"}:
        inference.assert_not_awaited()
        assert "artifact" in result.report["env_bootstrap_run"]["reason"]


async def test_missing_promised_verifier_fails_before_install(setup):
    stand, score, cache, _ = setup
    (cache / "verify.sh").unlink()
    harness = Harness(cache, "install")
    result = await EnvBootstrapWorkflow().run(stand, score, toolkit(stand, score, harness))
    assert result.report["env_bootstrap_run"]["status"] == "error"
    assert harness.personas == []


@pytest.mark.parametrize("managed", [True, False])
async def test_readiness_precedes_verification_and_respects_mode(setup, monkeypatch, managed):
    stand, score, cache, _ = setup
    score.coordinare_manages_services = managed
    order = []
    async def ready(*args):
        order.append("readiness")
        return True, []
    async def verify(*args):
        order.append("verify")
        return True, ""
    monkeypatch.setattr("performer.workspace.run_service_readiness", ready)
    monkeypatch.setattr("performer.workspace.run_env_cache_verify", verify)
    harness = Harness(cache, "install")
    result = await EnvBootstrapWorkflow().run(stand, score, toolkit(stand, score, harness))
    assert result.report["env_bootstrap_run"]["status"] == "complete"
    assert order == (["readiness", "verify"] if managed else ["verify"])


async def test_zero_repair_budget(setup):
    stand, score, cache, _ = setup
    score.workflow_env = {"ENV_BOOTSTRAP_MAX_REPAIRS": "0"}
    harness = Harness(cache, "missing")
    result = await EnvBootstrapWorkflow().run(stand, score, toolkit(stand, score, harness))
    assert result.report["env_bootstrap_run"]["status"] == "error"
    assert len(harness.personas) == 1


async def test_timeout_stops_harness_without_repair(setup):
    stand, score, cache, _ = setup
    score.workflow_env = {"ENV_BOOTSTRAP_TIMEOUT_SECONDS": "1"}
    harness = Harness(cache, "hang")
    tk = toolkit(stand, score, harness)
    result = await EnvBootstrapWorkflow().run(stand, score, tk)
    assert tk.metrics.agent_turns == 1
    assert len(tk.metrics.turn_durations_ms) == 1
    assert result.report["env_bootstrap_run"]["status"] == "error"
    assert "timeout" in result.report["env_bootstrap_run"]["reason"]
    assert harness.stopped
    assert len(harness.personas) == 1


async def test_cancellation_stops_harness_and_propagates(setup):
    stand, score, cache, _ = setup
    harness = Harness(cache, "hang")
    task = asyncio.create_task(EnvBootstrapWorkflow().run(stand, score, toolkit(stand, score, harness)))
    for _ in range(100):
        if harness.personas:
            break
        await asyncio.sleep(0.001)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert harness.stopped
    assert len(harness.personas) == 1


@pytest.mark.parametrize("env", [
    {"ENV_BOOTSTRAP_MAX_REPAIRS": "3"}, {"ENV_BOOTSTRAP_TIMEOUT_SECONDS": "0"},
    {"ENV_BOOTSTRAP_TIMEOUT_SECONDS": "oops"},
])
async def test_invalid_budget_fails_before_install(setup, env):
    stand, score, cache, _ = setup
    score.workflow_env = env
    harness = Harness(cache, "install")
    result = await EnvBootstrapWorkflow().run(stand, score, toolkit(stand, score, harness))
    assert result.report["env_bootstrap_run"]["status"] == "error"
    assert not harness.personas


async def test_missing_unpromised_verifier_is_still_not_success(setup):
    stand, score, cache, _ = setup
    score.verify_provided = False
    (cache / "verify.sh").unlink()
    harness = Harness(cache, "install")
    result = await EnvBootstrapWorkflow().run(stand, score, toolkit(stand, score, harness))
    assert result.report["env_bootstrap_run"]["status"] == "error"


async def test_symlinked_promised_artifact_fails_before_install(setup):
    stand, score, cache, _ = setup
    verify = cache / "verify.sh"
    original = cache / "original.sh"
    verify.rename(original)
    verify.symlink_to(original)
    harness = Harness(cache, "install")
    result = await EnvBootstrapWorkflow().run(stand, score, toolkit(stand, score, harness))
    assert result.report["env_bootstrap_run"]["status"] == "error"
    assert not harness.personas


async def test_inference_timeout_remains_best_effort(setup):
    stand, score, cache, inference = setup
    inference.side_effect = TimeoutError
    harness = Harness(cache, "install")
    result = await EnvBootstrapWorkflow().run(stand, score, toolkit(stand, score, harness))
    run = result.report["env_bootstrap_run"]
    assert run["status"] == "complete"
    assert run["inference"]["inference_skipped_reason"] == "timeout"


async def test_readiness_failure_is_repaired_before_verify(setup, monkeypatch):
    stand, score, cache, _ = setup
    score.coordinare_manages_services = True
    readiness = AsyncMock(side_effect=[(False, [{"reason": "database not ready"}]), (True, [])])
    monkeypatch.setattr("performer.workspace.run_service_readiness", readiness)
    harness = Harness(cache, "install")
    result = await EnvBootstrapWorkflow().run(stand, score, toolkit(stand, score, harness))
    assert result.report["env_bootstrap_run"]["status"] == "complete"
    assert len(harness.personas) == 2
    assert "database not ready" in harness.personas[-1]


async def test_adapter_reports_named_steps_and_real_metrics(setup):
    from performer.workflows.adapter import WorkflowAdapter
    stand, score, cache, _ = setup
    harness = Harness(cache, "install")
    adapter = WorkflowAdapter("env_bootstrap", toolkit_factory=lambda metrics, sink:
        build_production_toolkit(score, metrics=metrics, event_sink=sink,
                                 workflow_name="env_bootstrap", stand=stand,
                                 backend_factory=lambda name: harness))
    await adapter.start(stand, score)
    await adapter._task
    assert adapter.get_status().state == "done"
    assert adapter.metrics.agent_turns == 1
    assert adapter.metrics.model_calls == 0
    assert {"snapshot", "integrity", "inference", "readiness", "verify", "install"} <= adapter.metrics.step_durations_ms.keys()
    assert any(e.text == "env_bootstrap.verify" for e in adapter.drain_events())


async def test_cancelled_verification_reaps_subprocess(setup):
    import os
    import signal
    from contextlib import suppress

    from performer.workspace import run_env_cache_verify
    _, _, cache, _ = setup
    pid_path = cache / "pid"
    (cache / "verify.sh").write_text(f'echo $$ > "{pid_path}"\nexec sleep 60\n')
    task = asyncio.create_task(run_env_cache_verify(str(cache)))
    for _ in range(200):
        if pid_path.exists():
            break
        await asyncio.sleep(0.005)
    pid = int(pid_path.read_text())
    try:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        with suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)


async def test_cancellation_during_harness_start_stops_it(setup):
    stand, score, cache, _ = setup
    harness = Harness(cache, "hang")
    started = asyncio.Event()
    async def start(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()
    harness.start = start
    task = asyncio.create_task(EnvBootstrapWorkflow().run(stand, score, toolkit(stand, score, harness)))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert harness.stopped


async def test_harness_error_never_runs_verification_or_repair(setup):
    stand, score, cache, inference = setup
    harness = Harness(cache, "missing")
    harness.get_status = lambda: BackendStatus(state="error", error_reason="install failed")
    result = await EnvBootstrapWorkflow().run(stand, score, toolkit(stand, score, harness))
    assert result.report["env_bootstrap_run"]["status"] == "error"
    assert len(harness.personas) == 1
    inference.assert_not_awaited()


async def test_no_cache_fails_before_install(setup):
    stand, score, cache, _ = setup
    score.env_cache_path = ""
    harness = Harness(cache, "install")
    result = await EnvBootstrapWorkflow().run(stand, score, toolkit(stand, score, harness))
    assert result.report["env_bootstrap_run"]["status"] == "error"
    assert "env_cache_path" in result.report["env_bootstrap_run"]["reason"]
    assert not harness.personas


@pytest.mark.parametrize("phase", ["start", "health"])
async def test_cancelled_service_gate_cannot_write_after_timeout(setup, phase):
    import os
    import signal
    from contextlib import suppress

    from performer.workspace import _run_env_cache_health_check, _start_env_cache_services
    _, _, cache, _ = setup
    services = cache / "services"
    services.mkdir()
    pid_path = cache / "pid"
    script = services / f"services-{phase}.sh"
    late = cache / "late"
    script.write_text(f'#!/bin/bash\necho $$ > "{pid_path}"\n(sleep 0.2; touch "{late}") &\nexec sleep 60\n')
    script.chmod(0o755)
    run = _start_env_cache_services if phase == "start" else _run_env_cache_health_check
    task = asyncio.create_task(run(str(cache), {}))
    for _ in range(200):
        if pid_path.exists():
            break
        await asyncio.sleep(0.005)
    pid = int(pid_path.read_text())
    try:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        await asyncio.sleep(0.3)
        assert not late.exists()
    finally:
        with suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)


async def test_verifier_cannot_rewrite_activation_and_report_success(setup):
    stand, score, cache, _ = setup
    (cache / "verify.sh").write_text(f'echo changed > "{cache / "activate.sh"}"\nexit 0\n')
    harness = Harness(cache, "install")
    result = await EnvBootstrapWorkflow().run(stand, score, toolkit(stand, score, harness))
    assert result.report["env_bootstrap_run"]["status"] == "error"
    assert "artifact changed" in result.report["env_bootstrap_run"]["reason"]
    assert len(harness.personas) == 1

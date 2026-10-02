"""Spec 167 FR-011 to FR-014, FR-017: main.py maps the implementer workflow's
run record onto the response the prose path returns for that outcome, and
skips the prose post-processing (089 gate, push, PR, 075 loop). Without the
report key the prose path is untouched."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from performer.backends.base import BackendStatus
from performer.config import Settings
from performer.main import Performance, handle_status
from performer.models import Score, Stand
from performer.protocol import PerformerMessage


def _perf() -> Performance:
    stand = Stand(path=Path("/tmp/fake"), branch="feat/test")
    stand.git_env = {}
    score = Score(title="Test card", repo_url="https://github.com/acme/repo", branch="feat/test", issue_number=7, workflow="implementer")
    return Performance(session_id="sid", stand=stand, score=score, backend=MagicMock(), role="implementing")


def _status_msg() -> PerformerMessage:
    return PerformerMessage(action="status", session_id="sid", payload={})


def _run(status: str, **over) -> dict:
    record = {
        "status": status, "reason": over.pop("reason", ""), "milestones_planned": 1, "milestones_completed": 1 if status == "pr_opened" else 0,
        "next_focus_milestone": over.pop("next_focus", None), "per_milestone": [], "quality_attempts": [], "ci_attempts": [],
        "scope_reverts": [], "phase_durations_ms": {"intake": 1}, "total_duration_ms": 10, "turn_count": 2, "model_calls": 0, "github_api_calls": 3,
    }
    return {"implementer_run": record, "pr_url": over.pop("pr_url", None), "workflow_metrics": {"step_durations_ms": {"intake": 1}}}


async def _handle(report: dict, *, push_error=None, workflow="implementer"):
    perf = _perf()
    perf.score = perf.score.model_copy(update={"workflow": workflow})
    perf.backend.get_status.return_value = BackendStatus(state="done", output=json.dumps(report))
    settings = Settings(AGENT_BACKEND="codex", AGENT_TIMEOUT=1800)
    with (
        patch("performer.main.get_head_sha", new=AsyncMock(return_value="abc123")),
        patch("performer.main.push_branch", new=AsyncMock(side_effect=push_error)) as push,
        patch("performer.main.create_pull_request", new=AsyncMock(return_value=("https://pr", "node"))) as create_pr,
        patch("performer.main.commit_file", new=AsyncMock()) as commit,
        patch("performer.main._run_test_check", new=AsyncMock()) as gate,
    ):
        resp = await handle_status(_status_msg(), perf, settings)
    return perf, resp, {"push": push, "create_pr": create_pr, "commit": commit, "gate": gate}


@pytest.mark.asyncio
async def test_green_ci_reports_pr_opened_and_skips_the_prose_path():
    perf, resp, mocks = await _handle(_run("pr_opened", pr_url="https://github.com/acme/repo/pull/12", reason="all checks green"))
    assert resp.status == "pr_opened" and resp.pr_url == "https://github.com/acme/repo/pull/12"
    assert resp.report["implementer_run"]["status"] == "pr_opened" and resp.head_after == "abc123"
    assert resp.pushed_branch == "feat/test"
    assert perf.state == "pr_opened" and perf.pr_url == "https://github.com/acme/repo/pull/12"
    for name, m in mocks.items():
        assert not m.called, f"prose path {name} must not run under the workflow"


@pytest.mark.asyncio
async def test_a_failed_milestone_is_partial_progress_with_the_next_focus():
    perf, resp, mocks = await _handle(_run("partial_progress", reason="green was not observed after 3 attempts", next_focus="milestone 1"))
    assert resp.status == "partial_progress" and resp.next_focus == "milestone 1"
    assert "green was not observed" in (resp.reason or "")
    assert resp.report["implementer_run"]["status"] == "partial_progress"
    mocks["push"].assert_awaited_once()
    assert not mocks["create_pr"].called


@pytest.mark.asyncio
async def test_a_hold_is_env_blocked_with_the_reason():
    perf, resp, _ = await _handle(_run("env_blocked", reason="CI checks pending past the wait budget: Slow"))
    assert resp.status == "env_blocked" and "Slow" in (resp.reason or "")
    assert perf.state == "env_blocked"


@pytest.mark.asyncio
async def test_a_local_gate_failure_is_changes_requested():
    perf, resp, _ = await _handle(_run("changes_requested", reason="local test gate failed after green milestones: 1 failed"))
    assert resp.status == "changes_requested" and resp.local_test_failed is True
    assert perf.state == "changes_requested"


@pytest.mark.asyncio
async def test_output_without_the_report_key_takes_the_prose_path():
    perf = _perf()
    perf.backend.get_status.return_value = BackendStatus(state="done", output="I implemented the feature.")
    settings = Settings(AGENT_BACKEND="codex", AGENT_TIMEOUT=1800)
    with (
        patch("performer.main.get_head_sha", new=AsyncMock(return_value="abc123")),
        patch("performer.main.push_branch", new=AsyncMock()) as push,
        patch("performer.main.create_pull_request", new=AsyncMock(return_value=("https://pr", "node"))) as create_pr,
        patch("performer.main.strip_agent_artifacts", new=AsyncMock(), create=True),
    ):
        resp = await handle_status(_status_msg(), perf, settings)
    assert resp.status != "pr_opened" or create_pr.called
    assert push.called or resp.status in ("working", "blocked", "error", "changes_requested")


@pytest.mark.asyncio
async def test_a_failed_checkpoint_push_holds_instead_of_claiming_saved_progress():
    perf, resp, mocks = await _handle(_run("partial_progress", next_focus="milestone 1"), push_error=RuntimeError("remote unavailable"))
    assert resp.status == "env_blocked" and perf.state == "env_blocked"
    assert "checkpoint push failed" in resp.reason
    assert "remote unavailable" in resp.reason
    assert not mocks["create_pr"].called


@pytest.mark.asyncio
async def test_checkpoint_reports_the_head_after_push_rebases_it():
    perf = _perf()
    perf.backend.get_status.return_value = BackendStatus(state="done", output=json.dumps(_run("partial_progress")))
    with (
        patch("performer.main.get_head_sha", new=AsyncMock(side_effect=["before", "rebased"])),
        patch("performer.main.push_branch", new=AsyncMock()),
    ):
        resp = await handle_status(_status_msg(), perf, Settings(AGENT_BACKEND="codex"))
    assert resp.head_after == "rebased"


@pytest.mark.asyncio
async def test_partial_checkpoint_survives_a_fresh_clone(tmp_path):
    import subprocess

    def git(*args, cwd=None):
        return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()

    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    git("init", "--bare", str(remote))
    git("init", "-b", "feat/test", str(work))
    git("config", "user.name", "Test", cwd=work)
    git("config", "user.email", "test@example.test", cwd=work)
    (work / "base.txt").write_text("baseline")
    git("add", "base.txt", cwd=work)
    git("commit", "-m", "baseline", cwd=work)
    base = git("rev-parse", "HEAD", cwd=work)
    (work / "m0.py").write_text("VALUE = 1")
    git("add", "m0.py", cwd=work)
    git("commit", "-m", "feat(#7): milestone 0", cwd=work)
    completed = git("rev-parse", "HEAD", cwd=work)
    (work / "unfinished.py").write_text("must not be checkpointed")
    perf = _perf()
    perf.stand = Stand(path=work, branch="feat/test")
    # Local bare remote replaces the validated HTTPS transport for this test.
    perf.score = perf.score.model_copy(update={"repo_url": str(remote)})
    perf.head_at_start = base
    perf.backend.get_status.return_value = BackendStatus(state="done", output=json.dumps(_run("partial_progress", next_focus="milestone 1")))
    with patch("performer.main.create_pull_request", new=AsyncMock()) as create_pr:
        response = await handle_status(_status_msg(), perf, Settings(AGENT_BACKEND="codex"))
    assert response.status == "partial_progress" and response.head_after == completed
    create_pr.assert_not_called()
    fresh = tmp_path / "fresh"
    git("clone", "--branch", "feat/test", str(remote), str(fresh))
    assert (fresh / "m0.py").read_text() == "VALUE = 1"
    assert not (fresh / "unfinished.py").exists()
    assert git("log", "-1", "--format=%s", cwd=fresh) == "feat(#7): milestone 0"


@pytest.mark.asyncio
async def test_workflow_report_checkpoints_even_without_optional_workflow_setting():
    _, response, mocks = await _handle(_run("partial_progress"), workflow=None)
    assert response.status == "partial_progress"
    mocks["push"].assert_awaited_once()
    mocks["create_pr"].assert_not_called()


@pytest.mark.asyncio
async def test_checkpoint_error_is_bounded_single_line_and_redacted():
    error = RuntimeError("remote unavailable\nAuthorization: Bearer fake-secret-token\n" + "stderr " * 1000)
    _, response, _ = await _handle(_run("partial_progress"), push_error=error)
    assert response.status == "env_blocked"
    assert "remote unavailable" in response.reason
    assert "fake-secret-token" not in response.reason
    assert "\n" not in response.reason
    assert len(response.reason) <= 450

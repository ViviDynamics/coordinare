"""165 FR-011: when the architect ran as a workflow, main.py returns its report
and commits no plan.md or tasks.md; the prose path is untouched otherwise."""
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
    score = Score(title="Test card", repo_url="https://github.com/acme/repo", branch="feat/test")
    return Performance(session_id="sid", stand=stand, score=score, backend=MagicMock(), role="architecting")


def _status_msg() -> PerformerMessage:
    return PerformerMessage(action="status", session_id="sid", payload={})


_REPORT = {
    "blueprint": {
        "summary": "s",
        "milestones": [{"goal": "g", "scope": ["a"], "done_when": "d"}],
        "modules": [], "data_model": {"changes": []}, "interfaces": [], "risks": [],
        "criteria": [{"surface": "/", "action": "open", "expected": "ok", "kind": "functional"}],
        "docs": [], "size": "small", "blueprint_hash": "abc", "created_at": "2026-09-06T00:00:00+00:00",
    },
    "size": "small",
    "write_free_check": {"command": "git status --porcelain", "exit_code": 0, "passed": True, "refused_commands": 0},
    "workflow_metrics": {"model_calls": 2, "step_durations_ms": {"survey": 1}},
}


@pytest.mark.asyncio
async def test_a_blueprint_report_is_returned_and_nothing_is_committed():
    perf = _perf()
    perf.backend.get_status.return_value = BackendStatus(state="done", output=json.dumps(_REPORT))
    settings = Settings(AGENT_BACKEND="codex", AGENT_TIMEOUT=1800)

    with patch("performer.main.commit_file", new=AsyncMock()) as commit:
        resp = await handle_status(_status_msg(), perf, settings)

    assert resp.status == "plan_committed"
    assert resp.report == _REPORT
    assert resp.plan_path is None
    commit.assert_not_called()
    assert perf.state == "plan_committed"


@pytest.mark.asyncio
async def test_prose_output_still_takes_the_plan_md_path():
    perf = _perf()
    perf.backend.get_status.return_value = BackendStatus(state="done", output="# Plan\n## Overview\n...")
    settings = Settings(AGENT_BACKEND="codex", AGENT_TIMEOUT=1800)

    with patch("performer.main.commit_file", new=AsyncMock()) as commit:
        resp = await handle_status(_status_msg(), perf, settings)

    assert resp.status == "plan_committed"
    assert resp.plan_path == "docs/cards/test-card/plan.md"
    assert commit.await_count >= 1


@pytest.mark.asyncio
async def test_json_without_a_blueprint_is_not_mistaken_for_the_workflow():
    """An assessor-style JSON blob from the prose architect still goes down the
    prose path (and is committed as the plan text, as before)."""
    perf = _perf()
    perf.backend.get_status.return_value = BackendStatus(state="done", output=json.dumps({"summary": "just prose in json"}))
    settings = Settings(AGENT_BACKEND="codex", AGENT_TIMEOUT=1800)

    with patch("performer.main.commit_file", new=AsyncMock()) as commit:
        resp = await handle_status(_status_msg(), perf, settings)

    assert resp.plan_path == "docs/cards/test-card/plan.md"
    assert commit.await_count >= 1

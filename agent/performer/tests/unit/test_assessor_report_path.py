"""166 FR-011: when the assessor ran as a workflow, main.py returns its report
and commits no assessment.md; the prose path is untouched otherwise."""
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
    return Performance(session_id="sid", stand=stand, score=score, backend=MagicMock(), role="assessing")


def _status_msg() -> PerformerMessage:
    return PerformerMessage(action="status", session_id="sid", payload={})


_ASSESSMENT_REPORT = {
    "assessment": {
        "ready": True,
        "goal": "Enable user registration",
        "expected_behavior": "Users can create accounts",
        "out_of_scope": [],
        "questions": [],
        "assumptions": [],
        "criteria": [],
        "criteria_source": "card",
        "clarifications": [],
        "assessment_hash": "a" * 64,
    },
    "gate_record": {"questions_kept": 0, "questions_dropped_by_cap": [], "questions_dropped_as_answered": [], "answers_matched": [], "questions_turned_to_assumptions": [], "round_count": 1},
    "write_free_check": {"passed": True, "commands_run": 0, "has_command_runner": False},
    "workflow_metrics": {"model_calls": 1, "step_durations_ms": {"intake": 1}},
}


@pytest.mark.asyncio
async def test_an_assessment_report_is_returned_and_nothing_is_committed():
    perf = _perf()
    perf.backend.get_status.return_value = BackendStatus(state="done", output=json.dumps(_ASSESSMENT_REPORT))
    settings = Settings(AGENT_BACKEND="codex", AGENT_TIMEOUT=1800)

    with patch("performer.main.commit_file", new=AsyncMock()) as commit:
        resp = await handle_status(_status_msg(), perf, settings)

    assert resp.status == "assessment_complete"
    assert resp.report == _ASSESSMENT_REPORT
    commit.assert_not_called()
    assert perf.state == "assessment_complete"


@pytest.mark.asyncio
async def test_not_ready_assessment_report_is_returned_and_nothing_is_committed():
    """FR-011: a not-ready assessment reports `blocked` with the gate's
    questions, the same shape the prose assessor returns, commits nothing,
    and still carries the report."""
    perf = _perf()
    blocked_report = {
        "assessment": {
            "ready": False,
            "goal": "Make page better",
            "expected_behavior": "Looks nice",
            "out_of_scope": [],
            "questions": ["What audience?"],
            "assumptions": [],
            "criteria": [],
            "criteria_source": "card",
            "clarifications": [],
            "assessment_hash": "b" * 64,
        },
        "gate_record": {"questions_kept": 1, "questions_dropped_by_cap": [], "questions_dropped_as_answered": [], "answers_matched": [], "questions_turned_to_assumptions": [], "round_count": 1},
        "write_free_check": {"passed": True, "commands_run": 0, "has_command_runner": False},
        "workflow_metrics": {"model_calls": 1, "step_durations_ms": {"intake": 1}},
    }
    perf.backend.get_status.return_value = BackendStatus(state="done", output=json.dumps(blocked_report))
    settings = Settings(AGENT_BACKEND="codex", AGENT_TIMEOUT=1800)

    with patch("performer.main.commit_file", new=AsyncMock()) as commit:
        resp = await handle_status(_status_msg(), perf, settings)

    assert resp.status == "blocked"
    assert resp.questions == ["What audience?"]
    assert resp.report == blocked_report
    assert perf.state == "blocked" and perf.open_questions == ["What audience?"]
    commit.assert_not_called()


@pytest.mark.asyncio
async def test_prose_json_still_takes_the_assessment_md_path():
    """Prose JSON (no workflow report shape) goes down the legacy path and commits."""
    perf = _perf()
    perf.backend.get_status.return_value = BackendStatus(state="done", output='{"sufficient": true, "questions": []}')
    settings = Settings(AGENT_BACKEND="codex", AGENT_TIMEOUT=1800)

    with patch("performer.main.commit_file", new=AsyncMock()) as commit:
        resp = await handle_status(_status_msg(), perf, settings)

    assert resp.status == "assessment_complete"
    assert commit.await_count >= 1


@pytest.mark.asyncio
async def test_json_without_an_assessment_is_not_mistaken_for_the_workflow():
    """A JSON blob without the assessment key from the prose assessor still goes
    down the prose path (and is committed as the assessment text, as before)."""
    perf = _perf()
    perf.backend.get_status.return_value = BackendStatus(state="done", output=json.dumps({"sufficient": True}))
    settings = Settings(AGENT_BACKEND="codex", AGENT_TIMEOUT=1800)

    with patch("performer.main.commit_file", new=AsyncMock()) as commit:
        resp = await handle_status(_status_msg(), perf, settings)

    assert resp.status == "assessment_complete"
    assert commit.await_count >= 1


@pytest.mark.asyncio
async def test_schema_violation_error_is_not_assessment_complete():
    """When the adapter reports a schema violation error, the response is not
    assessment_complete and nothing is committed."""
    perf = _perf()
    error_reason = "BACKEND_FORMAT_ERROR: SchemaViolation: schema still unmet after one reprompt"
    perf.backend.get_status.return_value = BackendStatus(state="error", error_reason=error_reason)
    settings = Settings(AGENT_BACKEND="codex", AGENT_TIMEOUT=1800)

    with patch("performer.main.commit_file", new=AsyncMock()) as commit:
        resp = await handle_status(_status_msg(), perf, settings)

    assert resp.status != "assessment_complete"
    commit.assert_not_called()
    assert perf.state != "assessment_complete"

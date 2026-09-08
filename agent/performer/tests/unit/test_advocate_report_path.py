"""Spec 173 US1: the advocate's terminal branch in handle_status.

The trap this file exists for: the role cascade in handle_status ends in the
implementer tail, which lints, pushes the branch and opens a pull request. The
comment above that tail states that every role before now returns earlier. A
new role added without its own branch would silently open pull requests from a
run that only reads issues and comments.
"""
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


def _perf(role: str = "advocate") -> Performance:
    stand = Stand(path=Path("/tmp/fake"), branch="advocate/s")
    stand.git_env = {}
    score = Score(title="t", repo_url="https://github.com/o/r", branch="advocate/s", role=role)
    return Performance(session_id="sid", stand=stand, score=score, backend=MagicMock(), role=role)


def _record(**over: object) -> dict:
    base = {
        "verdict": "advocate_complete",
        "issues_seen": 2,
        "documents_read": ["README.md"],
        "outcomes": [{"issue_id": "I_1", "action": "replied", "label_applied": "advocate-handled",
                      "comment_posted": True, "model_calls": 1}],
        "withheld": [],
        "model_calls": 1,
        "write_free_check": "",
    }
    base.update(over)
    return base


async def _status(perf: Performance, payload: dict) -> object:
    perf.backend.get_status.return_value = BackendStatus(state="done", output=json.dumps(payload))
    return await handle_status(
        PerformerMessage(action="status", session_id="sid", payload={}),
        perf,
        Settings(AGENT_BACKEND="codex", AGENT_TIMEOUT=1800),
    )


@pytest.mark.asyncio
async def test_a_completed_run_reports_its_own_terminal_status() -> None:
    resp = await _status(_perf(), {"advocate": _record()})
    assert resp.status == "advocate_complete"
    assert resp.report["advocate"]["issues_seen"] == 2


@pytest.mark.asyncio
async def test_a_held_run_reports_env_blocked() -> None:
    resp = await _status(_perf(), {"advocate": _record(verdict="env_blocked", error="github is down")})
    assert resp.status == "env_blocked"


@pytest.mark.asyncio
async def test_the_run_never_reaches_the_tail_that_opens_a_pull_request() -> None:
    """SC-003. The assertion is on the push and the PR call, not on the status,
    because a role can return the right status having already pushed."""
    with (
        patch("performer.main.push_branch", new=AsyncMock()) as push,
        patch("performer.main.create_pull_request", new=AsyncMock()) as pr,
    ):
        resp = await _status(_perf(), {"advocate": _record()})
    assert resp.status == "advocate_complete"
    assert not push.called, "an advocate run must never push a branch"
    assert not pr.called, "an advocate run must never open a pull request"


@pytest.mark.asyncio
async def test_a_held_run_also_pushes_nothing() -> None:
    with (
        patch("performer.main.push_branch", new=AsyncMock()) as push,
        patch("performer.main.create_pull_request", new=AsyncMock()) as pr,
    ):
        await _status(_perf(), {"advocate": _record(verdict="env_blocked", error="x")})
    assert not push.called and not pr.called


@pytest.mark.asyncio
async def test_a_report_without_a_known_verdict_is_not_treated_as_a_run() -> None:
    """A dict under the key is not a workflow report. Requiring the verdict and
    the shape keeps a malformed payload out of the terminal path."""
    with (
        patch("performer.main.push_branch", new=AsyncMock()),
        patch("performer.main.create_pull_request", new=AsyncMock()),
    ):
        resp = await _status(_perf(), {"advocate": {"verdict": "nonsense"}})
    assert resp.status != "advocate_complete"


@pytest.mark.asyncio
async def test_a_malformed_report_errors_rather_than_falling_through_to_the_tail() -> None:
    """There is no prose path for this role, so a fall-through lands on the
    implementer tail. An unusable report must be an error, not a pull request."""
    with (
        patch("performer.main.push_branch", new=AsyncMock()) as push,
        patch("performer.main.create_pull_request", new=AsyncMock()) as pr,
    ):
        resp = await _status(_perf(), {"advocate": {"verdict": "advocate_complete"}})
    assert resp.status == "error"
    assert not push.called and not pr.called


@pytest.mark.asyncio
async def test_a_run_reporting_nothing_at_all_errors_too() -> None:
    with (
        patch("performer.main.push_branch", new=AsyncMock()) as push,
        patch("performer.main.create_pull_request", new=AsyncMock()) as pr,
    ):
        resp = await _status(_perf(), {"something_else": True})
    assert resp.status == "error"
    assert not push.called and not pr.called


@pytest.mark.asyncio
async def test_a_report_missing_its_required_fields_is_not_treated_as_a_run() -> None:
    with (
        patch("performer.main.push_branch", new=AsyncMock()),
        patch("performer.main.create_pull_request", new=AsyncMock()),
    ):
        resp = await _status(_perf(), {"advocate": {"verdict": "advocate_complete"}})
    assert resp.status != "advocate_complete"

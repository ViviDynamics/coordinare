"""Issue #396: the QA performer hands its evidence comment to coordinare.

The QA evidence comment on the work item is a work item write, so coordinare
posts it through its work item provider. A coordinare that does so says so on
the score; the performer then returns the comment it built instead of posting
it. A score without the flag comes from an older coordinare, which does not
post it, so the performer posts it itself as it always has: the comment is
posted once whichever side is newer.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from performer.backends.base import BackendStatus
from performer.config import Settings
from performer.main import handle_status
from performer.models import Performance, Score, Stand
from performer.protocol import PerformerMessage, PerformerResponse


def _perf(*, by_coordinare: bool, issue_number: int = 89, work_item_ref: str = "") -> Performance:
    stand = Stand(path=Path("/tmp/fake"), branch="feat/test")
    stand.git_env = {}
    score = Score(
        title="Test card",
        repo_url="https://github.com/acme/repo",
        branch="feat/test",
        issue_number=issue_number,
        work_item_ref=work_item_ref,
        work_item_comments_by_coordinare=by_coordinare,
    )
    perf = Performance(session_id="sid", stand=stand, score=score, backend=MagicMock(), role="qa")
    perf.pr_url = "https://github.com/acme/repo/pull/42"
    return perf


def _output(failures: list[dict] | None = None) -> str:
    return json.dumps({
        "failures": failures or [],
        "criteria_checked": 1,
        "criteria_passed": 0 if failures else 1,
        "verification_steps": ["Open page and verify copied text."],
        "executed_checks": [{"command": "pytest -q", "exit_code": 1 if failures else 0, "output": "1 passed"}],
    })


async def _run(perf: Performance, output: str) -> tuple[PerformerResponse, AsyncMock, AsyncMock]:
    perf.backend.get_status.return_value = BackendStatus(state="done", output=output)
    pr_comment, issue_comment = AsyncMock(return_value={}), AsyncMock(return_value={})
    with (
        patch("performer.main.commit_file", new=AsyncMock()),
        patch("performer.main.post_pr_comment", new=pr_comment),
        patch("performer.main.post_issue_comment", new=issue_comment),
    ):
        resp = await handle_status(
            PerformerMessage(action="status", session_id="sid"), perf, Settings(AGENT_BACKEND="opencode"),
        )
    return resp, pr_comment, issue_comment


@pytest.mark.asyncio
async def test_the_comment_is_returned_and_not_posted_when_coordinare_posts_it() -> None:
    resp, pr_comment, issue_comment = await _run(_perf(by_coordinare=True), _output())

    assert resp.status == "qa_passed"
    issue_comment.assert_not_called()
    # The pull request comment is a code-host write and stays with the performer.
    pr_comment.assert_called_once()
    assert resp.work_item_comment == pr_comment.call_args.kwargs["body"]
    assert "QA Evidence" in resp.work_item_comment
    assert "Open page and verify copied text." in resp.work_item_comment


@pytest.mark.asyncio
async def test_a_failing_run_returns_its_comment_too() -> None:
    failure = {"criterion": "copies", "expected": "copied", "actual": "nothing", "category": "defect"}
    resp, pr_comment, issue_comment = await _run(_perf(by_coordinare=True), _output([failure]))

    assert resp.status == "qa_failed"
    issue_comment.assert_not_called()
    assert resp.work_item_comment == pr_comment.call_args.kwargs["body"]


@pytest.mark.asyncio
async def test_an_older_coordinare_still_gets_the_comment_posted_by_the_performer() -> None:
    resp, pr_comment, issue_comment = await _run(_perf(by_coordinare=False), _output())

    issue_comment.assert_called_once()
    assert issue_comment.call_args.args[2] == 89
    assert issue_comment.call_args.kwargs["body"] == pr_comment.call_args.kwargs["body"]
    assert resp.work_item_comment is None


@pytest.mark.asyncio
async def test_an_item_with_no_github_issue_gets_its_comment_through_coordinare() -> None:
    resp, _, issue_comment = await _run(_perf(by_coordinare=True, issue_number=0, work_item_ref="PAY-7"), _output())

    issue_comment.assert_not_called()
    assert resp.work_item_comment


@pytest.mark.asyncio
async def test_no_work_item_means_no_comment() -> None:
    resp, _, issue_comment = await _run(_perf(by_coordinare=True, issue_number=0), _output())

    issue_comment.assert_not_called()
    assert resp.work_item_comment is None


@pytest.mark.asyncio
async def test_a_settled_status_does_not_carry_the_comment_again() -> None:
    """Coordinare posts what the first terminal status carries, so it is posted once."""
    perf = _perf(by_coordinare=True)
    first, _, _ = await _run(perf, _output())
    again, _, _ = await _run(perf, _output())

    assert first.work_item_comment
    assert again.status == first.status
    assert again.work_item_comment is None


def test_the_field_survives_the_wire() -> None:
    resp = PerformerResponse(status="qa_passed", work_item_comment="## QA Evidence")
    assert json.loads(resp.model_dump_json(exclude_none=True))["work_item_comment"] == "## QA Evidence"

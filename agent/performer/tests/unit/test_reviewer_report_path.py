"""Spec 169 FR-011, FR-015: main.py maps the reviewer workflow's review record
onto the response the prose path returns for that verdict and skips the prose
post (the workflow already posted the one review). Without the report key the
prose path is untouched."""
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
    score = Score(title="Test card", repo_url="https://github.com/acme/repo", branch="feat/test", issue_number=7, pr_url="https://github.com/acme/repo/pull/12")
    perf = Performance(session_id="sid", stand=stand, score=score, backend=MagicMock(), role="reviewing")
    perf.pr_url = "https://github.com/acme/repo/pull/12"
    return perf


def _msg() -> PerformerMessage:
    return PerformerMessage(action="status", session_id="sid", payload={})


def _finding(**over):
    base = {"path": "src/a.py", "line": 3, "category": "logic_error", "problem": "off by one", "why_blocking": "wrong result", "evidence": "range(n)", "origin": "model"}
    base.update(over)
    return base


def _report(verdict: str, findings=None, **over):
    review = {
        "changed_files": [{"path": "src/a.py", "hunks": [{"header": "@@ -1 +1,5 @@", "start_line": 1, "end_line": 5, "lines": []}], "fully_in_diff": True, "opened_by_survey": False}],
        "diff_truncated": False, "unread_files": over.pop("unread_files", []), "survey_commands": [], "survey_refusals": [],
        "findings": findings or [], "findings_before_gate": findings or [], "findings_dropped": [], "findings_after_anchor_recheck": [],
        "dispositions": [], "coverage_pass_ran": False, "coverage_pass_output": "", "verdict": verdict, "covered_files": ["src/a.py"],
        "post_error": over.pop("post_error", None), "posted_review_url": over.pop("posted_review_url", "https://github.com/acme/repo/pull/12#r1"), "workflow_metrics": {},
    }
    return {"review": review, "write_free_check": {"passed": True}, "workflow_metrics": {"step_durations_ms": {"intake": 1}}}


async def _handle(output: str, cycle: int = 0):
    perf = _perf()
    perf.review_cycle = cycle
    perf.backend.get_status.return_value = BackendStatus(state="done", output=output)
    settings = Settings(AGENT_BACKEND="codex", AGENT_TIMEOUT=1800)
    with (
        patch("performer.main.post_pull_request_review", new=AsyncMock(return_value={})) as post,
        patch("performer.main.resolve_pr_review_threads", new=AsyncMock(return_value=0)) as resolve,
    ):
        resp = await handle_status(_msg(), perf, settings)
    return perf, resp, post, resolve


@pytest.mark.asyncio
async def test_approved_verdict_reports_approved_without_a_second_post_or_thread_resolution():
    perf, resp, post, resolve = await _handle(json.dumps(_report("approved")))
    assert resp.status == "approved" and perf.state == "approved"
    assert resp.report["review"]["verdict"] == "approved"
    assert not post.called, "the workflow posted the review already"
    assert not resolve.called, "FR-010: no thread is resolved"


@pytest.mark.asyncio
async def test_changes_requested_carries_the_findings_as_comments_and_the_report():
    perf, resp, post, _ = await _handle(json.dumps(_report("changes_requested", [_finding()])))
    assert resp.status == "changes_requested" and perf.state == "changes_requested"
    assert resp.comments == [{"path": "src/a.py", "line": 3, "body": "logic_error: off by one Why blocking: wrong result"}]
    assert resp.report["review"]["findings"][0]["evidence"] == "range(n)"
    assert perf.review_cycle == 1 and not post.called


@pytest.mark.asyncio
async def test_the_review_cycle_limit_still_blocks():
    perf, resp, _, _ = await _handle(json.dumps(_report("changes_requested", [_finding()])), cycle=2)
    assert resp.status == "blocked" and perf.state == "blocked"
    assert "cycle limit" in resp.questions[0]


@pytest.mark.asyncio
async def test_a_hold_names_the_post_error_or_the_unread_files():
    perf, resp, _, _ = await _handle(json.dumps(_report("env_blocked", [_finding()], post_error="posting the review failed: 502", posted_review_url=None)))
    assert resp.status == "env_blocked" and perf.state == "env_blocked" and "502" in resp.reason
    _, resp2, _, _ = await _handle(json.dumps(_report("env_blocked", unread_files=["src/z.py"], posted_review_url=None)))
    assert resp2.status == "env_blocked" and "src/z.py" in resp2.reason


@pytest.mark.asyncio
async def test_output_without_the_report_key_takes_the_prose_path():
    perf, resp, post, _ = await _handle(json.dumps({"approved": False, "comments": [{"path": "src/a.py", "line": 3, "body": "fix"}], "body": "please fix"}))
    assert resp.status == "changes_requested"
    assert post.called, "the prose path posts the review itself, byte for byte as before"
    assert post.call_args.kwargs["event"] == "COMMENT"

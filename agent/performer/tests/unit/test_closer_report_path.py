"""Spec 172 FR-010, FR-011: main.py maps the closer workflow's record onto the
statuses coordinare handles today and skips the prose path, including its
resolve-everything call. Without a known verdict the prose path is untouched."""
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
    perf = Performance(session_id="sid", stand=stand, score=score, backend=MagicMock(), role="closing_review")
    perf.pr_url = "https://github.com/acme/repo/pull/12"
    return perf


def _msg() -> PerformerMessage:
    return PerformerMessage(action="status", session_id="sid", payload={})


def _report(verdict="approved", open_threads=None, resolved=None, **over):
    closing = {"head_sha": "abc1234", "threads_read": 2, "pages_read": 1,
               "classifications": [{"thread_id": "t1", "state": "resolved", "rule": "is_resolved"}],
               "judgements": [], "resolved": resolved or [], "open_threads": open_threads or [],
               "verdict": verdict, "hold_reason": over.pop("hold_reason", None),
               "posted_review_url": "https://github.com/acme/repo/pull/12#review-1", "workflow_metrics": {"model_calls": 0}}
    return {"closing": closing, "workflow_metrics": {"step_durations_ms": {"intake": 5}}}


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
async def test_approved_skips_the_prose_post_and_the_resolve_everything_call():
    perf, resp, post, resolve = await _handle(json.dumps(_report("approved", resolved=[{"thread_id": "t1", "reason": "outdated"}])))
    assert resp.status == "approved" and perf.state == "approved"
    assert resp.report["closing"]["verdict"] == "approved"
    assert not post.called, "the workflow posted its own review"
    assert not resolve.called, "the workflow resolved exactly what it judged; a blanket resolve would close open threads"


@pytest.mark.asyncio
async def test_changes_requested_names_the_open_threads():
    open_threads = [{"thread_id": "t2", "path": "src/app.py", "line": 10, "excerpt": "needs a guard"}]
    perf, resp, post, _ = await _handle(json.dumps(_report("changes_requested", open_threads=open_threads)))
    assert resp.status == "changes_requested" and perf.state == "changes_requested"
    assert resp.comments == [{"path": "src/app.py", "line": 10, "body": "unresolved review thread: needs a guard"}]
    assert perf.review_cycle == 1 and not post.called


@pytest.mark.asyncio
async def test_the_cycle_limit_still_blocks():
    open_threads = [{"thread_id": "t2", "path": "src/app.py", "line": 10, "excerpt": "needs a guard"}]
    perf, resp, _, _ = await _handle(json.dumps(_report("changes_requested", open_threads=open_threads)), cycle=2)
    assert resp.status == "blocked" and perf.state == "blocked" and "cycle limit" in resp.questions[0]


@pytest.mark.asyncio
async def test_a_hold_names_the_reason():
    perf, resp, _, _ = await _handle(json.dumps(_report("env_blocked", hold_reason="could not read the review threads: 502")))
    assert resp.status == "env_blocked" and perf.state == "env_blocked" and "502" in resp.reason


@pytest.mark.asyncio
async def test_a_closing_dict_without_a_known_verdict_takes_the_prose_path():
    perf, resp, post, _ = await _handle(json.dumps({"closing": {"notes": "all good"}, "approved": True, "body": "looks fine"}))
    assert resp.status == "approved" and post.called, "the prose path posts its own review"


@pytest.mark.asyncio
async def test_output_without_the_report_key_takes_the_prose_path():
    perf, resp, post, resolve = await _handle(json.dumps({"approved": True, "body": "threads resolved"}))
    assert resp.status == "approved" and post.called and resolve.called, "the prose path resolves every thread as before"

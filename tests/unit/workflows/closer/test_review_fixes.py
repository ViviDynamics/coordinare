"""Pinning tests for the spec-172 adversarial review findings (all confirmed by execution)."""
from __future__ import annotations

import json

import pytest
from performer.workflows.closer.classify import is_answered
from performer.workflows.closer.gate import to_resolve
from performer.workflows.closer.models import Classification, Judgement, Thread

from tests.unit.workflows.closer._fakes import comment, thread


def _t(*comments, **over) -> Thread:
    return Thread.model_validate(thread("t1", *comments, **over))


# classify timestamps: mutation = compare the strings again
def test_timestamps_are_compared_as_moments_not_strings():
    """Live-shaped case: a reply at 09:30Z is later than a question at 10:00+02:00."""
    ask = comment("reviewer", "please guard this", "2026-01-01T10:00:00+02:00")
    reply = comment("implementer", "guarded in abc123", "2026-01-01T09:30:00Z")
    assert is_answered(_t(ask, reply)), "09:30Z is 90 minutes after 08:00Z"
    earlier = comment("implementer", "before the question", "2026-01-01T07:00:00Z")
    assert not is_answered(_t(ask, earlier)), "a reply that predates the question is not an answer"
    same = comment("implementer", "same instant", "2026-01-01T10:00:00+02:00")
    assert is_answered(_t(ask, same)), "equal moments count"
    junk = comment("implementer", "unparseable", "not a timestamp")
    assert isinstance(is_answered(_t(ask, junk)), bool), "an unparseable timestamp must not raise"


# to_resolve: mutation = drop the dedup
def test_a_thread_is_resolved_once_even_when_two_rules_claim_it():
    classifications = [Classification(thread_id="t1", state="stale", rule="is_stale")]
    judgements = [Judgement(thread_id="t1", addressed=True, quote="fixed it", reason="", accepted=True)]
    out = to_resolve(classifications, judgements)
    assert [tid for tid, _r in out] == ["t1"] and out[0][1].startswith("outdated"), "the first rule wins, once"


# github _first_author: mutation = restore dict.get with a default
def test_a_null_author_still_logs_as_unknown():
    """The refactor turned a null author into "", which is a present key, so the
    historical "unknown" default stopped applying."""
    import pathlib

    import performer.github as gh

    src = (gh.__file__ or "")
    assert src, "module path"
    text = pathlib.Path(src).read_text()
    assert 'comments[0].get("author") or "unknown"' in text


# main.py: mutation = accept a record with only a verdict
@pytest.mark.asyncio
async def test_a_closing_record_missing_its_required_fields_takes_the_prose_path():
    from pathlib import Path
    from unittest.mock import AsyncMock, MagicMock, patch

    from performer.backends.base import BackendStatus
    from performer.config import Settings
    from performer.main import Performance, handle_status
    from performer.models import Score, Stand
    from performer.protocol import PerformerMessage

    stand = Stand(path=Path("/tmp/fake"), branch="b")
    stand.git_env = {}
    score = Score(title="t", repo_url="https://github.com/o/r", branch="b", issue_number=1, pr_url="https://github.com/o/r/pull/3")
    perf = Performance(session_id="sid", stand=stand, score=score, backend=MagicMock(), role="closing_review")
    perf.pr_url = "https://github.com/o/r/pull/3"
    perf.backend.get_status.return_value = BackendStatus(state="done", output=json.dumps({"closing": {"verdict": "approved"}, "approved": True, "body": "fine"}))
    with (
        patch("performer.main.post_pull_request_review", new=AsyncMock(return_value={})) as post,
        patch("performer.main.resolve_pr_review_threads", new=AsyncMock(return_value=0)),
    ):
        resp = await handle_status(PerformerMessage(action="status", session_id="sid", payload={}), perf, Settings(AGENT_BACKEND="codex", AGENT_TIMEOUT=1800))
    assert resp.status == "approved" and post.called, "a record without threads_read or classifications is not a workflow report"


# the post step logs like every other step
def test_the_post_step_logs_its_outcome():
    import inspect

    from performer.workflows import closer as mod

    assert 'log.info("closer.post"' in inspect.getsource(mod)

"""172 end-to-end scenarios against a fake GitHub and a stubbed model: a clean card
makes no model call, an answered thread is resolved with its quote, an unanswered
thread blocks, an outdated thread is resolved by rule, a hallucinated quote is
discarded, and every failure path holds without approving."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from performer.workflows.base import WorkflowMetrics
from performer.workflows.budget import ModelReply
from performer.workflows.closer import STATES, CloserWorkflow
from performer.workflows.closer.models import ClosingRecord
from performer.workflows.toolkit import Toolkit

from tests.unit.workflows.closer._fakes import FakeGitHub, comment, score, thread

ASK = comment("reviewer", "This needs a guard for the empty case.", "2026-09-07T10:00:00Z")
REPLY = comment("implementer", "Added the guard in commit abc123; it returns early when the list is empty.", "2026-09-07T11:00:00Z")
CONFIRM = comment("reviewer", "Confirmed, works now", "2026-09-07T12:00:00Z")  # the raiser's sign-off: the quote authority


def _toolkit(judgements=None):
    calls = {"n": 0, "personas": []}

    async def model_call(persona, content, max_tokens):
        calls["n"] += 1
        calls["personas"].append(persona)
        return ModelReply(content=json.dumps({"judgements": judgements or []}), finish_reason="stop")

    events = []
    return Toolkit(metrics=WorkflowMetrics(), model_call=model_call, command_runner=None, event_sink=events.append, call_limit=8), events, calls


async def _run(threads, judgements=None, *, gh=None, **gh_kwargs):
    gh = gh or FakeGitHub(threads, **gh_kwargs)
    tk, events, calls = _toolkit(judgements)
    result = await CloserWorkflow(fetcher=gh.fetcher, resolver=gh.resolver, poster=gh.poster).run(SimpleNamespace(path="/tmp/x"), score(), tk)
    return ClosingRecord.model_validate(result.report["closing"]), result, gh, events, calls


@pytest.mark.asyncio
async def test_a_card_whose_threads_are_all_resolved_passes_with_no_model_call():
    record, result, gh, events, calls = await _run([thread("t1", ASK, resolved=True), thread("t2", ASK, resolved=True)])
    assert record.verdict == "approved" and calls["n"] == 0 and result.metrics.model_calls == 0
    assert [c.state for c in record.classifications] == ["resolved", "resolved"]
    assert gh.resolved == [] and [r["event"] for r in gh.reviews] == ["COMMENT"]
    assert "Every review thread is resolved" in gh.reviews[0]["body"]
    steps = [e.text for e in events if e.text.startswith("closer.")]
    assert steps == ["closer.intake", "closer.classify", "closer.gate", "closer.post", "closer.report"], steps
    assert set(STATES) >= {s.split(".", 1)[1] for s in steps}
    assert record.posted_review_url.endswith("review-1") and record.head_sha == "abc1234"


@pytest.mark.asyncio
async def test_a_pull_request_with_no_threads_passes():
    record, _r, gh, _e, calls = await _run([])
    assert record.verdict == "approved" and record.threads_read == 0 and calls["n"] == 0 and gh.resolved == []


@pytest.mark.asyncio
async def test_an_answered_thread_is_judged_then_resolved_with_its_quote():
    judgements = [{"thread_id": "t1", "addressed": True, "quote": "Confirmed, works now", "reason": ""}]
    record, _r, gh, _e, calls = await _run([thread("t1", ASK, REPLY, CONFIRM)], judgements)
    assert calls["n"] == 1 and "thread t1 at src/app.py:10" in calls["personas"][0]
    assert record.verdict == "approved" and gh.resolved == ["t1"]
    assert record.resolved[0]["thread_id"] == "t1" and "Confirmed" in record.resolved[0]["reason"]
    assert record.judgements[0].accepted and record.judgements[0].discard_reason is None
    assert "Resolved by this run (1)" in gh.reviews[0]["body"]


@pytest.mark.asyncio
async def test_a_quote_that_appears_nowhere_in_the_thread_is_discarded():
    judgements = [{"thread_id": "t1", "addressed": True, "quote": "I rewrote the whole module", "reason": ""}]
    record, _r, gh, _e, _c = await _run([thread("t1", ASK, REPLY)], judgements)
    assert record.verdict == "changes_requested" and gh.resolved == []
    j = record.judgements[0]
    assert not j.accepted and j.discard_reason == "quote_not_found"
    assert [t["thread_id"] for t in record.open_threads] == ["t1"]
    assert "1 review thread(s) still open" in gh.reviews[0]["body"] and "`src/app.py:10`" in gh.reviews[0]["body"]


@pytest.mark.asyncio
async def test_a_not_addressed_judgement_keeps_the_thread_open():
    judgements = [{"thread_id": "t1", "addressed": False, "quote": "", "reason": "the reply promises a later fix"}]
    record, _r, gh, _e, _c = await _run([thread("t1", ASK, REPLY)], judgements)
    assert record.verdict == "changes_requested" and gh.resolved == []
    assert record.judgements[0].accepted and "later fix" in record.judgements[0].reason


@pytest.mark.asyncio
async def test_an_unanswered_thread_never_reaches_the_model_and_blocks():
    record, _r, gh, _e, calls = await _run([thread("t1", ASK)])
    assert calls["n"] == 0, "a thread with no reply is open by rule"
    assert record.verdict == "changes_requested" and gh.resolved == []
    assert [c.state for c in record.classifications] == ["open"]


@pytest.mark.asyncio
async def test_an_outdated_thread_is_resolved_by_rule_without_the_model():
    record, _r, gh, _e, calls = await _run([thread("t1", ASK, outdated=True)])
    assert calls["n"] == 0 and record.verdict == "approved"
    assert [c.state for c in record.classifications] == ["stale"]
    assert gh.resolved == ["t1"] and "outdated" in record.resolved[0]["reason"]


@pytest.mark.asyncio
async def test_a_judgement_for_a_thread_that_was_not_sent_is_discarded():
    judgements = [{"thread_id": "t1", "addressed": True, "quote": "Confirmed, works now", "reason": ""},
                  {"thread_id": "ghost", "addressed": True, "quote": "anything", "reason": ""}]
    record, _r, gh, _e, _c = await _run([thread("t1", ASK, REPLY, CONFIRM)], judgements)
    ghost = next(j for j in record.judgements if j.thread_id == "ghost")
    assert not ghost.accepted and ghost.discard_reason == "thread_not_sent"
    assert gh.resolved == ["t1"] and record.verdict == "approved"


@pytest.mark.asyncio
async def test_a_thread_the_model_did_not_judge_stays_open():
    judgements = [{"thread_id": "t1", "addressed": True, "quote": "Confirmed, works now", "reason": ""}]
    record, _r, gh, _e, _c = await _run([thread("t1", ASK, REPLY, CONFIRM), thread("t2", ASK, REPLY, CONFIRM, path="src/db.py")], judgements)
    assert record.verdict == "changes_requested" and [t["thread_id"] for t in record.open_threads] == ["t2"]
    assert gh.resolved == ["t1"], "the accepted thread still resolves even though the verdict fails"


@pytest.mark.asyncio
async def test_a_failed_fetch_holds_before_anything_else():
    record, _r, gh, _e, calls = await _run([], gh=FakeGitHub([], fetch_error="502 from GitHub"))
    assert record.verdict == "env_blocked" and "502" in record.hold_reason
    assert calls["n"] == 0 and gh.reviews == [] and gh.resolved == []


@pytest.mark.asyncio
async def test_a_failed_post_holds_and_resolves_nothing():
    record, _r, gh, _e, _c = await _run([], gh=FakeGitHub([thread("t1", ASK, outdated=True)], post_error="422 Unprocessable"))
    assert record.verdict == "env_blocked" and "422" in record.hold_reason, "the failed post holds the card"
    assert gh.resolved == ["t1"], "act runs before post: the thread is resolved, but the failed post still blocks the card"


@pytest.mark.asyncio
async def test_a_failed_resolution_holds_rather_than_approving():
    """FR-009: a card must never advance carrying a thread the closer believed closed."""
    record, _r, gh, _e, _c = await _run([], gh=FakeGitHub([thread("t1", ASK, outdated=True)], resolve_failures={"t1"}))
    assert record.verdict == "env_blocked" and "could not resolve 1 thread" in record.hold_reason
    assert record.resolved == []
    assert len(gh.reviews) == 1 and "CHANGES REQUESTED" in gh.reviews[0]["body"], "the review names the thread that failed to resolve instead of approving"


@pytest.mark.asyncio
async def test_only_the_ambiguous_threads_reach_the_model():
    threads = [thread("resolved1", ASK, resolved=True), thread("stale1", ASK, outdated=True),
               thread("open1", ASK), thread("answered1", ASK, REPLY, CONFIRM, path="src/db.py")]
    judgements = [{"thread_id": "answered1", "addressed": True, "quote": "Confirmed, works now", "reason": ""}]
    record, _r, gh, _e, calls = await _run(threads, judgements)
    persona = calls["personas"][0]
    assert calls["n"] == 1 and "answered1" in persona
    for other in ("resolved1", "stale1", "open1"):
        assert other not in persona, other
    assert record.verdict == "changes_requested", "open1 still blocks"
    assert gh.resolved == ["stale1", "answered1"], "act runs before post: the closed threads resolve even though the verdict fails"


@pytest.mark.asyncio
async def test_every_step_is_timed_and_logged(monkeypatch):
    from performer.workflows import closer as closer_mod

    from tests.unit.workflows._fakelog import FakeLog

    fake = FakeLog()
    monkeypatch.setattr(closer_mod, "log", fake)
    judgements = [{"thread_id": "t1", "addressed": True, "quote": "Confirmed, works now", "reason": ""}]
    _record, result, _gh, _e, _c = await _run([thread("t1", ASK, REPLY, CONFIRM)], judgements)
    durations = result.metrics.step_durations_ms
    assert list(durations) == ["intake", "classify", "judge", "gate", "act", "post"], durations
    assert result.report["workflow_metrics"]["step_durations_ms"] == durations
    events = {e["event"]: e for e in fake.entries}
    assert events["closer.intake"]["threads"] == 1 and events["closer.gate"]["verdict"] == "approved"
    assert events["closer.act"]["resolved"] == 1

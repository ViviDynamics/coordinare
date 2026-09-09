"""165 US3: once per blueprint hash, only after architecting, only with a
documentation brief; outcomes recorded; failures never retried."""
from __future__ import annotations

import pytest

from coordinare.services.documenting_side import (
    build_card_context,
    classify_status,
    poll_to_completion,
    record_dispatched,
    record_result,
    should_dispatch,
)

_BP = {
    "summary": "s",
    "milestones": [{"goal": "g", "scope": ["a"], "done_when": "d"}],
    "modules": [{"path": "app/", "note": "n"}],
    "data_model": {"changes": []}, "interfaces": [], "risks": [],
    "criteria": [{"surface": "/", "action": "a", "expected": "e", "kind": "functional"}],
    "docs": [{"topic": "t", "location": "docs/wiki/x.md", "say": "s"}],
    "size": "small", "blueprint_hash": "h1", "created_at": "t",
}


def _session(stage="implementing", blueprint=_BP, side=None):
    return {"card_id": "c1", "performer_stage": stage, "blueprint": blueprint, "documenting_side": side, "workspace_branch": "coordinare/c1/x"}


def test_dispatch_wanted_after_architecting_with_docs():
    ok, reason = should_dispatch(_session())
    assert ok, reason


@pytest.mark.parametrize("stage", ["assessing", "architecting"])
def test_not_before_the_card_has_a_plan_to_implement(stage):
    ok, reason = should_dispatch(_session(stage=stage))
    assert not ok and stage in reason


def test_not_without_a_documentation_brief():
    assert should_dispatch(_session(blueprint={**_BP, "docs": []}))[0] is False
    assert should_dispatch(_session(blueprint=None))[0] is False


def test_once_per_blueprint_hash_whatever_the_outcome():
    for status in ("running", "done", "failed"):
        ok, reason = should_dispatch(_session(side={"status": status, "blueprint_hash": "h1"}))
        assert not ok and status in reason


def test_a_new_blueprint_hash_dispatches_again():
    ok, _ = should_dispatch(_session(blueprint={**_BP, "blueprint_hash": "h2"}, side={"status": "done", "blueprint_hash": "h1"}))
    assert ok


def test_card_context_carries_only_the_documentation_brief():
    ctx = build_card_context({"id": "c1", "title": "T", "body": "B", "issue_number": 7}, _session(),
                             persona="p", backend="codex", model_block={"model": "m"}, repo_url="https://x/y.git", base_branch="main")
    assert ctx["role"] == "documenting" and ctx["doc_mode"] == "update"
    assert set(ctx["documentation_brief"]) == {"summary", "docs", "modules"}
    assert "implementation_brief" not in ctx and "verification_brief" not in ctx
    assert ctx["branch"] == "coordinare/c1/x" and ctx["model"] == "m" and ctx["issue_number"] == 7


def test_records():
    s = _session()
    record_dispatched(s, blueprint_hash="h1", session_id="sid")
    assert s["documenting_side"]["status"] == "running" and s["documenting_side"]["session_id"] == "sid"
    record_result(s, status="done", head_sha="abc", reason=None)
    assert s["documenting_side"]["status"] == "done" and s["documenting_side"]["head_sha"] == "abc"
    assert s["documenting_side"]["blueprint_hash"] == "h1"


def test_classify_status():
    assert classify_status({"status": "docs_committed"}) == "done"
    assert classify_status({"status": "error"}) == "failed"
    assert classify_status({"status": "working"}) is None
    assert classify_status(None) is None


@pytest.mark.asyncio
async def test_poll_records_done_with_the_head_sha():
    s = _session(side={"status": "running", "blueprint_hash": "h1", "session_id": "sid"})
    answers = iter([{"status": "working"}, {"status": "working"}, {"status": "docs_committed", "head_sha": "deadbeef"}])
    slept = []

    async def check(sid):
        return next(answers)

    async def sleep(n):
        slept.append(n)

    out = await poll_to_completion(s, check, session_id="sid", interval_s=5, sleep=sleep)
    assert out == "done" and s["documenting_side"]["head_sha"] == "deadbeef"
    assert slept == [5, 5]


@pytest.mark.asyncio
async def test_poll_records_failure_and_its_reason():
    s = _session(side={"status": "running", "blueprint_hash": "h1"})

    async def check(sid):
        return {"status": "error", "reason": "tree_violation: app/x.rb"}

    out = await poll_to_completion(s, check, session_id="sid", sleep=lambda n: None)
    assert out == "failed" and "tree_violation" in s["documenting_side"]["result_reason"]


@pytest.mark.asyncio
async def test_poll_error_and_timeout_are_recorded_not_raised():
    s = _session(side={"status": "running", "blueprint_hash": "h1"})

    async def boom(sid):
        raise RuntimeError("gateway down")

    assert await poll_to_completion(s, boom, session_id="sid") == "failed"
    assert "gateway down" in s["documenting_side"]["result_reason"]

    s2 = _session(side={"status": "running", "blueprint_hash": "h1"})
    t = {"now": 0.0}

    async def working(sid):
        return {"status": "working"}

    async def sleep(n):
        t["now"] += n

    assert await poll_to_completion(s2, working, session_id="sid", interval_s=10, timeout_s=25, sleep=sleep, now=lambda: t["now"]) == "failed"
    assert "timed out" in s2["documenting_side"]["result_reason"]


class _Svc:
    def __init__(self, fail=False, statuses=None):
        self.fail, self.dispatched = fail, []
        self._statuses = iter(statuses or [{"status": "docs_committed", "head_sha": "abc"}])

    async def dispatch_card(self, ctx, workspace_info=None):
        self.dispatched.append(ctx)
        if self.fail:
            raise RuntimeError("endpoint down")
        return {"session_id": f"sid-{len(self.dispatched)}"}

    async def check_status(self, sid):
        return next(self._statuses)


async def _resolve(card_id, session):
    return ({"id": card_id, "role": "documenting"}, object())


@pytest.mark.asyncio
async def test_run_cycle_dispatches_once_per_hash_and_polls_to_done():
    from coordinare.services.documenting_side import run_cycle

    sessions = {"c1": _session(), "c2": _session(stage="architecting")}
    svc = _Svc()
    polls = []

    def spawn(coro):
        polls.append(coro)

    n = await run_cycle(sessions, svc=svc, resolve=_resolve, spawn=spawn)
    assert n == 1 and [c["id"] for c in svc.dispatched] == ["c1"]
    assert sessions["c1"]["documenting_side"]["status"] == "running"
    await polls[0]
    assert sessions["c1"]["documenting_side"]["status"] == "done"
    # a second cycle dispatches nothing for the same blueprint
    assert await run_cycle(sessions, svc=svc, resolve=_resolve, spawn=spawn) == 0


@pytest.mark.asyncio
async def test_run_cycle_records_a_dispatch_failure_and_does_not_retry():
    from coordinare.services.documenting_side import run_cycle

    sessions = {"c1": _session()}
    svc = _Svc(fail=True)
    assert await run_cycle(sessions, svc=svc, resolve=_resolve, spawn=lambda c: c.close()) == 0
    side = sessions["c1"]["documenting_side"]
    assert side["status"] == "failed" and "endpoint down" in side["result_reason"]
    assert await run_cycle(sessions, svc=svc, resolve=_resolve, spawn=lambda c: c.close()) == 0
    assert len(svc.dispatched) == 1


@pytest.mark.asyncio
async def test_run_cycle_skips_when_the_daemon_cannot_resolve_the_dispatch():
    from coordinare.services.documenting_side import run_cycle

    sessions = {"c1": _session()}

    async def no_repo(card_id, session):
        return None

    assert await run_cycle(sessions, svc=_Svc(), resolve=no_repo, spawn=lambda c: c.close()) == 0
    assert sessions["c1"]["documenting_side"] is None, "nothing recorded: it can be retried next cycle"


# --- review of #266: no side run beside a card that has no live lifecycle -------

@pytest.mark.parametrize("phase", ["blocked", "idle", "done", "completed", "closed", "merging", "system_error"])
def test_not_while_the_card_is_inactive(phase):
    ok, reason = should_dispatch({**_session(), "phase": phase})
    assert not ok and phase in reason


@pytest.mark.parametrize("phase", ["monitoring_performer", "dispatching", "recovery", "", None])
def test_live_phases_still_dispatch(phase):
    ok, _ = should_dispatch({**_session(), "phase": phase})
    assert ok

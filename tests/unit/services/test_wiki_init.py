"""Unit tests for WikiInitService (spec 124 US3)."""
from __future__ import annotations

from pathlib import Path

import pytest

from coordinare.models.env_cache import EnvCacheState
from coordinare.models.notification import EventType
from coordinare.services.wiki_init import WikiInitService


def _ec(**kw) -> EnvCacheState:
    base = {"symphony_name": "s", "sanitised_name": "s", "cache_dir": Path("/tmp/c")}
    base.update(kw)
    return EnvCacheState(**base)


# --- needs_init / gate_holds -------------------------------------------------

def test_needs_init_false_when_disabled() -> None:
    assert WikiInitService(enabled=False).needs_init(_ec(), False) is False


def test_needs_init_true_when_enabled_and_wiki_absent() -> None:
    assert WikiInitService(enabled=True).needs_init(_ec(), wiki_present_on_default_branch=False) is True


def test_needs_init_false_when_wiki_present_on_branch() -> None:
    assert WikiInitService(enabled=True).needs_init(_ec(), wiki_present_on_default_branch=True) is False


def test_needs_init_false_when_in_flight_or_exhausted() -> None:
    svc = WikiInitService(enabled=True)
    assert svc.needs_init(_ec(wiki_in_flight=True), False) is False
    assert svc.needs_init(_ec(wiki_exhausted=True), False) is False


def test_gate_holds_until_initialized() -> None:
    svc = WikiInitService(enabled=True)
    assert svc.gate_holds(_ec()) is True
    assert svc.gate_holds(_ec(wiki_initialized=True)) is False


def test_gate_off_never_holds() -> None:
    assert WikiInitService(enabled=False).gate_holds(_ec()) is False


def test_gate_holds_when_exhausted_is_still_true() -> None:
    # Surfaced hold (operator must act) — never a silent deadlock (notify fired).
    assert WikiInitService(enabled=True).gate_holds(_ec(wiki_exhausted=True)) is True


# --- state transitions -------------------------------------------------------

def test_mark_initialized_sets_marker_and_clears_flight() -> None:
    svc = WikiInitService(enabled=True)
    ec = _ec(wiki_in_flight=True)
    svc.mark_initialized(ec)
    assert ec.wiki_initialized is True
    assert ec.wiki_in_flight is False
    assert ec.last_wiki_init_succeeded is True
    assert ec.last_wiki_init_at is not None


def test_register_failure_trips_breaker_at_budget() -> None:
    svc = WikiInitService(enabled=True, max_attempts=2)
    ec = _ec(wiki_in_flight=True)
    assert svc.register_failure(ec, "boom") is False
    assert ec.wiki_attempts == 1
    assert ec.wiki_in_flight is False
    assert ec.last_wiki_init_error == "boom"
    assert svc.register_failure(ec, "boom2") is True
    assert ec.wiki_exhausted is True


# --- auto-merge (scoped exception to human-only merge) -----------------------

class _Github:
    def __init__(self, merge, reviews, squash) -> None:
        self._merge, self._reviews, self._squash = merge, reviews, squash
        self.squash_called = False

    async def check_mergeability(self, pr_id):
        return self._merge

    async def get_pr_reviews(self, pr_id):
        return self._reviews

    async def squash_merge(self, pr_id):
        self.squash_called = True
        return self._squash


_BOT = "copilot-pull-request-reviewer[bot]"


@pytest.mark.asyncio
async def test_auto_merge_success_on_ci_green_and_bot_approval() -> None:
    gh = _Github(
        merge={"mergeable_raw": "MERGEABLE", "merge_state_status": "CLEAN"},
        reviews=[{"author_login": _BOT, "state": "APPROVED"}],
        squash={"merged": True},
    )
    merged, _reason = await WikiInitService(enabled=True).try_auto_merge(gh, "PR1", [_BOT], ec_state=_ec(wiki_in_flight=True))
    assert merged is True
    assert gh.squash_called is True


@pytest.mark.asyncio
async def test_auto_merge_blocked_without_bot_approval() -> None:
    gh = _Github(
        merge={"mergeable_raw": "MERGEABLE", "merge_state_status": "CLEAN"},
        reviews=[],
        squash={"merged": True},
    )
    merged, reason = await WikiInitService(enabled=True).try_auto_merge(gh, "PR1", [_BOT], ec_state=_ec(wiki_in_flight=True))
    assert merged is False
    assert "approval" in reason
    assert gh.squash_called is False  # never attempts merge without approval


@pytest.mark.asyncio
async def test_auto_merge_blocked_when_not_mergeable() -> None:
    gh = _Github(
        merge={"mergeable_raw": "CONFLICTING", "merge_state_status": "DIRTY"},
        reviews=[{"author_login": _BOT, "state": "APPROVED"}],
        squash={"merged": True},
    )
    merged, _reason = await WikiInitService(enabled=True).try_auto_merge(gh, "PR1", [_BOT], ec_state=_ec(wiki_in_flight=True))
    assert merged is False
    assert gh.squash_called is False


@pytest.mark.asyncio
async def test_auto_merge_rejects_unstable_ci() -> None:
    # review #2: mergeable_raw MERGEABLE but checks failing/pending (UNSTABLE)
    # must NOT merge — only CLEAN passes.
    gh = _Github(
        merge={"mergeable_raw": "MERGEABLE", "merge_state_status": "UNSTABLE"},
        reviews=[{"author_login": _BOT, "state": "APPROVED"}],
        squash={"merged": True},
    )
    merged, reason = await WikiInitService(enabled=True).try_auto_merge(
        gh, "PR1", [_BOT], ec_state=_ec(wiki_in_flight=True)
    )
    assert merged is False
    assert "CI-green" in reason
    assert gh.squash_called is False


@pytest.mark.asyncio
async def test_auto_merge_refused_when_no_wiki_init_in_flight() -> None:
    # review #3: never auto-merge when no wiki-init cycle is active (guards a
    # misrouted pr_node_id from bypassing the human-only merge gate).
    gh = _Github(
        merge={"mergeable_raw": "MERGEABLE", "merge_state_status": "CLEAN"},
        reviews=[{"author_login": _BOT, "state": "APPROVED"}],
        squash={"merged": True},
    )
    merged, reason = await WikiInitService(enabled=True).try_auto_merge(
        gh, "PR1", [_BOT], ec_state=_ec(wiki_in_flight=False)
    )
    assert merged is False
    assert "in flight" in reason
    assert gh.squash_called is False


@pytest.mark.asyncio
async def test_auto_merge_degrades_when_branch_protected() -> None:
    gh = _Github(
        merge={"mergeable_raw": "MERGEABLE", "merge_state_status": "CLEAN"},
        reviews=[{"author_login": _BOT, "state": "APPROVED"}],
        squash={"merged": False},  # branch protection requires a human
    )
    merged, reason = await WikiInitService(enabled=True).try_auto_merge(gh, "PR1", [_BOT], ec_state=_ec(wiki_in_flight=True))
    assert merged is False
    assert "branch protection" in reason


# --- notification ------------------------------------------------------------

@pytest.mark.asyncio
async def test_notify_blocked_dispatches_one_dedup_keyed_critical_event() -> None:
    events = []

    class _Notify:
        async def dispatch(self, e):
            events.append(e)

    ec = _ec(wiki_attempts=3, wiki_exhausted=True, last_wiki_init_error="boom")
    await WikiInitService(enabled=True).notify_blocked(_Notify(), "sym", "exhausted", ec)
    assert len(events) == 1
    assert events[0].event_type == EventType.wiki_init_exhausted
    assert events[0].dedup_key == "wiki_init_exhausted:sym"


@pytest.mark.asyncio
async def test_notify_blocked_noop_without_service() -> None:
    # Must not raise when no notification service is wired.
    await WikiInitService(enabled=True).notify_blocked(None, "sym", "exhausted", _ec())


# --- check_and_trigger (per-cycle decision) ----------------------------------

class _GhFile:
    def __init__(self, content=None, raise_=False) -> None:
        self._content, self._raise = content, raise_

    async def get_file_content(self, org, repo, path):
        if self._raise:
            raise RuntimeError("api error")
        return self._content


class _Collector:
    def __init__(self) -> None:
        self.events = []

    async def dispatch(self, e):
        self.events.append(e)


@pytest.mark.asyncio
async def test_check_and_trigger_dispatches_when_wiki_absent() -> None:
    ec = _ec()
    calls = []

    async def dispatch_fn(sym):
        calls.append(sym)

    await WikiInitService(enabled=True).check_and_trigger("s", ec, _GhFile(None), "o", "r", dispatch_fn)
    assert calls == ["s"]
    assert ec.wiki_in_flight is True


@pytest.mark.asyncio
async def test_check_and_trigger_noop_when_disabled() -> None:
    ec = _ec()
    calls = []

    async def dispatch_fn(sym):
        calls.append(sym)

    await WikiInitService(enabled=False).check_and_trigger("s", ec, _GhFile(None), "o", "r", dispatch_fn)
    assert calls == []
    assert ec.wiki_in_flight is False


@pytest.mark.asyncio
async def test_check_and_trigger_adopts_existing_wiki_without_dispatch() -> None:
    ec = _ec()
    calls = []

    async def dispatch_fn(sym):
        calls.append(sym)

    await WikiInitService(enabled=True).check_and_trigger(
        "s", ec, _GhFile('{"gitHead":"abc"}'), "o", "r", dispatch_fn
    )
    assert calls == []
    assert ec.wiki_initialized is True


@pytest.mark.asyncio
async def test_check_and_trigger_adopts_empty_marker_without_reinit() -> None:
    """An existing-but-empty docs/wiki/README.md is PRESENT (adopt), not absent.

    get_file_content returns "" for an empty file (only None means absent). A
    prior bug used bool(content), which treated "" as absent and would dispatch
    a redundant re-init.
    """
    ec = _ec()
    calls = []

    async def dispatch_fn(sym):
        calls.append(sym)

    await WikiInitService(enabled=True).check_and_trigger(
        "s", ec, _GhFile(""), "o", "r", dispatch_fn
    )
    assert calls == []
    assert ec.wiki_initialized is True


@pytest.mark.asyncio
async def test_check_and_trigger_skips_when_in_flight_initialized_or_exhausted() -> None:
    calls = []

    async def dispatch_fn(sym):
        calls.append(sym)

    svc = WikiInitService(enabled=True)
    for ec in (_ec(wiki_in_flight=True), _ec(wiki_initialized=True), _ec(wiki_exhausted=True)):
        await svc.check_and_trigger("s", ec, _GhFile(None), "o", "r", dispatch_fn)
    assert calls == []


@pytest.mark.asyncio
async def test_check_and_trigger_records_failure_on_dispatch_error() -> None:
    ec = _ec()

    async def dispatch_fn(sym):
        raise RuntimeError("boom")

    await WikiInitService(enabled=True).check_and_trigger("s", ec, _GhFile(None), "o", "r", dispatch_fn)
    assert ec.wiki_in_flight is False
    assert ec.wiki_attempts == 1
    assert "boom" in (ec.last_wiki_init_error or "")


# --- handle_init_result (completion) -----------------------------------------

@pytest.mark.asyncio
async def test_handle_init_result_success_merges_and_marks() -> None:
    ec = _ec(wiki_in_flight=True)
    gh = _Github(
        merge={"mergeable_raw": "MERGEABLE", "merge_state_status": "CLEAN"},
        reviews=[{"author_login": _BOT, "state": "APPROVED"}],
        squash={"merged": True},
    )
    await WikiInitService(enabled=True).handle_init_result(
        "s", ec, gh, "PR1", [_BOT], _Collector(), job_succeeded=True
    )
    assert ec.wiki_initialized is True


@pytest.mark.asyncio
async def test_handle_init_result_job_failed_notifies_on_exhaustion() -> None:
    ec = _ec(wiki_in_flight=True)
    notify = _Collector()
    await WikiInitService(enabled=True, max_attempts=1).handle_init_result(
        "s", ec, _Github({}, [], {}), "", [_BOT], notify, job_succeeded=False, error="boom"
    )
    assert ec.wiki_exhausted is True
    assert len(notify.events) == 1


@pytest.mark.asyncio
async def test_handle_init_result_automerge_blocked_notifies_before_exhaustion() -> None:
    ec = _ec(wiki_in_flight=True)
    notify = _Collector()
    gh = _Github(
        merge={"mergeable_raw": "MERGEABLE", "merge_state_status": "CLEAN"},
        reviews=[{"author_login": _BOT, "state": "APPROVED"}],
        squash={"merged": False},  # branch protection blocks the merge
    )
    await WikiInitService(enabled=True, max_attempts=5).handle_init_result(
        "s", ec, gh, "PR1", [_BOT], notify, job_succeeded=True
    )
    assert ec.wiki_initialized is False
    assert len(notify.events) == 1  # surfaced hold, even before the budget is spent

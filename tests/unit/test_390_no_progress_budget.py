"""390: a relay that produces nothing must not be free.

Card #160, 2026-09-12: two complete implementer runs, identical verdict, and
coordinare redispatched immediately each time. Roughly 25 minutes of wall clock
and a 620-second baseline suite run per cycle, with no ceiling in sight.

    implementer.red_judged     expected_red=False
    implementer.milestone_failed  'red was not observed'
    implementer.run_reported   status=partial_progress  milestones_completed=0

`bounce_counter` did not engage because it is keyed by head SHA and a
partial_progress run pushes nothing, so every retry looks like the first
attempt at that head.

Three paths in monitor_performer detect "produced nothing" and answer by
dispatching again. None of them counted anything:

    partial_progress -> redispatch          (the one that bit us)
    blocked + no commits -> redispatch
    review stage zero-progress -> redispatch

The existing ceilings do not cover this. The session ceiling measures from the
last thing PRODUCED and these runs produce plenty -- files changed, tool calls,
a completed turn. They are busy, not stalled.
"""
from __future__ import annotations

from coordinare.services.no_progress import (
    MAX_NO_PROGRESS_RELAYS,
    note_no_progress,
    note_progress,
    relays_spent,
    should_block,
)


def _state() -> dict:
    return {}


# --- counting ----------------------------------------------------------------

def test_a_relay_that_produced_nothing_is_counted():
    s = _state()
    note_no_progress(s, "implementing")
    assert relays_spent(s, "implementing") == 1


def test_counting_is_per_stage():
    """A reviewer that checkpointed must not spend the implementer's budget."""
    s = _state()
    note_no_progress(s, "implementing")
    note_no_progress(s, "reviewing")
    note_no_progress(s, "reviewing")
    assert relays_spent(s, "implementing") == 1
    assert relays_spent(s, "reviewing") == 2


def test_real_progress_clears_the_budget():
    """A run that actually commits is making progress and starts fresh; the
    budget is for CONSECUTIVE empty relays, not a lifetime cap."""
    s = _state()
    note_no_progress(s, "implementing")
    note_no_progress(s, "implementing")
    note_progress(s, "implementing")
    assert relays_spent(s, "implementing") == 0
    assert should_block(s, "implementing") is False


def test_progress_on_one_stage_does_not_clear_another():
    s = _state()
    note_no_progress(s, "implementing")
    note_progress(s, "reviewing")
    assert relays_spent(s, "implementing") == 1


# --- the ceiling -------------------------------------------------------------

def test_the_budget_is_bounded():
    assert 1 <= MAX_NO_PROGRESS_RELAYS <= 5, (
        "an unbounded relay budget is the defect this exists to fix, and a very "
        "large one is indistinguishable from having none"
    )


def test_blocking_trips_only_once_the_budget_is_spent():
    s = _state()
    for i in range(MAX_NO_PROGRESS_RELAYS):
        assert should_block(s, "implementing") is False, f"blocked early at {i}"
        note_no_progress(s, "implementing")
    assert should_block(s, "implementing") is True


def test_the_measured_case_terminates():
    """#160 produced the identical empty relay every cycle. However many times
    it repeats, it must stop."""
    s = _state()
    for _ in range(50):
        if should_block(s, "implementing"):
            break
        note_no_progress(s, "implementing")
    else:
        raise AssertionError("50 empty relays and still dispatching")
    assert relays_spent(s, "implementing") <= MAX_NO_PROGRESS_RELAYS


# --- robustness --------------------------------------------------------------

def test_a_corrupt_counter_does_not_crash_the_monitor():
    """This runs on the path that decides whether to dispatch. It reads state
    that has round-tripped through a JSON snapshot and older schema versions."""
    for junk in ({"no_progress_relays": "nonsense"}, {"no_progress_relays": None},
                 {"no_progress_relays": {"implementing": "three"}},
                 {"no_progress_relays": {"implementing": -5}}):
        assert relays_spent(junk, "implementing") >= 0
        assert should_block(junk, "implementing") in (True, False)
        note_no_progress(junk, "implementing")  # must not raise


def test_an_absent_counter_reads_as_zero():
    assert relays_spent({}, "implementing") == 0
    assert should_block({}, "implementing") is False


# --- the three relay paths, at the node -------------------------------------

import pytest  # noqa: E402

from tests.unit.graph.nodes.test_monitor_performer import (  # noqa: E402
    _Performer,
    initial_state,
)


def _relay_state(marker, *, stage="implementing", head_before="abc", head_after="abc",
                 relays=0, extra=None):
    """A performer reporting `marker` with the head unmoved (nothing pushed)."""
    resp = {"status": marker, "head_before": head_before, "head_after": head_after,
            "reason": "red was not observed"}
    if extra:
        resp.update(extra)
    s = initial_state()
    s["performer_services"] = {stage: _Performer(response=resp)}
    s["performer_stage"] = stage
    s["lifecycle_sequence"] = [stage]
    s["current_card"] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    s["agent_dispatch"] = {"session_id": "s1"}
    s["no_progress_relays"] = {stage: relays} if relays else {}
    return s


@pytest.mark.asyncio
async def test_partial_progress_with_no_push_is_bounded():
    """The measured loop: card #160 relayed this twice, identically, and would
    have continued indefinitely."""
    from coordinare.graph.nodes.monitor_performer import monitor_performer

    s = _relay_state("partial_progress", relays=MAX_NO_PROGRESS_RELAYS)
    result = await monitor_performer(s)
    assert result["phase"] == "blocked", "an exhausted relay budget dispatched again"
    q = " ".join(result.get("open_questions", []))
    assert "without committing" in q or "no commit" in q.lower(), q


@pytest.mark.asyncio
async def test_partial_progress_that_pushed_keeps_relaying():
    """The half that must not weaken: a checkpoint that committed work is
    genuine progress and keeps its escape hatch."""
    from coordinare.graph.nodes.monitor_performer import monitor_performer

    s = _relay_state("partial_progress", head_before="abc", head_after="def",
                     relays=MAX_NO_PROGRESS_RELAYS)
    result = await monitor_performer(s)
    assert result["phase"] != "blocked", "a run that pushed work was blocked"
    assert result.get("no_progress_relays", {}).get("implementing", 0) == 0, \
        "a productive run did not clear its budget"


@pytest.mark.asyncio
async def test_each_empty_relay_spends_budget():
    from coordinare.graph.nodes.monitor_performer import monitor_performer

    s = _relay_state("partial_progress", relays=0)
    result = await monitor_performer(s)
    assert result["phase"] != "blocked"
    assert result.get("no_progress_relays", {}).get("implementing") == 1


# --- unknown must never read as progress -------------------------------------

@pytest.mark.parametrize("hb,ha,label", [
    (None, None, "both heads absent"),
    ("abc", None, "head_after absent"),
    (None, "def", "head_before absent"),
    ("", "", "empty strings"),
])
@pytest.mark.asyncio
async def test_unknown_head_information_spends_budget(hb, ha, label):
    """Adversarial review, confirmed critical. head_before and head_after are
    `str | None = None` in the protocol, so a performer may legitimately omit
    them. The first cut read a missing head as "produced work" and CLEARED the
    budget, so any backend that does not report heads looped forever -- the
    exact defect this whole change exists to prevent, reintroduced through a
    fail-open default.

    Not knowing whether work was produced is not evidence that it was.
    """
    from coordinare.graph.nodes.monitor_performer import monitor_performer

    s = _relay_state("partial_progress", head_before=hb, head_after=ha, relays=1)
    result = await monitor_performer(s)
    assert result.get("no_progress_relays", {}).get("implementing") == 2, (
        f"{label}: unknown head information cleared the budget instead of spending it"
    )


@pytest.mark.asyncio
async def test_unknown_heads_eventually_block():
    """The consequence, end to end."""
    from coordinare.graph.nodes.monitor_performer import monitor_performer

    s = _relay_state("partial_progress", head_before=None, head_after=None,
                     relays=MAX_NO_PROGRESS_RELAYS)
    result = await monitor_performer(s)
    assert result["phase"] == "blocked", "a performer reporting no heads relayed forever"


# --- the other two call sites -------------------------------------------------

@pytest.mark.asyncio
async def test_blocked_with_no_commits_is_bounded():
    """Second of the three relay paths. Reviewed and found untested at the node
    level: only partial_progress had an integration test, which is the
    'pin one instance of a rule with three' defect this repo keeps producing."""
    from coordinare.graph.nodes.monitor_performer import monitor_performer

    s = _relay_state("blocked", relays=MAX_NO_PROGRESS_RELAYS)
    result = await monitor_performer(s)
    assert result["phase"] == "blocked"
    q = " ".join(result.get("open_questions", []))
    assert "without pushing" in q or "no commit" in q.lower(), q


@pytest.mark.asyncio
async def test_blocked_with_no_commits_spends_budget_before_blocking():
    from coordinare.graph.nodes.monitor_performer import monitor_performer

    s = _relay_state("blocked", relays=0)
    result = await monitor_performer(s)
    assert result.get("no_progress_relays", {}).get("implementing") == 1


@pytest.mark.parametrize("hb,ha,label", [
    ("", "def", "head_before empty, head_after known"),
    ("abc", "", "head_before known, head_after empty"),
])
@pytest.mark.asyncio
async def test_a_half_known_head_pair_is_not_progress(hb, ha, label):
    """Mutation V2 found the asymmetry. An empty head on either side means the
    before-and-after cannot be compared, and a comparison that cannot be made
    is not evidence that work landed. Both sides must be known AND differ.

    The earlier cases used ("", "") which reads the same with or without the
    guard, so they could not see this.
    """
    from coordinare.graph.nodes.monitor_performer import monitor_performer

    s = _relay_state("partial_progress", head_before=hb, head_after=ha, relays=1)
    result = await monitor_performer(s)
    assert result.get("no_progress_relays", {}).get("implementing") == 2, (
        f"{label}: an incomparable head pair was treated as progress"
    )

"""380 (second half): when the ceiling trips, ask whether the run is converging.

The mechanical floor (#382) decides WHEN to look: it measures from the last
thing the performer produced rather than from dispatch. This decides what to do
when it trips, which the floor alone cannot: a turn that has produced nothing
for a long time might be genuinely stuck, or might be waiting on something slow
and legitimate.

The judgement can only ever GRANT A REPRIEVE. It cannot cause a kill that would
not otherwise have happened, and when the gateway is unreachable the floor
decides exactly as it does today. That direction matters: the gateway being
wedged is itself a cause of stalled performers, so a judge that could kill would
fail hardest at the moment it is least trustworthy.
"""
from __future__ import annotations

import pytest

from coordinare.services.convergence import (
    MAX_REPRIEVES,
    ConvergenceVerdict,
    build_question,
    parse_verdict,
)

# --- the verdict --------------------------------------------------------------

def test_a_converging_verdict_is_read():
    v = parse_verdict({"data": {"converging": True, "reason": "a long test suite is running"}})
    assert v.converging is True
    assert "suite" in v.reason


def test_a_not_converging_verdict_is_read():
    v = parse_verdict({"data": {"converging": False, "reason": "same error four times"}})
    assert v.converging is False


def test_an_unreadable_answer_does_not_grant_a_reprieve():
    """Fail closed. An answer that cannot be read is not a reason to keep
    running a performer that has produced nothing."""
    for bad in ({"data": None}, {}, {"data": {}}, {"data": "yes"}, None, {"data": {"reason": "x"}}):
        assert parse_verdict(bad).converging is False, bad


def test_a_non_boolean_converging_field_is_not_a_yes():
    """A truthy string must not be read as a verdict."""
    for junk in ("true", 1, "yes", []):
        assert parse_verdict({"data": {"converging": junk, "reason": "r"}}).converging is False, junk


# --- the question -------------------------------------------------------------

def test_the_question_carries_the_evidence_not_just_the_clock():
    """The whole point: elapsed time is what the old stopwatch had, and it could
    not tell the measured stuck turn from the measured healthy one."""
    q = build_question(
        stage="implementing", elapsed_s=2700, tool_uses=0, completions=0,
        total_events=164, recent_text=["thinking about the approach"],
    )
    assert "164" in q
    assert "0" in q
    for word in ("command", "produced"):
        assert word in q.lower(), word


def test_the_question_names_no_language_or_tool():
    """#364: the judgement must not carry stack knowledge."""
    q = build_question(stage="implementing", elapsed_s=100, tool_uses=1,
                       completions=0, total_events=5, recent_text=[]).lower()
    for bad in ("python", "ruby", "rspec", "pytest", "rails", "javascript", "npm"):
        assert bad not in q, bad


def test_the_question_is_bounded():
    """It is built from an event stream of unknown size and goes to a gateway."""
    q = build_question(stage="s", elapsed_s=1, tool_uses=0, completions=0,
                       total_events=9, recent_text=["x" * 50_000] * 50)
    assert len(q) < 8000, len(q)


# --- the reprieve budget ------------------------------------------------------

def test_reprieves_are_bounded():
    assert 1 <= MAX_REPRIEVES <= 3, (
        "an unbounded reprieve turns the ceiling back into no ceiling, which is "
        "what this whole issue is about"
    )


def test_verdict_is_frozen():
    from dataclasses import FrozenInstanceError

    v = ConvergenceVerdict(converging=True, reason="r")
    with pytest.raises(FrozenInstanceError):
        v.converging = False


# --- the wiring: it may only ever grant a reprieve ----------------------------

def _stalled_state(reprieves=0, backend=None, events=None):
    from datetime import UTC, datetime, timedelta

    from tests.unit.graph.nodes.test_monitor_performer import _Performer, initial_state

    s = initial_state()
    s["performer_services"] = {"implementing": _Performer(response={"status": "working"})}
    s["performer_stage"] = "implementing"
    s["lifecycle_sequence"] = ["implementing"]
    s["current_card"] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    s["agent_dispatch"] = {"session_id": "s1"}
    s["agent_dispatch_at"] = datetime.now(UTC) - timedelta(seconds=600)
    s["role_timeouts"] = {"implementing": 300}
    s["performer_events"] = events if events is not None else [{"type": "progress", "text": "t"}] * 164
    s["convergence_reprieves"] = reprieves
    if backend is not None:
        s["conducting_backend"] = backend
    return s


class _Backend:
    def __init__(self, answer=None, boom=False):
        self._answer, self._boom, self.calls = answer, boom, 0

    async def prompt(self, text, response_format=None):
        self.calls += 1
        if self._boom:
            raise RuntimeError("gateway unreachable")
        return self._answer

    async def assess(self, card):  # pragma: no cover - protocol completeness
        return {}


@pytest.mark.asyncio
async def test_no_backend_blocks_exactly_as_before():
    """The floor must keep working without the gateway."""
    from coordinare.graph.nodes.monitor_performer import monitor_performer

    result = await monitor_performer(_stalled_state(backend=None))
    assert result["phase"] == "blocked"


@pytest.mark.asyncio
async def test_an_unreachable_gateway_blocks_and_does_not_crash():
    """The gateway being wedged is itself a cause of stalled performers, so
    this is the moment the judge is least trustworthy and most likely absent."""
    from coordinare.graph.nodes.monitor_performer import monitor_performer

    backend = _Backend(boom=True)
    result = await monitor_performer(_stalled_state(backend=backend))
    assert result["phase"] == "blocked"
    assert backend.calls == 1


@pytest.mark.asyncio
async def test_not_converging_blocks_and_says_why():
    from coordinare.graph.nodes.monitor_performer import monitor_performer

    backend = _Backend({"data": {"converging": False, "reason": "no commands in forty minutes"}})
    result = await monitor_performer(_stalled_state(backend=backend))
    assert result["phase"] == "blocked"
    assert "forty minutes" in " ".join(result.get("open_questions", []))


@pytest.mark.asyncio
async def test_converging_grants_one_reprieve_and_keeps_running():
    from coordinare.graph.nodes.monitor_performer import monitor_performer

    backend = _Backend({"data": {"converging": True, "reason": "a slow suite is still running"}})
    result = await monitor_performer(_stalled_state(backend=backend))
    assert result["phase"] != "blocked", result.get("open_questions")
    assert result.get("convergence_reprieves") == 1


@pytest.mark.asyncio
async def test_the_reprieve_budget_is_spent_only_once():
    """Otherwise a model that always says 'converging' removes the ceiling."""
    from coordinare.graph.nodes.monitor_performer import monitor_performer
    from coordinare.services.convergence import MAX_REPRIEVES

    backend = _Backend({"data": {"converging": True, "reason": "still going"}})
    result = await monitor_performer(_stalled_state(reprieves=MAX_REPRIEVES, backend=backend))
    assert result["phase"] == "blocked", "an exhausted budget still granted more time"
    assert backend.calls == 0, "the model was asked after the budget was spent"


@pytest.mark.asyncio
async def test_a_healthy_performer_is_never_judged_at_all():
    """The judgement is only reached when the floor has already tripped, so a
    working performer never costs a model call."""
    from datetime import UTC, datetime, timedelta

    from coordinare.graph.nodes.monitor_performer import monitor_performer

    backend = _Backend({"data": {"converging": False, "reason": "should not be asked"}})
    s = _stalled_state(backend=backend)
    s["last_production_at"] = datetime.now(UTC) - timedelta(seconds=5)

    result = await monitor_performer(s)
    assert result["phase"] != "blocked"
    assert backend.calls == 0, "a working performer was sent to the gateway"


@pytest.mark.asyncio
async def test_a_reprieve_actually_buys_time():
    """Mutation R6: dropping the clock reset passed every test, because none of
    them polled twice. A reprieve that does not move the clock is granted and
    revoked in the same breath -- the next poll trips instantly, the budget is
    already spent, and the run blocks anyway. The performer gets nothing.
    """
    from coordinare.graph.nodes.monitor_performer import monitor_performer

    backend = _Backend({"data": {"converging": True, "reason": "a slow suite is still running"}})
    first = await monitor_performer(_stalled_state(backend=backend))
    assert first["phase"] != "blocked"
    assert first.get("convergence_reprieves") == 1

    # the very next poll, nothing else changed
    second = await monitor_performer(first)
    assert second["phase"] != "blocked", (
        "the reprieve bought no time: the next poll blocked immediately. "
        f"{second.get('open_questions')}"
    )
    assert backend.calls == 1, "the budget was spent, so the model must not be asked again"


# --- per-run state must die with its run -------------------------------------

#: Everything scoped to ONE performer run. A field here that outlives its run
#: corrupts the next one: a stale production clock kills a healthy stage on its
#: first poll, and a spent reprieve budget silently denies every later stage.
#:
#: Enumerated rather than spot-checked because this exact bug has now been
#: introduced twice -- last_production_at in #382, and convergence_reprieves in
#: this change, added directly beside the field whose fix was the lesson.
PER_RUN_STATE = ("agent_dispatch_at", "last_production_at",
                 "last_production_fingerprint", "convergence_reprieves")


def test_advance_stage_clears_every_per_run_field():
    from datetime import UTC, datetime

    from coordinare.graph.nodes.monitor_performer import _advance_stage
    from tests.unit.graph.nodes.test_monitor_performer import initial_state

    s = initial_state()
    s["performer_stage"] = "implementing"
    s["lifecycle_sequence"] = ["implementing", "documenting"]
    s["current_card"] = {"id": "ITEM_1"}
    s["last_production_at"] = datetime.now(UTC)
    s["last_production_fingerprint"] = (9, 2)
    s["convergence_reprieves"] = 1

    updates = _advance_stage(s)

    for key in PER_RUN_STATE:
        assert key in updates, f"{key} survives a stage transition into the next run"
        assert not updates[key], f"{key} was not cleared: {updates[key]!r}"


def test_dispatch_clears_every_per_run_field():
    """The other place a run begins. Checked by source because dispatch does far
    more than reset state and is not cheaply callable here."""
    import inspect

    from coordinare.graph.nodes import dispatch_performer as dp

    tail = inspect.getsource(dp).split('state["performer_events"] = []', 1)[1][:900]
    for key in PER_RUN_STATE:
        if key == "agent_dispatch_at":
            continue  # dispatch SETS this one, it does not clear it
        assert f'state["{key}"]' in tail, (
            f"{key} is not reset where the event stream is reset; a value from the "
            "previous run will govern this one"
        )

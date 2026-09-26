"""430 — the #389 convergence ask folds into the observer; the standalone path retires.

The stalled turn is now a trigger (``stalled_turn``) plus a verdict evaluation
in the observer. There is one judge, not two. The #389 "only ever grant time"
policy is preserved for ``continue``/``correction``/``retune``; a ``kill``
verdict does what #389 deliberately deferred, gated by the #427 kill
guardrails; ``escalate`` and a dead observer leave the floor in charge, exactly
as the pre-observer block path did.

Parity: these tests cover every case the retired standalone path handled, plus
the verdicts it never had. The standalone ask is deleted with its tests — this
file holds the rewritten survivors (reprieve budget, per-run state).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from coordinare.services.observer import (
    MAX_REPRIEVES,
    OBSERVER_TRIGGERS,
    correction_signature,
)

# --- the vocabulary ------------------------------------------------------------

def test_stalled_turn_is_a_trigger():
    """The ask became a trigger: the stalled turn is a wake, not a stopwatch."""
    assert "stalled_turn" in OBSERVER_TRIGGERS


def test_the_standalone_ask_module_is_retired():
    """One judge. The #389 module is deleted, not left dead behind a flag."""
    with pytest.raises(ModuleNotFoundError):
        __import__("coordinare.services.convergence")


# --- the wiring: the observer judges the stalled turn --------------------------

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


def _observer_cfg(**overrides):
    from coordinare.config import ObserverConfig

    base = {"enabled": True, "model_endpoint": "fast-model"}
    base.update(overrides)
    return ObserverConfig(**base)


def _stalled_state(backend, observer_cfg=None, events=None, **extra):
    """A turn whose stall floor has expired: 600s quiet against a 300s role timeout."""
    from coordinare.graph.state import initial_state

    s = initial_state()
    s["performer_services"] = {"implementing": _Performer()}
    s["performer_stage"] = "implementing"
    s["lifecycle_sequence"] = ["implementing"]
    s["current_card"] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    s["agent_dispatch"] = {"session_id": "s1"}
    s["agent_dispatch_at"] = datetime.now(UTC) - timedelta(seconds=600)
    s["role_timeouts"] = {"implementing": 300}
    s["performer_events"] = (
        events if events is not None else [{"type": "progress", "text": "t"}] * 164
    )
    if observer_cfg is not None:
        s["symphony_configs"] = {"sym": SimpleNamespace(observer=observer_cfg)}
        s["current_symphony"] = "sym"
        s["observer_backend"] = backend
    if backend is not None:
        s["conducting_backend"] = backend
    s.update(extra)
    return s


class _Performer:
    """Never polled: an expired stall short-circuits before the status poll."""

    def __init__(self) -> None:
        self.polled = 0

    async def check_status(self, session_id: str, **kwargs: object) -> dict:
        self.polled += 1
        return {"status": "working"}


def _answered(**overrides):
    answer = {"verdict": "continue", "reason": "a slow suite is still running"}
    answer.update(overrides)
    return {"data": answer}


# --- parity: the cases the #389 path handled ------------------------------------

@pytest.mark.asyncio
async def test_continue_grants_one_reprieve_and_keeps_running():
    """The old converging=True case: the observer's continue grants a reprieve."""
    from coordinare.graph.nodes.monitor_performer import monitor_performer

    backend = _Backend(_answered())
    result = await monitor_performer(
        _stalled_state(backend, _observer_cfg()),
    )
    assert result["phase"] != "blocked", result.get("open_questions")
    assert result["convergence_reprieves"] == 1
    assert result["observer_verdict"] == "continue"
    assert backend.calls == 1


@pytest.mark.asyncio
async def test_the_reprieve_budget_is_spent_only_once():
    """Otherwise a model that always says 'continue' removes the ceiling."""
    from coordinare.graph.nodes.monitor_performer import monitor_performer

    backend = _Backend(_answered())
    result = await monitor_performer(
        _stalled_state(backend, _observer_cfg(), convergence_reprieves=MAX_REPRIEVES),
    )
    assert result["phase"] == "blocked", "an exhausted budget still granted more time"
    assert backend.calls == 0, "the model was asked after the budget was spent"


@pytest.mark.asyncio
async def test_a_reprieve_actually_buys_time():
    """A reprieve that does not move the clock is granted and revoked in the
    same breath: the next poll trips instantly and blocks anyway."""
    from coordinare.graph.nodes.monitor_performer import monitor_performer

    backend = _Backend(_answered())
    first = await monitor_performer(_stalled_state(backend, _observer_cfg()))
    assert first["phase"] != "blocked"
    assert first.get("convergence_reprieves") == 1

    second = await monitor_performer(first)
    assert second["phase"] != "blocked", (
        "the reprieve bought no time: the next poll blocked immediately. "
        f"{second.get('open_questions')}"
    )
    assert backend.calls == 1, "the budget was spent, so the model must not be asked again"


@pytest.mark.asyncio
async def test_a_dead_observer_blocks_exactly_as_before():
    """Fail-safe: an unreachable judge leaves the floor in charge."""
    from coordinare.graph.nodes.monitor_performer import monitor_performer

    backend = _Backend(boom=True)
    result = await monitor_performer(_stalled_state(backend, _observer_cfg()))
    assert result["phase"] == "blocked"
    assert backend.calls == 1, "the wake happened"


@pytest.mark.asyncio
async def test_a_malformed_observer_blocks_exactly_as_before():
    from coordinare.graph.nodes.monitor_performer import monitor_performer

    backend = _Backend({"data": {"verdict": "whatever", "reason": "x"}})
    result = await monitor_performer(_stalled_state(backend, _observer_cfg()))
    assert result["phase"] == "blocked"
    assert backend.calls == 1


@pytest.mark.asyncio
async def test_a_healthy_performer_is_never_judged_at_all():
    """The judge is only reached when the floor has already tripped."""
    from coordinare.graph.nodes.monitor_performer import monitor_performer

    backend = _Backend(_answered(verdict="kill"))
    s = _stalled_state(backend, _observer_cfg())
    s["last_production_at"] = datetime.now(UTC) - timedelta(seconds=5)

    result = await monitor_performer(s)
    assert result["phase"] != "blocked"
    assert backend.calls == 0, "a working performer was sent to the gateway"


@pytest.mark.asyncio
async def test_no_double_judging_the_standalone_ask_is_gone():
    """With the observer enabled the stalled turn is evaluated exactly once,
    by the observer. The symphony's own backend (the #389 ask's judge) is
    never consulted."""
    from coordinare.graph.nodes.monitor_performer import monitor_performer

    backend = _Backend(_answered())
    result = await monitor_performer(_stalled_state(backend, _observer_cfg()))
    assert backend.calls == 1, "exactly one judge, exactly one call"
    assert result["phase"] != "blocked"


@pytest.mark.asyncio
async def test_observer_disabled_stall_blocks_without_a_model_call():
    """The ask is retired: with the observer off the floor decides, silently."""
    from coordinare.graph.nodes.monitor_performer import monitor_performer

    backend = _Backend(_answered())
    result = await monitor_performer(_stalled_state(backend, None))
    assert result["phase"] == "blocked"
    assert backend.calls == 0, "a retired judge was still reached"


# --- the new verdicts ------------------------------------------------------------

@pytest.mark.asyncio
async def test_correction_grants_time_and_pends_the_channel():
    """A correction is a grant-time verdict: the turn continues, the directive
    rides its next dispatch."""
    from coordinare.graph.nodes.monitor_performer import monitor_performer

    backend = _Backend(_answered(verdict="correction", reason="you are looping on the same error"))
    result = await monitor_performer(_stalled_state(backend, _observer_cfg()))
    assert result["phase"] != "blocked"
    assert result["convergence_reprieves"] == 1
    pending = result["observer_correction"]
    assert pending["signature"] == correction_signature("you are looping on the same error")
    assert "looping" in pending["body"]


@pytest.mark.asyncio
async def test_retune_grants_time_and_applies_the_request():
    from coordinare.graph.nodes.monitor_performer import monitor_performer

    backend = _Backend(_answered(
        verdict="retune", reason="the budget is the wrong shape",
        retune={"max_tool_calls": 500},
    ))
    cfg = _observer_cfg(retune_bounds={"max_tool_calls": {"floor": 1, "ceiling": 500}})
    result = await monitor_performer(_stalled_state(backend, cfg))
    assert result["phase"] != "blocked"
    assert result["convergence_reprieves"] == 1
    assert result["observer_retunes"]["sym"]["max_tool_calls"] == 500


@pytest.mark.asyncio
async def test_escalate_blocks_with_the_observer_reason():
    """Escalate routes to the existing blocked path: a person must decide."""
    from coordinare.graph.nodes.monitor_performer import monitor_performer

    backend = _Backend(_answered(verdict="escalate", reason="the evidence is contradictory"))
    result = await monitor_performer(_stalled_state(backend, _observer_cfg()))
    assert result["phase"] == "blocked"
    assert "contradictory" in " ".join(result.get("open_questions", []))


@pytest.mark.asyncio
async def test_kill_reprompts_under_the_kill_guardrails(monkeypatch):
    """A kill verdict does what #389 deliberately deferred: stops the judged-dead
    turn through the #427 drain-or-reap path and re-dispatches."""
    from coordinare.graph.nodes.monitor_performer import monitor_performer

    performer = _KillablePerformer()
    backend = _Backend(_answered(verdict="kill", reason="all motion, no artifacts"))
    s = _stalled_state(backend, _observer_cfg())
    s["performer_services"] = {"implementing": performer}
    result = await monitor_performer(s)
    assert result["phase"] == "dispatching", "the judged-dead turn was not reprompted"
    assert result["agent_dispatch"] == {}, "the dead session was not released"
    assert performer.drain_calls == ["s1"], "the stalled session was not stopped"


class _KillablePerformer:
    def __init__(self, status: str = "working") -> None:
        self.drain_calls: list[str] = []
        self.polled = 0
        self._status = status
        self._active_jobs = {"s1": SimpleNamespace(container_id="c1")}

    async def check_status(self, session_id: str, **kwargs: object) -> dict:
        self.polled += 1
        return {"status": self._status}

    async def drain_session(self, session_id: str) -> None:
        self.drain_calls.append(session_id)


async def test_kill_skips_a_turn_that_just_completed():
    """Copilot round 1: the fold runs BEFORE the poll, so it must poll for
    liveness itself — a terminal turn advances through its normal routing,
    it is never drained and re-dispatched."""
    from coordinare.graph.nodes.monitor_performer import monitor_performer

    performer = _KillablePerformer(status="completed")
    backend = _Backend(_answered(verdict="kill", reason="all motion, no artifacts"))
    s = _stalled_state(backend, _observer_cfg())
    s["performer_services"] = {"implementing": performer}
    result = await monitor_performer(s)
    assert performer.drain_calls == [], "a completed turn was drained by a kill"
    assert result.get("agent_dispatch") == {"session_id": "s1"}, (
        "the completed session was released by a kill"
    )
    assert result["phase"] != "blocked"
    assert performer.polled == 1, (
        "Copilot round 2: for an ephemeral service the liveness probe consumed "
        "the terminal job — poll_service must reuse the probed status, not "
        "re-poll a session that no longer exists"
    )


async def test_deferred_kill_keeps_the_turn_alive(monkeypatch):
    """Copilot round 1: a kill the 076 mutex defers keeps monitoring — it must
    not fall through to the timeout block while the session is still live."""
    from coordinare.graph.nodes.monitor_performer import monitor_performer
    from coordinare.services import dispatch_guard

    performer = _KillablePerformer()
    backend = _Backend(_answered(verdict="kill", reason="all motion, no artifacts"))
    s = _stalled_state(backend, _observer_cfg())
    s["performer_services"] = {"implementing": performer}
    monkeypatch.setattr(
        dispatch_guard, "acquire_dispatch_lock",
        lambda card_id, stage: SimpleNamespace(locked=lambda: True, acquire=lambda: None),
    )
    result = await monitor_performer(s)
    assert result["phase"] != "blocked", (
        "a deferred kill blocked a live session: " + str(result.get("open_questions"))
    )
    assert result["phase"] != "dispatching", "a deferred kill re-dispatched anyway"
    assert performer.drain_calls == [], "the deferred kill tore the session down"
    assert result.get("convergence_reprieves", 0) == 0, "a kill verdict spent the reprieve budget"


async def test_a_reprieve_keeps_the_workspace_alive(monkeypatch):
    """Copilot round 2: the fold returns before _phase_in_progress latches the
    workspace as still-active, so the reprieve must latch it itself — a live
    turn must not lose its workspace to the finally-teardown."""
    from coordinare.graph.nodes.monitor import body
    from coordinare.graph.nodes.monitor_performer import monitor_performer

    torn_down: list = []

    async def _record(state):
        torn_down.append(state)

    monkeypatch.setattr(body, "_teardown_workspace", _record)

    backend = _Backend(_answered())
    result = await monitor_performer(_stalled_state(backend, _observer_cfg()))
    assert result["phase"] != "blocked"
    assert result["convergence_reprieves"] == 1
    assert torn_down == [], "a reprieved (still-running) turn had its workspace torn down"

    blocked = await monitor_performer(_stalled_state(_Backend(_answered()), None))
    assert blocked["phase"] == "blocked"
    assert len(torn_down) == 1, "a blocked (dead) run should still tear its workspace down"


async def test_retune_rides_a_continue_verdict():
    """Copilot round 2 (428 parity): a structured retune may ride ANY non-kill
    verdict, so retune-on-continue behaves like the trigger wake's retune."""
    from coordinare.graph.nodes.monitor_performer import monitor_performer

    backend = _Backend(_answered(
        verdict="continue", reason="a slow suite is still running",
        retune={"max_tool_calls": 500},
    ))
    cfg = _observer_cfg(retune_bounds={"max_tool_calls": {"floor": 1, "ceiling": 500}})
    result = await monitor_performer(_stalled_state(backend, cfg))
    assert result["phase"] != "blocked"
    assert result["convergence_reprieves"] == 1
    assert result["observer_retunes"]["sym"]["max_tool_calls"] == 500


# --- per-run state must die with its run (rewritten from #389's suite) ----------

#: Everything scoped to ONE performer run. A field here that outlives its run
#: corrupts the next one: a stale production clock kills a healthy stage on its
#: first poll, and a spent reprieve budget silently denies every later stage.
PER_RUN_STATE = ("agent_dispatch_at", "last_production_at",
                 "last_production_fingerprint", "convergence_reprieves")


def test_advance_stage_clears_every_per_run_field():
    from datetime import datetime

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

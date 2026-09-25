"""428 — bounded observer retune of coordinare orchestration knobs.

A ``retune`` verdict lets the observer adjust coordinare-side orchestration
knobs mid-session: effort, the per-turn token budget (``max_tokens``) and
``max_tool_calls``. The adjustment is bounded by explicit config (floor and
ceiling per knob, defaulting to none — no bounds, no retune), clamped on
out-of-bounds requests, refused for mode switches, logged before/after, and
reversible through an append-only audit event. Retuned values survive the
session and reset with it; a retune never errors the cycle.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from coordinare.services.observer import build_prompt, parse_verdict
from coordinare.services.retune import (
    RETUNE_KNOBS,
    apply_retune,
    retune_prompt_fragment,
)
from tests.unit.test_425_observer_core import _Backend, _observer_cfg, _observer_state

# --- the retunable vocabulary ---------------------------------------------------


def test_the_retunable_vocabulary_is_exactly_the_three_knobs():
    assert frozenset({"effort", "max_tokens", "max_tool_calls"}) == RETUNE_KNOBS


# --- the verdict carries a structured retune request ----------------------------


def test_a_retune_verdict_parses_its_structured_request():
    v = parse_verdict({"data": {"verdict": "retune", "reason": "push effort",
                                "retune": {"effort": "high", "max_tool_calls": 80}}})
    assert v is not None
    assert v.verdict == "retune"
    assert v.retune == {"effort": "high", "max_tool_calls": 80}


def test_a_verdict_without_a_retune_block_parses_with_none():
    v = parse_verdict({"data": {"verdict": "retune", "reason": "vague"}})
    assert v is not None
    assert v.retune is None


def test_non_scalar_retune_values_are_dropped():
    v = parse_verdict({"data": {"verdict": "retune", "reason": "r",
                                "retune": {"max_tool_calls": 80, "mode": {"strategy": "always"}}}})
    assert v is not None
    assert v.retune == {"max_tool_calls": 80}


def test_a_non_retune_verdict_carries_no_request():
    v = parse_verdict({"data": {"verdict": "continue", "reason": "fine",
                                "retune": {"effort": "high"}}})
    assert v is not None
    assert v.verdict == "continue"
    assert v.retune == {"effort": "high"}, "the block parses on any verdict; the phase gates the action"


# --- config: the bounds are explicit and loud -----------------------------------


def _bounds(**knobs):
    from coordinare.config import ObserverConfig

    return ObserverConfig(
        enabled=True, model_endpoint="fast-model", retune_bounds=dict(knobs) if knobs else None,
    )


def test_unknown_retunable_knob_is_a_config_error():
    with pytest.raises(ValueError, match="unknown retunable knobs"):
        _bounds(max_turns={"floor": 1, "ceiling": 2})


def test_a_bound_without_floor_and_ceiling_is_a_config_error():
    with pytest.raises(ValueError, match="floor and ceiling"):
        _bounds(max_tool_calls={"floor": 1})


def test_an_effort_bound_outside_the_vocabulary_is_a_config_error():
    with pytest.raises(ValueError, match="low\\|medium\\|high"):
        _bounds(effort={"floor": "tiny", "ceiling": "high"})


def test_an_effort_floor_above_its_ceiling_is_a_config_error():
    with pytest.raises(ValueError, match="floor must not exceed ceiling"):
        _bounds(effort={"floor": "high", "ceiling": "low"})


def test_a_non_numeric_int_bound_is_a_config_error():
    with pytest.raises(ValueError, match="must be integers"):
        _bounds(max_tool_calls={"floor": "a few", "ceiling": 10})


def test_max_tool_calls_outside_the_performer_domain_is_a_config_error():
    """The performer contract accepts 1..500 (performer/models.py Score).
    A bound outside it turns every clamped retune into a dispatch failure."""
    with pytest.raises(ValueError, match=r"1\.\.500"):
        _bounds(max_tool_calls={"floor": 0, "ceiling": 100})
    with pytest.raises(ValueError, match=r"1\.\.500"):
        _bounds(max_tool_calls={"floor": 10, "ceiling": 501})


def test_max_tokens_must_bound_a_positive_budget():
    with pytest.raises(ValueError, match="positive"):
        _bounds(max_tokens={"floor": 0, "ceiling": 50_000})


def test_valid_bounds_validate():
    cfg = _bounds(
        effort={"floor": "low", "ceiling": "medium"},
        max_tokens={"floor": 1000, "ceiling": 50_000},
        max_tool_calls={"floor": 10, "ceiling": 100},
    )
    assert cfg.retune_bounds is not None


def test_non_finite_floats_are_refused_not_crashed():
    """NaN/Infinity parse from JSON but crash int() — refused_invalid instead."""
    store: dict[str, Any] = {"max_tool_calls": 50}
    new_store, decisions = apply_retune(
        store,
        {"max_tool_calls": float("nan"), "max_tokens": float("inf")},
        {"max_tool_calls": {"floor": 10, "ceiling": 100},
         "max_tokens": {"floor": 1000, "ceiling": 50_000}},
    )
    assert new_store == {"max_tool_calls": 50}, "fail-safe: previous value stays"
    assert [d.outcome for d in decisions] == ["refused_invalid", "refused_invalid"]


# --- clamping: within bounds applies, out of bounds clamps, invalid refuses -----


def _bounds_map():
    return {
        "effort": {"floor": "low", "ceiling": "medium"},
        "max_tokens": {"floor": 1000, "ceiling": 50_000},
        "max_tool_calls": {"floor": 10, "ceiling": 100},
    }


def test_an_in_bounds_request_applies_verbatim():
    overrides, decisions = apply_retune({}, {"max_tool_calls": 80}, _bounds_map())
    assert overrides == {"max_tool_calls": 80}
    assert decisions[0].outcome == "applied"
    assert decisions[0].applied == 80


def test_an_above_ceiling_request_clamps_to_the_ceiling():
    overrides, decisions = apply_retune({}, {"max_tool_calls": 500}, _bounds_map())
    assert overrides == {"max_tool_calls": 100}
    assert decisions[0].outcome == "clamped"
    assert decisions[0].applied == 100


def test_a_below_floor_request_clamps_to_the_floor():
    overrides, decisions = apply_retune({}, {"max_tokens": 10}, _bounds_map())
    assert overrides == {"max_tokens": 1000}
    assert decisions[0].outcome == "clamped"


def test_an_effort_request_clamps_within_the_rank_bounds():
    overrides, decisions = apply_retune({}, {"effort": "high"}, _bounds_map())
    assert overrides == {"effort": "medium"}
    assert decisions[0].outcome == "clamped"


def test_an_in_vocabulary_effort_request_applies():
    overrides, decisions = apply_retune({}, {"effort": "medium"}, _bounds_map())
    assert overrides == {"effort": "medium"}
    assert decisions[0].outcome == "applied"


def test_an_invalid_int_value_refuses_and_keeps_the_previous_value():
    overrides, decisions = apply_retune({"max_tool_calls": 50}, {"max_tool_calls": "lots"}, _bounds_map())
    assert overrides == {"max_tool_calls": 50}, "fail-safe: the previous value stays in place"
    assert decisions[0].outcome == "refused_invalid"


def test_a_bool_request_is_not_an_int_value():
    overrides, _ = apply_retune({}, {"max_tool_calls": True}, _bounds_map())
    assert overrides == {}


# --- refusal: mode switches and unconfigured knobs -------------------------------


def test_a_mode_switch_attempt_is_refused():
    overrides, decisions = apply_retune({}, {"mode": "think_once"}, _bounds_map())
    assert overrides == {}
    assert decisions[0].outcome == "refused_mode_switch"


def test_a_strategy_switch_attempt_is_refused():
    overrides, decisions = apply_retune({}, {"strategy": "always"}, _bounds_map())
    assert overrides == {}
    assert decisions[0].outcome == "refused_mode_switch"


def test_a_knob_without_configured_bounds_is_refused():
    """Retune adjusts only the knobs explicitly configured as retunable."""
    overrides, decisions = apply_retune({}, {"max_tool_calls": 80}, {"effort": {"floor": "low", "ceiling": "high"}})
    assert overrides == {}
    assert decisions[0].outcome == "refused_not_retunable"


def test_apply_retune_never_raises_on_garbage():
    """Fail-safe: any malformed input is a refusal, never an error the cycle."""
    for current, requested, bounds in (
        (None, None, None),
        ({}, {"max_tool_calls": None}, {"max_tool_calls": {"floor": 1, "ceiling": 2}}),
        ({}, "retune everything", _bounds_map()),
        ({}, {"effort": {"nested": "dict"}}, _bounds_map()),
        ({}, {"max_tool_calls": 80}, {"max_tool_calls": {"floor": None, "ceiling": None}}),
    ):
        overrides, decisions = apply_retune(current, requested, bounds)
        assert isinstance(overrides, dict)
        assert isinstance(decisions, list)


# --- the audit event: before, requested, applied --------------------------------


def test_the_audit_event_reconstructs_the_change():
    overrides, decisions = apply_retune({"max_tool_calls": 50}, {"max_tool_calls": 90}, _bounds_map())
    assert overrides == {"max_tool_calls": 90}
    d = decisions[0]
    assert d.before == 50
    assert d.requested == 90
    assert d.applied == 90
    assert d.outcome == "applied"


def test_a_first_retune_audits_no_previous_value():
    _, decisions = apply_retune({}, {"max_tool_calls": 90}, _bounds_map())
    assert decisions[0].before is None


def test_a_clamped_request_audits_what_was_applied_not_what_was_asked():
    _, decisions = apply_retune({"max_tool_calls": 50}, {"max_tool_calls": 500}, _bounds_map())
    assert decisions[0].requested == 500
    assert decisions[0].applied == 100


# --- the prompt evidence: bounds are advertised only when configured --------------


def test_the_default_prompt_carries_no_retune_evidence():
    prompt = build_prompt({"tool_uses": 1}, ["quiet_window"], [])
    assert "Retunable knobs" not in prompt


def test_configured_bounds_are_advertised_in_the_prompt():
    prompt = build_prompt(
        {"tool_uses": 1}, ["quiet_window"], [],
        retune_bounds={"effort": {"floor": "low", "ceiling": "medium"},
                       "max_tool_calls": {"floor": 10, "ceiling": 100}},
    )
    assert "Retunable knobs" in prompt
    assert "effort=[low..medium]" in prompt
    assert "max_tool_calls=[10, 100]" in prompt


def test_the_retune_prompt_stays_bounded():
    prompt = build_prompt(
        {"tool_uses": 1}, ["quiet_window"], [],
        retune_bounds={"effort": {"floor": "low", "ceiling": "medium"},
                       "max_tokens": {"floor": 1, "ceiling": 100_000}},
    )
    assert len(prompt) <= 8000


def test_the_fragment_is_empty_without_bounds():
    assert retune_prompt_fragment(None) == ""
    assert retune_prompt_fragment({}) == ""


# --- the monitor phase: folding a retune verdict into the knobs -------------------

def _retune_state(cfg, backend, **extra):
    s = _observer_state(
        cfg, backend, last_production_at=datetime.now(UTC) - timedelta(seconds=600),
    )
    s.update(extra)
    return s


@pytest.mark.asyncio
async def test_a_retune_verdict_writes_the_override_store_and_the_audit():
    from coordinare.graph.nodes.monitor_performer import monitor_performer

    backend = _Backend({"data": {"verdict": "retune", "reason": "too many tools",
                                "retune": {"max_tool_calls": 500}}})
    cfg = _observer_cfg(retune_bounds={"max_tool_calls": {"floor": 10, "ceiling": 100}})
    s = _retune_state(cfg, backend)

    result = await monitor_performer(s)
    assert result["observer_retunes"]["sym"] == {"max_tool_calls": 100}, "out of bounds clamps"
    audit = result["observer_retune_audit"]
    assert len(audit) == 1
    assert audit[0]["knob"] == "max_tool_calls"
    assert audit[0]["before"] is None
    assert audit[0]["requested"] == 500
    assert audit[0]["applied"] == 100
    assert audit[0]["symphony"] == "sym"
    assert audit[0]["card_id"] == "ITEM_1"


@pytest.mark.asyncio
async def test_a_retune_never_interrupts_the_cycle():
    from coordinare.graph.nodes.monitor_performer import monitor_performer

    backend = _Backend({"data": {"verdict": "retune", "reason": "r", "retune": {"max_tool_calls": 50}}})
    cfg = _observer_cfg(retune_bounds={"max_tool_calls": {"floor": 10, "ceiling": 100}})
    s = _retune_state(cfg, backend)
    dispatch_before = dict(s["agent_dispatch"])

    result = await monitor_performer(s)
    assert result["phase"] == "monitoring_performer"
    assert result["agent_dispatch"] == dispatch_before
    assert not result.get("open_questions")


@pytest.mark.asyncio
async def test_a_retune_without_bounds_is_ignored():
    from coordinare.graph.nodes.monitor_performer import monitor_performer

    backend = _Backend({"data": {"verdict": "retune", "reason": "r", "retune": {"max_tool_calls": 500}}})
    cfg = _observer_cfg()
    s = _retune_state(cfg, backend)

    result = await monitor_performer(s)
    assert "observer_retunes" not in result, "no bounds, no retune — narrow default"
    assert "observer_retune_audit" not in result


@pytest.mark.asyncio
async def test_a_retune_without_a_structured_request_is_ignored():
    from coordinare.graph.nodes.monitor_performer import monitor_performer

    backend = _Backend({"data": {"verdict": "retune", "reason": "just tweak it somehow"}})
    cfg = _observer_cfg(retune_bounds={"max_tool_calls": {"floor": 10, "ceiling": 100}})
    s = _retune_state(cfg, backend)

    result = await monitor_performer(s)
    assert "observer_retunes" not in result
    assert "observer_retune_audit" not in result


@pytest.mark.asyncio
async def test_a_retune_riding_a_continue_verdict_is_applied():
    """Retune-on-continue: a structured retune block applies on ANY verdict."""
    from coordinare.graph.nodes.monitor_performer import monitor_performer

    backend = _Backend({"data": {"verdict": "continue", "reason": "fine, but ease off",
                                 "retune": {"max_tool_calls": 50}}})
    cfg = _observer_cfg(retune_bounds={"max_tool_calls": {"floor": 10, "ceiling": 100}})
    s = _retune_state(cfg, backend)

    result = await monitor_performer(s)
    assert result["observer_retunes"]["sym"] == {"max_tool_calls": 50}


@pytest.mark.asyncio
async def test_kill_keeps_precedence_over_a_retune_block():
    """A kill (live turn) acts on the turn; the piggy-backed retune is skipped."""
    from coordinare.graph.nodes.monitor_performer import monitor_performer

    backend = _Backend({"data": {"verdict": "kill", "reason": "off the rails",
                                 "retune": {"max_tool_calls": 50}}})
    cfg = _observer_cfg(retune_bounds={"max_tool_calls": {"floor": 10, "ceiling": 100}})
    s = _retune_state(cfg, backend, active_card_id="ITEM_1")

    result = await monitor_performer(s)
    assert result["phase"] == "monitoring_performer"
    assert "observer_retunes" not in result


@pytest.mark.asyncio
async def test_the_production_prompt_carries_the_configured_bounds():
    """The observe() call must advertise the retunable knobs — otherwise the
    observer cannot make a bounded request at all."""
    from coordinare.graph.nodes.monitor_performer import monitor_performer

    backend = _Backend({"data": {"verdict": "retune", "reason": "r",
                                 "retune": {"max_tool_calls": 50}}})
    cfg = _observer_cfg(retune_bounds={"max_tool_calls": {"floor": 10, "ceiling": 100}})
    s = _retune_state(cfg, backend)

    await monitor_performer(s)
    assert backend.last_prompt is not None
    assert "Retunable knobs" in backend.last_prompt
    assert "max_tool_calls=[10, 100]" in backend.last_prompt


@pytest.mark.asyncio
async def test_the_production_prompt_stays_bounded_without_config():
    """Default-off: the prompt is byte-identical, no retune evidence."""
    from coordinare.graph.nodes.monitor_performer import monitor_performer

    backend = _Backend({"data": {"verdict": "continue", "reason": "r"}})
    cfg = _observer_cfg()
    s = _retune_state(cfg, backend)

    await monitor_performer(s)
    assert backend.last_prompt is not None
    assert "Retunable knobs" not in backend.last_prompt


@pytest.mark.asyncio
async def test_a_refused_retune_leaves_the_previous_value_in_place():
    from coordinare.graph.nodes.monitor_performer import monitor_performer

    backend = _Backend({"data": {"verdict": "retune", "reason": "switch it up",
                                "retune": {"mode": "think_once", "max_tool_calls": "many"}}})
    cfg = _observer_cfg(retune_bounds={"max_tool_calls": {"floor": 10, "ceiling": 100}})
    s = _retune_state(cfg, backend, observer_retunes={"sym": {"max_tool_calls": 50}})

    result = await monitor_performer(s)
    assert result["observer_retunes"] == {"sym": {"max_tool_calls": 50}}, "fail-safe: unchanged"
    assert len(result["observer_retune_audit"]) == 2, "both refusals are audited"


@pytest.mark.asyncio
async def test_a_second_retune_audits_the_last_applied_value():
    from coordinare.graph.nodes.monitor_performer import monitor_performer

    backend = _Backend({"data": {"verdict": "retune", "reason": "r", "retune": {"max_tool_calls": 50}}})
    cfg = _observer_cfg(retune_bounds={"max_tool_calls": {"floor": 10, "ceiling": 100}})
    s = _retune_state(cfg, backend, observer_retunes={"sym": {"max_tool_calls": 90}})

    result = await monitor_performer(s)
    audit = result["observer_retune_audit"]
    assert audit[0]["before"] == 90
    assert audit[0]["applied"] == 50
    assert result["observer_retunes"] == {"sym": {"max_tool_calls": 50}}


# --- the dispatch: retuned values override the role tuning ------------------------

# _apply_observer_retune_overrides runs LAST in the dispatch tuning sequence
# (after the role tuning and the persona scope tier), so a retuned knob is the
# final word on its payload key.


def test_retuned_knobs_override_the_dispatch_context():
    from coordinare.graph.nodes.dispatch_performer import _apply_observer_retune_overrides

    state: dict[str, Any] = {
        "config": None,
        "current_symphony": "sym",
        "observer_retunes": {"sym": {"effort": "high", "max_tool_calls": 60, "max_tokens": 20_000}},
    }
    card_context: dict[str, Any] = {"effort": "low", "max_tool_calls": 30}
    _apply_observer_retune_overrides(state, card_context)
    assert card_context["effort"] == "high"
    assert card_context["max_tool_calls"] == 60
    assert card_context["max_tokens"] == 20_000


def test_a_symphony_without_retunes_dispatches_untouched():
    from coordinare.graph.nodes.dispatch_performer import _apply_observer_retune_overrides

    state: dict[str, Any] = {"config": None, "current_symphony": "sym"}
    card_context: dict[str, Any] = {"effort": "low"}
    _apply_observer_retune_overrides(state, card_context)
    assert card_context == {"effort": "low"}, "default-off: no retune writes, byte-identical dispatch"


def test_another_symphonys_retunes_do_not_leak():
    from coordinare.graph.nodes.dispatch_performer import _apply_observer_retune_overrides

    state: dict[str, Any] = {
        "config": None,
        "current_symphony": "other",
        "observer_retunes": {"sym": {"effort": "high"}},
    }
    card_context: dict[str, Any] = {"effort": "low"}
    _apply_observer_retune_overrides(state, card_context)
    assert card_context == {"effort": "low"}


# --- the session lifecycle: survive the session, reset with it --------------------


def test_retuned_values_survive_between_monitor_cycles():
    """The store is flat-state: written by one cycle, read by the next dispatch."""
    from coordinare.graph.nodes.dispatch_performer import _apply_observer_retune_overrides

    state: dict[str, Any] = {
        "config": None,
        "current_symphony": "sym",
        "observer_retunes": {"sym": {"max_tool_calls": 100}},
    }
    first: dict[str, Any] = {}
    _apply_observer_retune_overrides(state, first)
    second: dict[str, Any] = {}
    _apply_observer_retune_overrides(state, second)
    assert first["max_tool_calls"] == second["max_tool_calls"] == 100


def test_retiring_the_session_resets_the_retuned_values():
    from coordinare.graph.state import _retire_active_session

    state: dict[str, Any] = {
        "active_sessions": {},
        "active_card_id": "ITEM_1",
        "current_card": {"id": "ITEM_1"},
        "current_symphony": "sym",
        "observer_retunes": {"sym": {"max_tool_calls": 100, "effort": "high"}},
        "observer_retune_audit": [{
            "knob": "max_tool_calls", "symphony": "sym",
            "before": None, "applied": 100,
        }],
    }
    _retire_active_session(state, trigger="card_done")
    assert state["observer_retunes"] == {}
    assert state["observer_retune_audit"] == []


def test_retiring_one_symphony_keeps_the_others_retunes():
    """State is shared across symphonies: retiring B must not erase A's retunes."""
    from coordinare.graph.state import _retire_active_session

    state: dict[str, Any] = {
        "active_sessions": {},
        "active_card_id": "ITEM_1",
        "current_card": {"id": "ITEM_1"},
        "current_symphony": "bee",
        "observer_retunes": {
            "ay": {"effort": "high"},
            "bee": {"max_tool_calls": 100},
        },
        "observer_retune_audit": [
            {"knob": "effort", "symphony": "ay", "before": "low", "applied": "high"},
            {"knob": "max_tool_calls", "symphony": "bee", "before": None, "applied": 100},
        ],
    }
    _retire_active_session(state, trigger="card_done")
    assert state["observer_retunes"] == {"ay": {"effort": "high"}}
    assert state["observer_retune_audit"] == [{
        "knob": "effort", "symphony": "ay", "before": "low", "applied": "high",
    }]


def test_the_retune_state_survives_the_fanout_merge():
    """The daemon's global merge is the only path flat-state writes take into
    the next cycle — an allowlist miss here silently discards every retune."""
    from coordinare.daemon import _GLOBAL_STATE_KEYS

    assert "observer_retunes" in _GLOBAL_STATE_KEYS
    assert "observer_retune_audit" in _GLOBAL_STATE_KEYS


def test_retirement_without_retunes_writes_nothing():
    """Default-off stays byte-identical: no retune keys materialise."""
    from coordinare.graph.state import _retire_active_session

    state: dict[str, Any] = {
        "active_sessions": {"ITEM_1": {"current_card": {"id": "ITEM_1"}}},
        "active_card_id": "ITEM_1",
        "current_card": {"id": "ITEM_1"},
    }
    _retire_active_session(state, trigger="card_done")
    assert "observer_retunes" not in state
    assert "observer_retune_audit" not in state

"""426 — observer correction channel.

The observer's ``correction`` verdict must reach the performer's next turn
without becoming a bounce: the correction text rides the dispatch payload's
``relay_feedback`` (rendered under "## Human Feedback"), repeated identical
corrections collapse, a changed correction replaces them, and correction
turns consume the shared feedback-cycle budget exactly like bounce feedback.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from coordinare.graph.nodes.dispatch_performer import inject_observer_correction
from coordinare.services.observer import (
    correction_body,
    correction_signature,
    record_correction,
)
from tests.unit.graph.nodes.test_dispatch_performer import _base_state, _Service
from tests.unit.test_425_observer_core import _Backend, _observer_cfg, _observer_state

# --- pure helpers -----------------------------------------------------------------


def test_the_signature_is_stable_across_whitespace_and_case() -> None:
    first = correction_signature("Stop renaming the  module\n\nUse camelCase instead.")
    again = correction_signature("Stop renaming THE module use camelCase instead.")
    assert first == again
    assert len(first) == 12


def test_different_text_yields_a_different_signature() -> None:
    assert correction_signature("stop the refactor") != correction_signature("stop the refactors")


def test_the_correction_body_carries_text_and_evidence() -> None:
    body = correction_body("stop the refactor", {"tool_uses": 2, "quiet_age_s": 310})
    assert body.startswith("Observer correction: stop the refactor")
    assert "Evidence summary: quiet_age_s=310, tool_uses=2" in body


def test_the_correction_body_is_bounded() -> None:
    body = correction_body("x" * 2000, None)
    assert len(body) <= 800
    assert body.startswith("Observer correction: ")


def test_the_correction_body_skips_unrenderable_evidence() -> None:
    body = correction_body("x", {"obj": object(), "nothing": None})
    assert "Evidence summary" not in body


def test_the_same_signature_collapses() -> None:
    pending = {"signature": correction_signature("same thing"), "body": "Observer correction: same thing"}
    assert record_correction(pending, "same thing", None) is None


def test_a_new_signature_replaces() -> None:
    pending = {"signature": correction_signature("old"), "body": "Observer correction: old"}
    new = record_correction(pending, "new directive", {"tool_uses": 1})
    assert new is not None
    assert new["signature"] == correction_signature("new directive")
    assert "new directive" in new["body"]


# --- the monitor phase: pend, collapse, replace, budget ---------------------------


def _awake_state(reason: str, **extra: Any) -> tuple[Any, dict[str, Any]]:
    backend = _Backend({"data": {"verdict": "correction", "reason": reason}})
    cfg = _observer_cfg(quiet_window_seconds=100.0)
    s = _observer_state(cfg, backend, last_production_at=datetime.now(UTC) - timedelta(seconds=600))
    s.update(extra)
    return backend, s


@pytest.mark.asyncio
async def test_a_correction_verdict_pends_the_correction() -> None:
    from coordinare.graph.nodes.monitor_performer import monitor_performer

    _, s = _awake_state("stop renaming the module")
    result = await monitor_performer(s)
    pending = result["observer_correction"]
    assert pending["signature"] == correction_signature("stop renaming the module")
    assert pending["body"].startswith("Observer correction: ")
    assert result["phase"] == "monitoring_performer"
    assert result["agent_dispatch"] == {"session_id": "s1"}
    assert not result.get("relay_feedback")


@pytest.mark.asyncio
async def test_a_repeated_correction_collapses() -> None:
    from coordinare.graph.nodes.monitor_performer import monitor_performer

    _, s = _awake_state("same thing")
    first = await monitor_performer(s)
    s["last_production_at"] = datetime.now(UTC) - timedelta(seconds=600)
    second = await monitor_performer(s)
    assert first["observer_correction"]["signature"] == second["observer_correction"]["signature"]
    assert first["observer_correction"]["body"] == second["observer_correction"]["body"]


@pytest.mark.asyncio
async def test_a_changed_correction_replaces() -> None:
    from coordinare.graph.nodes.monitor_performer import monitor_performer

    _, s = _awake_state("first directive")
    await monitor_performer(s)
    s["observer_backend"] = _Backend({"data": {"verdict": "correction", "reason": "second directive"}})
    s["last_production_at"] = datetime.now(UTC) - timedelta(seconds=600)
    result = await monitor_performer(s)
    assert result["observer_correction"]["signature"] == correction_signature("second directive")


@pytest.mark.asyncio
async def test_a_correction_never_bounces_or_interrupts() -> None:
    from coordinare.graph.nodes.monitor_performer import monitor_performer

    _, s = _awake_state("a correction, not a bounce")
    before_dispatch = dict(s["agent_dispatch"])
    result = await monitor_performer(s)
    assert result["phase"] == "monitoring_performer"
    assert result["agent_dispatch"] == before_dispatch
    assert result.get("open_questions") == []


@pytest.mark.asyncio
async def test_a_non_correction_verdict_writes_no_correction() -> None:
    from coordinare.graph.nodes.monitor_performer import monitor_performer

    backend = _Backend({"data": {"verdict": "continue", "reason": "fine"}})
    cfg = _observer_cfg(quiet_window_seconds=100.0)
    s = _observer_state(cfg, backend, last_production_at=datetime.now(UTC) - timedelta(seconds=600))
    result = await monitor_performer(s)
    assert "observer_correction" not in result


@pytest.mark.asyncio
async def test_corrections_consume_the_feedback_budget() -> None:
    from coordinare.graph.nodes.monitor_performer import monitor_performer

    _, s = _awake_state("the same thing", config=SimpleNamespace(max_feedback_cycles=2))
    for _ in range(3):
        s["last_production_at"] = datetime.now(UTC) - timedelta(seconds=600)
        s = await monitor_performer(s)
    assert s["phase"] == "blocked"
    assert s["open_questions"]
    assert s["agent_dispatch"] == {}
    assert s["content_feedback_cycles"] == 3


# --- cross-cycle survival: the fanout session merge --------------------------------


def test_the_correction_survives_the_session_fanout_merge() -> None:
    """Multi-card mode: the daemon hydrates each card's flat state from its
    CardSession and merges back only _SESSION_FIELDS. A correction pended
    during a monitor cycle must survive that merge to reach the next dispatch."""
    from coordinare.session import session_to_state, state_to_session

    state: dict[str, Any] = {
        "observer_correction": {"signature": "abc", "body": "Observer correction: steer"},
    }
    session = state_to_session(state)
    hydrated: dict[str, Any] = {}
    session_to_state(session, hydrated)
    assert hydrated["observer_correction"] == {"signature": "abc", "body": "Observer correction: steer"}


def test_a_stale_correction_is_cleared_on_card_hydration() -> None:
    """_SESSION_OPTIONAL_FIELDS semantics: a session without a pending
    correction must not inherit one left in the flat state by a previous
    card's step."""
    from coordinare.session import session_to_state, state_to_session

    flat: dict[str, Any] = {"observer_correction": {"signature": "stale", "body": "stale"}}
    session = state_to_session({})
    session_to_state(session, flat)
    assert "observer_correction" not in flat


@pytest.mark.asyncio
async def test_cancelling_the_card_drops_the_pending_correction() -> None:
    from coordinare.cancel import cancel_active_card
    from coordinare.graph.state import initial_state

    s = initial_state()
    s["phase"] = "monitoring_performer"
    s["active_card_id"] = "ITEM_1"
    s["current_card"] = {"id": "ITEM_1", "title": "t", "status": "IN_PROGRESS"}
    s["agent_dispatch"] = {}
    s["observer_correction"] = {"signature": "abc", "body": "Observer correction: x"}
    result = await cancel_active_card(s)
    assert result["status"] == "cancelled"
    assert s["observer_correction"] is None


# --- delivery: the correction rides the dispatch payload --------------------------


def test_the_injection_appends_the_correction_to_the_payload() -> None:
    card_context: dict[str, Any] = {}
    state: dict[str, Any] = {"observer_correction": {"signature": "abc", "body": "Observer correction: steer"}}
    inject_observer_correction(card_context, state)
    assert card_context["relay_feedback"] == [{"body": "Observer correction: steer", "author_login": "observer"}]


def test_the_injection_is_a_noop_without_a_pending_correction() -> None:
    card_context: dict[str, Any] = {}
    inject_observer_correction(card_context, {})
    assert "relay_feedback" not in card_context


def test_the_state_feedback_list_is_never_mutated() -> None:
    """_base_card_context aliases the flat state's list into the payload; the
    injection must build a fresh list, or a failed dispatch would leave the
    correction inside state["relay_feedback"] — dispatch-causing feedback —
    and re-append a duplicate on the retry."""
    from coordinare.graph.nodes.dispatch_performer import _base_card_context

    state: dict[str, Any] = {
        "relay_feedback": [{"body": "bounce directive", "author_login": "coordinare"}],
        "observer_correction": {"signature": "abc", "body": "Observer correction: steer"},
    }
    card_context, _role = _base_card_context(state, None, "ITEM_1", "implementing")
    assert state["relay_feedback"] == [{"body": "bounce directive", "author_login": "coordinare"}]
    assert card_context["relay_feedback"] == [
        {"body": "bounce directive", "author_login": "coordinare"},
        {"body": "Observer correction: steer", "author_login": "observer"},
    ]


@pytest.mark.asyncio
async def test_the_next_dispatch_carries_then_consumes_the_correction() -> None:
    from coordinare.graph.nodes.dispatch_performer import dispatch_performer

    service = _Service()
    state = _base_state(
        performer_services={"implementing": service},
        performer_stage="implementing",
        lifecycle_sequence=["assessing", "implementing", "reviewing", "qa"],
    )
    state["observer_correction"] = {
        "signature": correction_signature("stop renaming"),
        "body": "Observer correction: stop renaming",
    }
    result = await dispatch_performer(state)
    assert result["phase"] == "monitoring_performer"
    payload_relay = service.dispatched[0]["relay_feedback"]
    assert payload_relay == [{"body": "Observer correction: stop renaming", "author_login": "observer"}]
    assert result["observer_correction"] is None
    assert result["relay_feedback"] == []


@pytest.mark.asyncio
async def test_exhausting_the_budget_releases_the_slot_and_the_run() -> None:
    from unittest.mock import patch

    from coordinare.graph.nodes.monitor_performer import monitor_performer

    released: list[tuple[str, str]] = []

    class _SlotMgr:
        def release(self, stage: str, card_id: str) -> None:
            released.append((stage, card_id))

    drains: list[dict[str, Any]] = []

    async def _fake_drain(session_id: str, **_: Any) -> tuple[str, float]:
        drains.append({"session_id": session_id})
        return "reaped", 0.0

    _, s = _awake_state("the same thing", config=SimpleNamespace(max_feedback_cycles=2))
    s["slot_manager"] = _SlotMgr()
    with patch("coordinare.services.dispatch_guard.drain_or_reap", _fake_drain):
        for _ in range(3):
            s["last_production_at"] = datetime.now(UTC) - timedelta(seconds=600)
            s = await monitor_performer(s)
    assert s["phase"] == "blocked"
    assert drains == [{"session_id": "s1"}]
    assert released == [("implementing", "ITEM_1")]


def test_a_stage_advance_drops_the_pending_correction() -> None:
    from coordinare.graph.nodes.monitor.verdict import _advance_stage

    state: dict[str, Any] = {
        "performer_stage": "implementing",
        "lifecycle_sequence": ["implementing", "reviewing"],
        "current_card": {"id": "ITEM_1", "status": "IN_PROGRESS"},
        # Not gated on the CURRENT observer config: a config reload can
        # disable the observer after a correction was pended, and the stale
        # directive must still not reach the next role.
        "symphony_configs": {"sym": SimpleNamespace(observer=_observer_cfg(enabled=False))},
        "current_symphony": "sym",
        "observer_correction": {"signature": "abc", "body": "Observer correction: x"},
    }
    updates = _advance_stage(state, None)
    assert updates["performer_stage"] == "reviewing"
    assert "observer_correction" in updates
    assert updates["observer_correction"] is None


def test_a_stage_advance_without_a_pending_correction_stays_absent() -> None:
    from coordinare.graph.nodes.monitor.verdict import _advance_stage

    state: dict[str, Any] = {
        "performer_stage": "implementing",
        "lifecycle_sequence": ["implementing", "reviewing"],
        "current_card": {"id": "ITEM_1", "status": "IN_PROGRESS"},
        "symphony_configs": {"sym": SimpleNamespace(observer=_observer_cfg())},
        "current_symphony": "sym",
    }
    updates = _advance_stage(state, None)
    assert "observer_correction" not in updates


@pytest.mark.asyncio
async def test_the_correction_rides_existing_feedback() -> None:
    from coordinare.graph.nodes.dispatch_performer import dispatch_performer

    service = _Service()
    state = _base_state(
        performer_services={"implementing": _Service(), "reviewing": service},
        performer_stage="reviewing",
        lifecycle_sequence=["assessing", "implementing", "reviewing", "qa"],
    )
    state["relay_feedback"] = [{"body": "bounce directive", "author_login": "coordinare"}]
    state["observer_correction"] = {
        "signature": correction_signature("observer note"),
        "body": "Observer correction: observer note",
    }
    await dispatch_performer(state)
    assert service.dispatched[0]["relay_feedback"] == [
        {"body": "bounce directive", "author_login": "coordinare"},
        {"body": "Observer correction: observer note", "author_login": "observer"},
    ]

"""Spec 076 T171 — monitor_performer empty-output retry cap.

Regression for card #149 (2026-05-29): qwen ground for ~17 min on a
71-file card and returned "Backend produced an empty architecture
plan".  Pre-T171 this looped forever (block → requeue → re-dispatch →
empty → …), pinging Slack every cycle.  The cap routes it through a
low-budget retry counter that fails fast to BLOCKED-for-human.
"""
from __future__ import annotations

import pytest

from coordinare.graph.nodes.monitor_performer import monitor_performer
from coordinare.graph.state import initial_state


class _EmptyPlanPerformer:
    """Emits the EXACT error string the architect backend produces on an
    empty plan (agent/performer/src/performer/main.py:1421)."""

    async def check_status(self, session_id, **kwargs):
        return {"status": "error", "reason": "Backend produced an empty architecture plan"}


def _state(card_id: str = "PVTI_149", stage: str = "architecting") -> dict:
    state = initial_state()
    state["performer_services"] = {stage: _EmptyPlanPerformer()}
    state["performer_stage"] = stage
    state["lifecycle_sequence"] = [stage, "implementing"]
    state["current_card"] = {"id": card_id, "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "uuid-x"}
    state["active_sessions"] = {
        card_id: {
            "phase": "monitoring_performer",
            "performer_stage": stage,
            "idle_timeout_retries": {},
            "agent_dispatch": {"session_id": "uuid-x"},
        }
    }
    return state


@pytest.mark.asyncio
async def test_empty_plan_first_occurrence_retries() -> None:
    """Default budget=1: the first empty plan re-dispatches the same
    stage (absorbs the rare transient)."""
    state = _state()
    result = await monitor_performer(state)
    assert result.get("phase") == "dispatching"
    assert result.get("agent_dispatch") == {}
    retries = state["active_sessions"]["PVTI_149"]["idle_timeout_retries"]
    assert "PVTI_149:architecting:empty_output" in retries
    assert retries["PVTI_149:architecting:empty_output"]["attempt_count"] == 1


@pytest.mark.asyncio
async def test_empty_plan_second_occurrence_blocks_with_operator_question() -> None:
    """After the budget is spent, the empty plan BLOCKS with an
    operator-facing open_question (so handle_blocked does NOT requeue —
    the infinite churn loop is broken)."""
    state = _state()
    # First occurrence consumes the single retry
    await monitor_performer(state)
    # Re-establish the in-flight session for the second occurrence
    state["agent_dispatch"] = {"session_id": "uuid-x"}
    state["active_sessions"]["PVTI_149"]["phase"] = "monitoring_performer"
    state["active_sessions"]["PVTI_149"]["agent_dispatch"] = {"session_id": "uuid-x"}
    state["phase"] = "monitoring_performer"

    result = await monitor_performer(state)
    assert result.get("phase") == "blocked"
    qs = result.get("open_questions") or []
    assert qs, "BLOCK must populate open_questions so handle_blocked won't requeue"
    assert "empty/unusable output" in qs[0]
    assert "model-capability" in qs[0]


@pytest.mark.asyncio
async def test_empty_plan_does_not_touch_idle_timeout_counter() -> None:
    """The empty-output counter is namespaced — it must not consume the
    idle-timeout budget for the same (card, stage)."""
    state = _state()
    await monitor_performer(state)
    retries = state["active_sessions"]["PVTI_149"]["idle_timeout_retries"]
    # Only the namespaced empty-output key exists; no bare idle key
    assert "PVTI_149:architecting:empty_output" in retries
    assert "PVTI_149:architecting" not in retries

"""072 FR-072-12: Integration regression for the implementer no-commit "blocked" pattern.

Reproduces the card #70 scenario observed in production: an implementer that
emits ``status=blocked`` while head_before == head_after must be routed back
to ``phase=dispatching`` with a directive relay_feedback rather than honoring
the blocked verdict.

This guards FR-070-7 (single-signal implementer guardrail) against silent
regression as FR-072 adds the parallel multi-signal review-stage guardrail.
"""

from __future__ import annotations

import pytest

from coordinare.graph.nodes.monitor_performer import monitor_performer
from coordinare.graph.state import initial_state


class _BlockedNoCommitPerformer:
    async def check_status(self, session_id: str, **_: object) -> dict:
        _ = session_id
        return {
            "status": "blocked",
            "questions": ["Should I keep going?"],
            "head_before": "abc123",
            "head_after": "abc123",
        }


def _state_for_implementer() -> dict:
    state = initial_state()
    state["performer_services"] = {"implementing": _BlockedNoCommitPerformer()}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing", "reviewing"]
    state["current_card"] = {"id": "CARD_70", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s-card-70"}
    return state


@pytest.mark.asyncio
async def test_implementer_blocked_zero_commits_is_redispatched() -> None:
    result = await monitor_performer(_state_for_implementer())

    assert result["phase"] == "dispatching"
    assert result["performer_stage"] == "implementing"
    assert result["agent_dispatch"] == {}
    relay = result["relay_feedback"]
    assert len(relay) == 1
    assert "without pushing any new commits" in relay[0]["body"]
    # FR-072-8: head audit trail captured on the terminal response.
    assert result["head_at_dispatch"] == "abc123"
    assert result["head_at_last_turn"] == "abc123"


@pytest.mark.asyncio
async def test_implementer_blocked_with_commits_stays_blocked() -> None:
    """Sanity check the inverse: real commits → honor blocked verdict."""

    class _Performer:
        async def check_status(self, session_id: str, **_: object) -> dict:
            _ = session_id
            return {
                "status": "blocked",
                "questions": ["What credential should I use?"],
                "head_before": "abc123",
                "head_after": "def456",
            }

    state = _state_for_implementer()
    state["performer_services"] = {"implementing": _Performer()}

    result = await monitor_performer(state)

    assert result["phase"] == "blocked"
    assert result["open_questions"] == ["What credential should I use?"]
    assert result["head_at_last_turn"] == "def456"

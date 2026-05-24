"""072 FR-072-13: Integration regression for role-aware checkpoint protocol.

Validates that the sentinel set ``{implementing, reviewing, security, qa,
documenting}`` honors ``partial_progress`` and that the per-role zero-progress
guardrail re-dispatches review-style stages that emit ``blocked`` with no
observable progress (no head delta + no new bot PR comments).
"""

from __future__ import annotations

import pytest

from coordinare.graph.nodes.monitor_performer import monitor_performer
from coordinare.graph.state import initial_state


class _Performer:
    def __init__(self, response: dict) -> None:
        self._response = response

    async def check_status(self, session_id: str, **_: object) -> dict:
        _ = session_id
        return self._response


def _state_for(stage: str, response: dict) -> dict:
    state = initial_state()
    state["performer_services"] = {stage: _Performer(response)}
    state["performer_stage"] = stage
    state["lifecycle_sequence"] = [
        "implementing", "reviewing", "security", "qa", "documenting"
    ]
    state["current_card"] = {"id": "CARD_X", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "sess-1"}
    return state


@pytest.mark.parametrize(
    "stage",
    ["implementing", "reviewing", "security", "qa", "documenting"],
)
@pytest.mark.asyncio
async def test_partial_progress_preserves_stage(stage: str) -> None:
    """FR-072-2: partial_progress from any sentinel role keeps performer_stage."""
    state = _state_for(stage, {
        "status": "partial_progress",
        "next_focus": f"Continue {stage} work",
        "head_before": "h1",
        "head_after": "h2",
    })

    result = await monitor_performer(state)

    assert result["phase"] == "dispatching"
    assert result["performer_stage"] == stage
    assert result["agent_dispatch"] == {}
    relay = result["relay_feedback"]
    assert any(f"Continue {stage} work" in r["body"] for r in relay)


@pytest.mark.parametrize(
    "stage",
    ["reviewing", "security", "qa", "documenting"],
)
@pytest.mark.asyncio
async def test_review_stage_zero_progress_redispatches(stage: str) -> None:
    """FR-072-5: review-style stages with zero head + zero bot comments retry."""
    state = _state_for(stage, {
        "status": "blocked",
        "questions": ["???"],
        "head_before": "same",
        "head_after": "same",
        "bot_pr_comment_delta": 0,
    })

    result = await monitor_performer(state)

    assert result["phase"] == "dispatching"
    assert result["performer_stage"] == stage
    relay = result["relay_feedback"]
    assert len(relay) == 1
    body = relay[0]["body"].lower()
    assert "resume" in body or "checkpoint" in body


@pytest.mark.asyncio
async def test_head_audit_trail_persists_across_turns() -> None:
    """FR-072-8..10: head_at_dispatch is sticky; head_at_last_turn updates."""
    # Turn 1: head_before=h1, head_after=h1 → both fields seeded to h1.
    state = _state_for("implementing", {
        "status": "blocked",
        "questions": ["q?"],
        "head_before": "h1",
        "head_after": "h1",
    })
    result1 = await monitor_performer(state)
    assert result1["head_at_dispatch"] == "h1"
    assert result1["head_at_last_turn"] == "h1"

    # Turn 2: head_before=h2 should NOT overwrite head_at_dispatch (sticky).
    state2 = _state_for("implementing", {
        "status": "blocked",
        "questions": ["q?"],
        "head_before": "h2",
        "head_after": "h3",
    })
    state2["head_at_dispatch"] = result1["head_at_dispatch"]
    state2["head_at_last_turn"] = result1["head_at_last_turn"]

    result2 = await monitor_performer(state2)

    assert result2["head_at_dispatch"] == "h1"  # sticky
    assert result2["head_at_last_turn"] == "h3"  # overwrites


@pytest.mark.parametrize(
    "stage",
    ["reviewing", "security", "qa", "documenting"],
)
@pytest.mark.asyncio
async def test_review_stage_zero_progress_skipped_when_clarifications_grew(
    stage: str,
) -> None:
    """FR-072-5(c): if new clarifications were appended during the turn (e.g.
    by route_issue_comments or check_board), the per-role guardrail must NOT
    trip — clarifications growth counts as progress, so the blocked verdict
    is honored.
    """
    state = _state_for(stage, {
        "status": "blocked",
        "questions": ["???"],
        "head_before": "same",
        "head_after": "same",
        "bot_pr_comment_delta": 0,
    })
    # Snapshot taken at dispatch was 0; now there's 1 clarification.
    state["clarifications_count_at_dispatch"] = 0
    state["card_clarifications"] = [{"question": "q?", "answer": "a"}]

    result = await monitor_performer(state)

    assert result["phase"] == "blocked"
    # Should NOT have been redispatched.
    assert result.get("agent_dispatch") != {}


@pytest.mark.asyncio
async def test_implementer_blocked_no_commits_redispatches_single_signal() -> None:
    """FR-070-7 regression guard: the implementer guardrail trips on the
    single head-delta signal alone — independent of bot_pr_comment_delta or
    clarifications. Don't accidentally widen it to the triple-zero check.
    """
    state = _state_for("implementing", {
        "status": "blocked",
        "questions": ["q?"],
        "head_before": "same",
        "head_after": "same",
        # bot_pr_comment_delta absent and clarifications already present —
        # the implementer guardrail must still fire on head-delta alone.
        "bot_pr_comment_delta": 7,
    })
    state["card_clarifications"] = [{"question": "q?", "answer": "a"}]
    state["clarifications_count_at_dispatch"] = 0

    result = await monitor_performer(state)

    assert result["phase"] == "dispatching"
    assert result["performer_stage"] == "implementing"
    assert result["agent_dispatch"] == {}
    relay = result["relay_feedback"]
    assert any("commit" in r["body"].lower() for r in relay)

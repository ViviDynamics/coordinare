"""072 FR-072-14: Integration regression — reviewer blocked with bot activity.

Ensures a reviewer that DID post PR comments during the turn (bot_pr_comment_delta
> 0) routes to ``phase=blocked`` even when head_before == head_after. The
guardrail must NOT fire for reviewers that surfaced something real; only the
triple-zero state (head delta + bot comments + clarifications) trips it.
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
    state["lifecycle_sequence"] = ["implementing", stage]
    state["current_card"] = {"id": "CARD_REV", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "sess-rev"}
    return state


@pytest.mark.parametrize("stage", ["reviewing", "security", "qa", "documenting"])
@pytest.mark.asyncio
async def test_review_stage_with_bot_comments_stays_blocked(stage: str) -> None:
    state = _state_for(stage, {
        "status": "blocked",
        "questions": ["What is the policy for empty PRs?"],
        "head_before": "same",
        "head_after": "same",
        "bot_pr_comment_delta": 1,
    })

    result = await monitor_performer(state)

    assert result["phase"] == "blocked"
    assert result["open_questions"] == ["What is the policy for empty PRs?"]


@pytest.mark.asyncio
async def test_reviewer_high_bot_activity_stays_blocked() -> None:
    """A reviewer that posted many comments still trips blocked, never the guardrail."""
    state = _state_for("reviewing", {
        "status": "blocked",
        "questions": ["multiple findings — confirm severity?"],
        "head_before": "h1",
        "head_after": "h1",
        "bot_pr_comment_delta": 7,
    })

    result = await monitor_performer(state)

    assert result["phase"] == "blocked"

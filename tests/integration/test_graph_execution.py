from __future__ import annotations

import pytest

from coordinare.graph.builder import CoordinareGraphBuilder
from coordinare.graph.state import initial_state
from tests.utils.fake_notification import FakeNotificationService


class _GitHub:
    async def poll_board(self):
        return {
            "snapshot": {"TODO": ["ITEM_1"], "IN_PROGRESS": [], "IN_REVIEW": [], "BLOCKED": []},
            "titles": {"ITEM_1": "Card"},
            "descriptions": {"ITEM_1": "Description"},
            "issue_numbers": {"ITEM_1": 1},
        }

    async def get_issue_details(self, issue_id: str):
        _ = issue_id
        return {"id": "ISSUE_1", "title": "Card"}

    async def move_card(self, item_id: str, status: str) -> None:
        _ = (item_id, status)

    async def get_pr_reviews(self, pr_id: str):
        _ = pr_id
        return []

    async def check_mergeability(self, pr_id: str):
        _ = pr_id
        return {"mergeable": True}

    async def squash_merge(self, pr_id: str):
        _ = pr_id
        return {"merged": True}

    async def add_comment(self, subject_id: str, body: str):
        _ = (subject_id, body)
        return {"id": "C1"}


class _Claude:
    async def assess_card_sufficiency(self, card):
        _ = card
        return {"sufficient": True, "questions": []}


class _Agent:
    async def dispatch_card(self, card_context, workspace_info=None):
        _ = (card_context, workspace_info)
        return {"status": "accepted", "session_id": "s1"}

    async def check_health(self):
        return {"status": "accepted"}

    async def relay_feedback(self, review_payload):
        _ = review_payload
        return {"status": "acknowledged"}

    async def check_status(self, session_id: str, **kwargs: object):
        _ = session_id
        return {"status": "working"}


@pytest.mark.asyncio
async def test_dispatch_loop_integration() -> None:
    state = initial_state()
    state.update(
        {
            "github_service": _GitHub(),
            "claude_service": _Claude(),
            "agent_service": _Agent(),
            "notification_service": FakeNotificationService(),
            "human_reviewers": ["alice"],
            "blocked_reminder_hours": 24,
        }
    )

    graph = CoordinareGraphBuilder().build()
    result = await graph.ainvoke(state)

    assert result["phase"] in {"monitoring_agent", "monitoring_performer", "idle"}


# ---------------------------------------------------------------------------
# 020 — Multi-role lifecycle integration with architect
# ---------------------------------------------------------------------------


class _ArchitectAgent:
    """Mock agent that returns plan_committed for the architect role."""

    def __init__(self) -> None:
        self.dispatched: list[dict] = []

    async def dispatch_card(self, card_context, workspace_info=None):
        self.dispatched.append(card_context)
        return {"status": "accepted", "session_id": "arch-1"}

    async def check_health(self):
        return {"status": "accepted"}

    async def relay_feedback(self, review_payload):
        return {"status": "acknowledged"}

    async def check_status(self, session_id: str, **kwargs: object):
        return {
            "status": "plan_committed",
            "plan_path": "docs/coordinare-architecture.md",
            "session_id": session_id,
        }


class _ImplementerAgent:
    """Mock agent that stays working (doesn't complete in one cycle)."""

    def __init__(self) -> None:
        self.dispatched: list[dict] = []

    async def dispatch_card(self, card_context, workspace_info=None):
        self.dispatched.append(card_context)
        return {"status": "accepted", "session_id": "impl-1"}

    async def check_health(self):
        return {"status": "accepted"}

    async def relay_feedback(self, review_payload):
        return {"status": "acknowledged"}

    async def check_status(self, session_id: str, **kwargs: object):
        return {"status": "working", "session_id": session_id}


@pytest.mark.asyncio
async def test_architect_to_implementer_lifecycle() -> None:
    """Integration: architect completes → lifecycle advances → implementer dispatched.

    Tests the multi-role lifecycle by directly invoking monitor_performer and
    dispatch_performer nodes (bypassing the full graph cycle which involves
    check_board and poll_board mocking complexity).
    """
    from coordinare.graph.nodes.dispatch_performer import dispatch_performer
    from coordinare.graph.nodes.monitor_performer import monitor_performer

    architect = _ArchitectAgent()
    implementer = _ImplementerAgent()

    state = initial_state()
    state.update({
        "github_service": _GitHub(),
        "notification_service": FakeNotificationService(),
        "human_reviewers": ["alice"],
        "blocked_reminder_hours": 24,
        "performer_services": {
            "architecting": architect,
            "implementing": implementer,
        },
        "lifecycle_sequence": ["architecting", "implementing"],
        "performer_stage": "architecting",
        "current_card": {"id": "ITEM_1", "status": "IN_PROGRESS"},
        "agent_dispatch": {"session_id": "arch-1"},
    })

    # Step 1: monitor_performer polls architect → plan_committed → advances
    result = await monitor_performer(state)
    assert result["performer_stage"] == "implementing"
    assert result["phase"] == "dispatching"
    card = result.get("current_card") or {}
    assert card.get("plan_path") == "docs/coordinare-architecture.md"

    # Step 2: dispatch_performer dispatches the implementer
    result2 = await dispatch_performer(result)
    assert result2["phase"] == "monitoring_performer"
    assert len(implementer.dispatched) == 1
    impl_payload = implementer.dispatched[0]
    assert impl_payload.get("architecture_plan_path") == "docs/coordinare-architecture.md"
    assert impl_payload.get("role") == "implementing"

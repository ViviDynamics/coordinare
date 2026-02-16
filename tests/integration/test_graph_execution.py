from __future__ import annotations

import pytest

from coordinare.graph.builder import CoordinareGraphBuilder
from coordinare.graph.state import initial_state


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
    async def dispatch_card(self, card_context):
        _ = card_context
        return {"status": "accepted"}

    async def check_health(self):
        return {"status": "healthy"}

    async def relay_feedback(self, review_payload):
        _ = review_payload
        return {"status": "ack"}

    async def check_status(self, card_id: str):
        _ = card_id
        return {"status": "working"}


class _Email:
    async def send_notification(self, recipient, notification):
        _ = (recipient, notification)


class _Slack:
    async def send_notification(self, notification):
        _ = notification


@pytest.mark.asyncio
async def test_dispatch_loop_integration() -> None:
    state = initial_state()
    state.update(
        {
            "github_service": _GitHub(),
            "claude_service": _Claude(),
            "agent_service": _Agent(),
            "email_service": _Email(),
            "slack_service": _Slack(),
            "notification_email": "team@example.com",
            "human_reviewers": ["alice"],
            "blocked_reminder_hours": 24,
        }
    )

    graph = CoordinareGraphBuilder().build()
    result = await graph.ainvoke(state)

    assert result["phase"] in {"monitoring_agent", "idle"}

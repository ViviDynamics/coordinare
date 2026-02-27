from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypedDict

if TYPE_CHECKING:
    from coordinare.models.notification import NotificationEvent


class GitHubServiceProtocol(Protocol):
    async def poll_board(self) -> dict[str, Any]: ...
    async def get_issue_details(self, issue_id: str) -> dict[str, Any]: ...
    async def move_card(self, item_id: str, status: str) -> None: ...
    async def get_pr_reviews(self, pr_id: str) -> list[dict[str, Any]]: ...
    async def check_mergeability(self, pr_id: str) -> dict[str, Any]: ...
    async def squash_merge(self, pr_id: str) -> dict[str, Any]: ...
    async def add_comment(self, subject_id: str, body: str) -> dict[str, Any]: ...


class AgentServiceProtocol(Protocol):
    async def dispatch_card(self, card_context: dict[str, Any]) -> dict[str, Any]: ...
    async def check_health(self) -> dict[str, Any]: ...
    async def relay_feedback(self, review_payload: dict[str, Any]) -> dict[str, Any]: ...
    async def check_status(self, session_id: str) -> dict[str, Any]: ...


class ClaudeServiceProtocol(Protocol):
    async def assess_card_sufficiency(self, card: dict[str, Any]) -> dict[str, Any]: ...


class NotificationServiceProtocol(Protocol):
    async def dispatch(self, event: NotificationEvent) -> None: ...


class CoordinareState(TypedDict, total=False):
    current_card: dict[str, Any] | None
    board_snapshot: dict[str, list[str]]
    phase: Literal["idle", "dispatching", "monitoring_agent", "monitoring_pr", "merging", "relay_feedback", "blocked", "recovery"]
    pending_reviews: list[dict[str, Any]]
    last_poll_at: datetime | None
    error_count: int
    github_field_cache: dict[str, Any]

    github_service: GitHubServiceProtocol
    agent_service: AgentServiceProtocol
    claude_service: ClaudeServiceProtocol
    notification_service: NotificationServiceProtocol

    human_reviewers: list[str]
    blocked_reminder_hours: int

    open_questions: list[str]
    agent_dispatch: dict[str, Any]
    commit_summary: str | None
    agent_health_status: str | None
    last_blocked_notified_at: datetime | None


def initial_state() -> CoordinareState:
    return {
        "current_card": None,
        "board_snapshot": {},
        "phase": "idle",
        "pending_reviews": [],
        "last_poll_at": datetime.now(UTC),
        "error_count": 0,
        "github_field_cache": {},
        "open_questions": [],
    }

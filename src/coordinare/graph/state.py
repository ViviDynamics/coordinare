from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path  # noqa: TC003 — needed at runtime for LangGraph get_type_hints()
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypedDict

from coordinare.config import (
    ProjectConfiguration,  # noqa: TC001 — needed at runtime for LangGraph get_type_hints()
)

if TYPE_CHECKING:
    from coordinare.models.notification import NotificationEvent
    from coordinare.workspace import WorkspaceInfo


class GitHubServiceProtocol(Protocol):
    async def poll_board(self) -> dict[str, Any]: ...
    async def get_issue_details(self, issue_id: str) -> dict[str, Any]: ...
    async def move_card(self, item_id: str, status: str) -> None: ...
    async def get_pr_reviews(self, pr_id: str) -> list[dict[str, Any]]: ...
    async def check_mergeability(self, pr_id: str) -> dict[str, Any]: ...
    async def squash_merge(self, pr_id: str) -> dict[str, Any]: ...
    async def add_comment(self, subject_id: str, body: str) -> dict[str, Any]: ...


class WorkspaceManagerProtocol(Protocol):
    async def prepare(self, card: dict[str, Any]) -> WorkspaceInfo: ...
    async def teardown(self, path: Path) -> None: ...


class AgentServiceProtocol(Protocol):
    async def dispatch_card(
        self,
        card_context: dict[str, Any],
        workspace_info: WorkspaceInfo | None = None,
    ) -> dict[str, Any]: ...
    async def check_health(self) -> dict[str, Any]: ...
    async def relay_feedback(self, review_payload: dict[str, Any]) -> dict[str, Any]: ...
    async def check_status(self, session_id: str, *, payload: dict[str, Any] | None = None) -> dict[str, Any]: ...


class ClaudeServiceProtocol(Protocol):
    async def assess_card_sufficiency(self, card: dict[str, Any]) -> dict[str, Any]: ...


class AssessmentBackendProtocol(Protocol):
    async def assess(self, card: dict[str, Any]) -> dict[str, Any]: ...
    async def prompt(self, text: str, response_format: str | None = None) -> dict[str, Any]: ...


class NotificationServiceProtocol(Protocol):
    async def dispatch(self, event: NotificationEvent) -> None: ...


class AdvocateServiceProtocol(Protocol):
    async def scan_and_respond(self, processed_ids: set[str], *, persona_instructions: str = "") -> set[str]:
        """Scan open issues, process unhandled ones, return updated processed_ids set."""
        ...


class CoordinareState(TypedDict, total=False):
    current_card: dict[str, Any] | None
    board_snapshot: dict[str, list[str]]
    phase: Literal["idle", "dispatching", "monitoring_agent", "monitoring_performer", "monitoring_pr", "merging", "relay_feedback", "blocked", "recovery", "system_error"]
    pending_reviews: list[dict[str, Any]]
    last_poll_at: datetime | None
    error_count: int
    github_field_cache: dict[str, Any]

    github_service: GitHubServiceProtocol
    agent_service: AgentServiceProtocol
    claude_service: ClaudeServiceProtocol
    assessment_backend: AssessmentBackendProtocol
    notification_service: NotificationServiceProtocol
    advocate_service: AdvocateServiceProtocol | None
    advocate_history: set[str]
    advocate_handled_label: str
    advocate_escalation_label: str

    workspace_manager: WorkspaceManagerProtocol | None
    workspace_path: Path | None
    workspace_branch: str | None

    config: ProjectConfiguration | None
    config_path: Path | None

    human_reviewers: list[str]
    trusted_bot_reviewers: list[str]
    blocked_reminder_hours: int

    open_questions: list[str]
    card_clarifications: list[dict]  # [{"questions": [...], "answer": str}]
    agent_dispatch: dict[str, Any]
    system_error_count: int
    system_error_last_at: datetime | None
    system_error_reason: str | None
    system_error_notified: bool
    agent_dispatch_at: datetime | None
    performer_events: list[dict]
    performer_metrics: dict | None
    commit_summary: str | None
    agent_health_status: str | None
    last_blocked_notified_at: datetime | None

    # 019 — Performer Lifecycle
    performer_stage: str  # Active role in the lifecycle (e.g. "implementing", "reviewing")
    performer_services: dict[str, Any]  # stage name (e.g. "implementing", "reviewing") → AgentService instance
    lifecycle_sequence: list[str]  # Ordered list of role stage names to execute
    relay_feedback: list[dict[str, Any]]  # PR comments to relay on next dispatch
    role_timeouts: dict[str, int]  # 027: stage name → timeout seconds
    phase_entered_at: datetime | None  # 028: timestamp when current phase was entered
    requirements_changed: bool  # 030: True if card requirements changed during monitoring
    requirements_changed_details: dict[str, Any]  # 030: diff details
    pending_override: dict[str, Any] | None  # 031: human override queued via dashboard or PR comment
    lifecycle_completed_at: datetime | None  # Cutoff for filtering old PR reviews after lifecycle completion
    processed_review_ids: set[str]  # Review node IDs already processed — prevents re-dispatch loops
    card_tokens_total: int  # 034: accumulated token count for current card
    card_cost_estimate: float  # 034: estimated cost in dollars
    card_budget_alert_sent: bool  # 034: True if budget exceeded notification was sent
    active_sessions: dict[str, Any]  # 035: card-ID → CardSession for multi-card parallelism


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
        "system_error_count": 0,
        "system_error_notified": False,
        "card_clarifications": [],
        "performer_events": [],
        "advocate_history": set(),
        "advocate_handled_label": "",
        "advocate_escalation_label": "",
        "workspace_manager": None,
        "workspace_path": None,
        "workspace_branch": None,
        "performer_stage": "implementing",
        "performer_services": {},
        "lifecycle_sequence": ["implementing"],
        "relay_feedback": [],
        "role_timeouts": {},
        "pending_override": None,
        "card_tokens_total": 0,
        "card_cost_estimate": 0.0,
        "card_budget_alert_sent": False,
        "active_sessions": {},
    }

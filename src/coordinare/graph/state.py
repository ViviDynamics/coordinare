from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dc_field
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


# ---------------------------------------------------------------------------
# 057 — Symphony Management & Multi-Project Orchestration
# ---------------------------------------------------------------------------


@dataclass
class SymphonyRuntimeState:
    """Per-symphony runtime tracking (spec 057)."""

    name: str
    last_poll_at: datetime | None = None
    active_card: dict[str, Any] | None = None
    active_sessions: dict[str, Any] = dc_field(default_factory=dict)
    cycle_count: int = 0
    error_count: int = 0
    last_error: str | None = None
    board_snapshot: dict[str, list[str]] | None = None
    session_skip_reasons: dict[str, Any] | None = None
    previous_phase: str | None = None


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
    # 045: Number of times reviewer/security/qa has returned a non-terminal
    # "changes_requested" / "_failed" marker for this card, routing back to
    # an earlier stage (usually implementer).  Bounded by
    # config.max_feedback_cycles — when the limit is hit the card is blocked
    # with a diagnostic question instead of looping indefinitely.  Performance-
    # local REVIEWER_MAX_CYCLES / SECURITY_MAX_CYCLES / QA_MAX_CYCLES reset
    # on every dispatch (fresh Performance instance), so the bound has to
    # live on the coordinare side per-card.
    feedback_cycle_count: int
    # 046: Dependency state for the current card — list of unsatisfied blocker
    # dicts [{issue_number, title, column, issue_url, source}].  Populated by
    # check_board's dependency filtering and consumed by dashboard + notifications.
    blocked_by_dependencies: list[dict[str, Any]]
    # 047: Auto-rebase — track main HEAD SHA to detect external merges, and
    # store the last rebase round for dashboard display.
    last_known_main_sha: str | None
    last_rebase_round: dict | None
    # 048: Per-role performer slot manager.  Typed as Any because LangGraph's
    # get_type_hints() resolves annotations at runtime — a TYPE_CHECKING
    # import of SlotManager would cause NameError.
    slot_manager: Any
    # 052: Backend transparency — live URL and session stats from the performer backend.
    backend_ui_url: str | None
    session_stats: Any  # SessionStats | None; typed Any — LangGraph resolves annotations at runtime
    # Deferred GitHub retries for transient outages (DNS/service down/circuit open).
    github_retry_queue: list[dict[str, Any]]
    github_retry_after: datetime | None
    # 054: Per-cycle eligibility skip map — card_id → {reason, detail, blockers}.
    # Cleared at cycle start; populated by _invoke_multi_session for ineligible sessions.
    session_skip_reasons: dict[str, dict[str, Any]]
    # 055: QA screenshot results from most recent capture pass.
    qa_screenshots: list[Any]
    # 055: Issue comment idempotency fields.
    last_issue_comment_id: int | None
    processed_issue_comment_ids: set[int]
    # 056: Containerized performer endpoint registry — id → PerformerEndpointState.
    # Typed as Any because LangGraph's get_type_hints() resolves annotations at
    # runtime; a TYPE_CHECKING import would NameError. Reset on coordinare restart.
    performer_endpoints: dict[str, Any]

    # 057: Multi-symphony orchestration
    symphony_configs: dict[str, Any]           # name → SymphonyConfig
    symphony_states: dict[str, Any]            # name → SymphonyRuntimeState
    current_symphony: str | None               # symphony currently being orchestrated
    orchestra_config: Any                      # OrchestraConfig
    config_version: int                        # incremented on hot-reload
    coordinare_config: Any                      # CoordinareConfiguration (full config object)
    config_mode: str                           # "legacy" or "multi_symphony"
    symphony_github_services: dict[str, Any]   # name → GitHubService
    symphony_workspace_managers: dict[str, Any]  # name → WorkspaceManager

    # 060: Per-symphony env-cache state (name → EnvCacheState)
    env_cache: dict[str, Any]


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
        "feedback_cycle_count": 0,
        "blocked_by_dependencies": [],
        "last_known_main_sha": None,
        "last_rebase_round": None,
        "slot_manager": None,
        "backend_ui_url": None,
        "session_stats": None,
        "github_retry_queue": [],
        "github_retry_after": None,
        "session_skip_reasons": {},
        "qa_screenshots": [],
        "last_issue_comment_id": None,
        "processed_issue_comment_ids": set(),
        "performer_endpoints": {},
        "symphony_configs": {},
        "symphony_states": {},
        "current_symphony": None,
        "orchestra_config": None,
        "config_version": 0,
        "coordinare_config": None,
        "config_mode": "legacy",
        "symphony_github_services": {},
        "symphony_workspace_managers": {},
        "env_cache": {},
    }

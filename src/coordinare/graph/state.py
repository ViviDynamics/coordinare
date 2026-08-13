from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import UTC, datetime
from pathlib import Path  # noqa: TC003 — needed at runtime for LangGraph get_type_hints()
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypedDict, runtime_checkable

from coordinare.config import (
    ProjectConfiguration,  # noqa: TC001 — needed at runtime for LangGraph get_type_hints()
)
from coordinare.services.activity_log import (
    ActivityLog,  # noqa: TC001 — needed at runtime for LangGraph get_type_hints()
)

if TYPE_CHECKING:
    from coordinare.models.notification import NotificationEvent
    from coordinare.workspace import WorkspaceInfo


@runtime_checkable
class GitHubServiceProtocol(Protocol):
    """The full surface the coordinare lifecycle depends on from the GitHub service.

    This is the single documented contract that both the real ``GitHubService``
    (``services/github.py``) and the benchmark's ``FakeGitHubService``
    (``services/fake_github.py``, spec 134) satisfy.  It is the node-called
    surface only — every method here is actually invoked by a graph node (see
    ``specs/134-board-sim-benchmark/contracts/github-service-protocol.md``) — so
    the real class already duck-types it and completing it caused no behaviour
    change.  Signatures mirror the real service.

    Deliberately excluded: ``post_comment`` (a latent, swallowed call in
    monitor_performer that does NOT exist on the real service — the fake provides
    it as an extra); and the duck-typed attribute surface a few nodes reach for
    (``_org`` / ``_project_name`` / ``project_id`` reads and the
    ``_pr_checks_service_cache`` write) — those aren't structural members
    (``_pr_checks_service_cache`` isn't set until a node writes it) and are
    covered by the conformance test separately.
    """

    # Board / issues
    async def poll_board(self) -> dict[str, Any]: ...
    async def move_card(self, item_id: str, status: str) -> None: ...
    async def get_issue_details(self, issue_id: str) -> dict[str, Any]: ...
    async def check_issue_state(self, repo: str, issue_number: int) -> str: ...
    async def get_issue_comments(self, issue_number: int, since_id: int | None = None) -> list[dict[str, Any]]: ...
    async def list_open_issues(self, owner: str, repo: str, first: int = 20) -> list[dict[str, Any]]: ...
    async def link_to_project(self, content_id: str, status: str = "IN_REVIEW") -> str | None: ...

    # PR / review
    async def find_pr_for_issue(self, issue_node_id: str) -> dict[str, str] | None: ...
    async def count_closed_prs_for_issue(self, issue_node_id: str) -> int: ...
    async def get_pr_reviews(self, pr_id: str) -> list[dict[str, Any]]: ...
    async def get_pr_review_context(self, pr_id: str) -> dict[str, Any]: ...
    async def request_reviews(self, pr_id: str, reviewer_logins: list[str]) -> dict[str, Any]: ...
    async def request_reviewers(self, owner: str, repo: str, pr_number: int, reviewers: list[str]) -> None: ...
    async def check_mergeability(self, pr_id: str) -> dict[str, Any]: ...
    async def squash_merge(self, pr_id: str) -> dict[str, Any]: ...
    async def add_comment(self, subject_id: str, body: str) -> dict[str, Any]: ...
    async def get_pr_files(self, owner: str, repo: str, pr_number: int) -> dict[str, Any]: ...
    async def get_pr_diff(self, pr_url: str) -> tuple[str, list[str]]: ...
    async def compare_changed_files(self, pr_url: str, base_sha: str, head_sha: str) -> list[str]: ...

    # CI / checks
    async def get_required_status_checks(self, owner: str, repo: str, default_branch: str) -> set[str] | None: ...
    async def fetch_failed_job_log(self, owner: str, repo: str, job_id: int, max_chars: int = 6000) -> str: ...

    # Branches / repo / files
    async def branch_exists(self, branch_name: str) -> bool: ...
    async def branch_has_open_pr(self, branch_name: str) -> bool | None: ...
    async def delete_branch(self, branch_name: str) -> None: ...
    async def get_repository_id(self, owner: str, repo: str) -> str: ...
    async def get_file_content(self, owner: str, repo: str, path: str, ref: str = "HEAD") -> str | None: ...
    async def get_file_blob_sha(self, owner: str, repo: str, path: str, ref: str = "HEAD") -> str | None: ...
    async def list_prs_by_branch_prefix(
        self, owner: str, repo: str, prefix: str, state: str = "OPEN", limit: int = 20
    ) -> list[dict[str, Any]]: ...

    # Labels
    async def get_label_ids(self, owner: str, repo: str) -> dict[str, str]: ...
    async def ensure_labels_exist(
        self, owner: str, repo: str, handled_label: str, escalation_label: str
    ) -> dict[str, str]: ...
    async def add_labels(self, issue_id: str, label_ids: list[str]) -> None: ...

    # Lifecycle / auth
    async def initialize(self) -> None: ...
    async def aclose(self) -> None: ...
    async def current_token(self) -> str: ...
    # Private but reached via hasattr() for the rebase token path (merge_pr,
    # check_board, dispatch_performer) — part of the contract the fake must meet.
    async def _current_token(self) -> str: ...


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


class ConductingBackendProtocol(Protocol):
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
    # 062: Per-card metadata for dashboard swimlane rendering (keyed by item_id).
    board_titles: dict[str, str] = dc_field(default_factory=dict)
    board_issue_numbers: dict[str, int] = dc_field(default_factory=dict)
    board_issue_urls: dict[str, str] = dc_field(default_factory=dict)
    board_pr_urls: dict[str, str] = dc_field(default_factory=dict)


class CoordinareState(TypedDict, total=False):
    # 066 (FR-010): current_card is a *derived mirror* of
    # active_sessions[active_card_id]["current_card"].  The only sanctioned
    # mutation site is _rederive_current_card() below; all other writers MUST
    # mutate the session entry instead.  See
    # specs/066-unify-card-pickup/contracts/current_card-derivation.md.
    current_card: dict[str, Any] | None
    board_snapshot: dict[str, list[str]]
    phase: Literal["idle", "dispatching", "monitoring_agent", "monitoring_performer", "monitoring_pr", "merging", "relay_feedback", "blocked", "recovery", "system_error"]
    pending_reviews: list[dict[str, Any]]
    last_poll_at: datetime | None
    error_count: int
    github_field_cache: dict[str, Any]

    github_service: GitHubServiceProtocol
    agent_service: AgentServiceProtocol
    conducting_backend: ConductingBackendProtocol
    notification_service: NotificationServiceProtocol
    # 138: dashboard activity feed. Optional by construction (total=False) —
    # every reader uses state.get("activity_log") and no-ops when it is None.
    activity_log: ActivityLog | None
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
    # 069 FR-004: per-session Slack-delivery watermark for card_blocked.
    # Lives on CardSession; mirrored here as part of the flat-state round-trip
    # so the cooldown gate in notify survives a cycle.
    last_blocked_slack_delivered_at: datetime | None
    # 072 FR-072-8..11: head-delta audit trail (see PersistedSession).
    head_at_dispatch: str | None
    head_at_last_turn: str | None
    # 072 FR-072-5(c): count of clarifications appended *before* the
    # current performer turn started.  Snapshotted by dispatch_performer
    # so the per-role zero-progress guardrail can detect whether new
    # clarifications were appended (e.g. by route_issue_comments or
    # check_board) while the performer was running.  Treated as a
    # progress signal — if the count grew, the guardrail does not trip.
    clarifications_count_at_dispatch: int

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
    surfaced_stale_reviews: dict[str, str]  # 128: {review_id: head_oid} — stale-review surfacing dedup
    card_tokens_total: int  # 034: accumulated token count for current card
    card_cost_estimate: float  # 034: estimated cost in dollars
    card_budget_alert_sent: bool  # 034: True if budget exceeded notification was sent
    active_sessions: dict[str, Any]  # 035: card-ID → CardSession for multi-card parallelism
    # 066: Index pointer into active_sessions for the card whose per-session
    # graph step is currently executing.  Set at the start of each
    # _invoke_multi_session per-session step; consulted by
    # _rederive_current_card() to refresh the top-level current_card mirror
    # (FR-010).  None outside of per-session steps and when no card is in flight.
    active_card_id: str | None
    # 045: Number of times reviewer/security/qa has returned a non-terminal
    # "changes_requested" / "_failed" marker for this card, routing back to
    # an earlier stage (usually implementer).  Bounded by
    # config.max_feedback_cycles — when the limit is hit the card is blocked
    # with a diagnostic question instead of looping indefinitely.  Performance-
    # local REVIEWER_MAX_CYCLES / SECURITY_MAX_CYCLES / QA_MAX_CYCLES reset
    # on every dispatch (fresh Performance instance), so the bound has to
    # live on the coordinare side per-card.
    feedback_cycle_count: int
    # 065 US4 — monotonic feedback / triage stats.  feedback_cycle_count
    # above is the *operative* counter that resets on un-block (BLOCKED → TODO).
    # total_feedback_cycles increments on every feedback round for the card
    # lifetime and never resets; triage_blocks increments each time the card
    # is moved to BLOCKED for feedback exhaustion.  Both surface on the
    # dashboard so the operator can see cumulative churn across un-blocks.
    total_feedback_cycles: int
    triage_blocks: int
    # 123: split bounce budget.  content_feedback_cycles counts content-driven
    # feedback rounds (reviewer/QA changes_requested) and is checked against
    # config.max_feedback_cycles; transient_error_cycles counts infra/transient
    # failures (env_blocked/system_error/unknown) and is checked against a
    # separate limit (3).  Both live on CardSession + PersistedSession and
    # round-trip through session ↔ state.  Splitting the old feedback_cycle_count
    # stops an infra-flaky card being escalated on content grounds (and reverse).
    content_feedback_cycles: int
    transient_error_cycles: int
    # 123: answered assessor Q&A ({"question","answer"} dicts) carried across
    # bounce cycles; injected as prior_clarifications on assessor re-dispatch.
    # Distinct from open_questions (list[str], blocked-card diagnostics) above.
    assessor_open_questions: list[dict]
    # 125: stage-verdict memory.  {stage: {"head_sha","verdict","recorded_at"}}
    # plain dicts; written by monitor_performer on passing terminal markers,
    # consulted by dispatch_performer's verdict-cache skip.  Round-trips
    # session ↔ state and persists (schema v14).
    stage_verdicts: dict[str, dict[str, Any]]
    # 125: one-shot veto — set to the target stage by _apply_pending_override's
    # "restart" action so the verdict cache never suppresses an operator-
    # requested run.  Consumed (cleared) by the cache check.  Transient:
    # deliberately NOT persisted (a crash loses only the forcing, never a
    # verdict).
    override_forced_dispatch: str | None
    # 126: terminal-success-floor state ({id, raiser, origin_sha, body_digest,
    # disposition, dispute_reason, re_raised, round_status} plain dicts +
    # the current round's origin head + the bounded no-op retry counter).
    # Written by monitor_performer's bounce stamping and floor; consumed by
    # dispatch_performer's dispute injection and the 125 cache veto.
    feedback_ledger: list[dict[str, Any]]
    feedback_origin_sha: str | None
    noop_success_retries: int
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
    # EnvCacheService instance — declared here so LangGraph preserves it across
    # graph.ainvoke() cycles (unknown keys are dropped during state merge).
    env_cache_service: Any
    # 088 (US5): async callable the daemon wires to an immediate state-store
    # save.  Bootstrap completion changes no lifecycle signature, so without an
    # explicit flush a recorded success only reaches disk on the NEXT stage
    # transition — a restart in that window loads last_bootstrap_succeeded=False
    # and re-runs a full bootstrap for nothing.
    snapshot_save_fn: Any
    # Performer service registry, keyed by performer id — same reason as above.
    performer_services_by_id: dict[str, Any]

    # 064: Closer PR-checks gate — per-card cache of last poll for tick fast-path.
    # Keyed by card_id; resets when PR HEAD SHA changes.
    card_checks_state: dict[str, dict[str, Any]]

    # 062: Per-card metadata from the latest poll_board.  Stashed on top-level
    # state so the daemon's post-cycle sync can copy them onto SymphonyRuntimeState
    # for the dashboard swimlane (titles + GitHub links).  Declared here so
    # LangGraph propagates them out of graph.ainvoke() — without these fields
    # in the TypedDict the keys are dropped during state merge.
    _board_titles: dict[str, str]
    _board_issue_numbers: dict[str, int]
    _board_issue_urls: dict[str, str]
    _board_pr_urls: dict[str, str]


def initial_state() -> CoordinareState:
    return {
        "current_card": None,
        "board_snapshot": {},
        "phase": "idle",
        "pending_reviews": [],
        "last_poll_at": datetime.now(UTC),
        "error_count": 0,
        "github_field_cache": {},
        "surfaced_stale_reviews": {},
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
        "active_card_id": None,
        "feedback_cycle_count": 0,
        "total_feedback_cycles": 0,
        "triage_blocks": 0,
        # 123: split bounce budget + assessor Q&A carryover
        "content_feedback_cycles": 0,
        "transient_error_cycles": 0,
        "assessor_open_questions": [],
        # 125: stage-verdict memory + one-shot override forcing flag
        "stage_verdicts": {},
        "override_forced_dispatch": None,
        # 126: terminal-success-floor state
        "feedback_ledger": [],
        "feedback_origin_sha": None,
        "noop_success_retries": 0,
        "blocked_by_dependencies": [],
        "last_known_main_sha": None,
        "last_rebase_round": None,
        "slot_manager": None,
        "backend_ui_url": None,
        "session_stats": None,
        "github_retry_queue": [],
        "github_retry_after": None,
        "session_skip_reasons": {},
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
        "env_cache_service": None,
        "performer_services_by_id": {},
        "card_checks_state": {},
        # 076 dispatcher dedup — defaults match CardSession initialisation
        # in coordinare.session.create_session_from_card so a round-trip
        # through session_to_state / state_to_session is a no-op.
        "idle_timeout_retries": {},
        "pr_artefacts_recorded_at": None,
        "multi_pr_divergence": None,
        "wedge_count_window": {},
        "reconciliation_decisions_last_startup": {},
    }


def _set_current_card(state: CoordinareState, card: dict) -> None:
    """066 FR-010: single write-site for current_card.

    Mutates the active session entry's ``current_card`` (creating a transient
    entry if active_card_id is unset, e.g. during cold start) and re-derives
    the flat mirror. All node-level writes that previously did
    ``state["current_card"] = card`` MUST go through this helper.

    For retirement (clearing the card), call :func:`_retire_active_session`
    directly — this helper is for *writing* a card, not clearing one.
    """
    sessions = state.get("active_sessions")
    if sessions is None:
        sessions = {}
        state["active_sessions"] = sessions
    active_id = state.get("active_card_id")
    card_id = str(card.get("id", ""))
    if card_id and (not active_id or active_id != card_id):
        # Transition active slot to the card being written (also covers
        # cold-start writes where active_card_id was never set).
        state["active_card_id"] = card_id
        active_id = card_id
    if active_id:
        if active_id not in sessions:
            sessions[active_id] = {}
        sessions[active_id]["current_card"] = card
    _rederive_current_card(state)


def _retire_active_session(state: CoordinareState) -> None:
    """066 FR-010: retire the active session and clear the derived mirror.

    Stronger than a mirror-only clear: this *removes* the active_sessions
    entry, drops active_card_id, and re-derives current_card to None.  Use at
    session-retirement points (card cancelled, completed, board-reconciled
    away).  For mirror-only clears where the session must survive, mutate the
    session entry directly and call :func:`_rederive_current_card`.

    Idempotent: calling on a state without an active session is a no-op
    (no spurious empty-dict materialisation, no mirror rewrite).
    """
    sessions = state.get("active_sessions")
    active_id = state.get("active_card_id")
    if not sessions and active_id is None and state.get("current_card") is None:
        return
    if sessions is None:
        sessions = {}
        state["active_sessions"] = sessions
    if active_id and active_id in sessions:
        del sessions[active_id]
    state["active_card_id"] = None
    _rederive_current_card(state)


def _rederive_current_card(state: CoordinareState) -> None:
    """Re-derive the top-level current_card mirror from active_sessions.

    Single mutation site for state["current_card"] per spec 066 FR-010.
    Idempotent: calling twice in sequence with no other mutation produces the
    same state.  See specs/066-unify-card-pickup/contracts/current_card-derivation.md.

    Post-conditions:
      - state["current_card"] is None when active_card_id is None or absent
        from active_sessions.
      - When non-None, state["current_card"] shares dict identity with
        state["active_sessions"][state["active_card_id"]]["current_card"].
    """
    sessions = state.get("active_sessions") or {}
    active_id = state.get("active_card_id")
    if active_id and active_id in sessions:
        state["current_card"] = sessions[active_id].get("current_card")
    else:
        state["current_card"] = None

from __future__ import annotations

import os
import tempfile
import time
from datetime import datetime  # noqa: TC003 — Pydantic needs this at runtime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import structlog
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

if TYPE_CHECKING:
    from coordinare.metrics import CoordinareMetrics

logger = structlog.get_logger(__name__)

CURRENT_SCHEMA_VERSION: int = 24  # 343: workflow_step trail for per-performer position

# Lowest schema_version we still know how to read.  v1 snapshots are upgraded
# in-memory at load time (065 Fix 7b: active_sessions added in v2; v1 snapshots
# simply restore with an empty active_sessions dict and rely on board re-adopt).
# v3 adds env_cache; older snapshots load with an empty env_cache dict and the
# first poll cycle re-fetches the SHA from GitHub.
# v4 (074) adds optional persona_scope on PersistedSession; v1-v3 snapshots load
# with persona_scope = None and the next cycle recomputes (FR-011).
# v5 (075) adds bounce_counter on PersistedSession; v1-v4 snapshots load with
# bounce_counter = {} and the next gate decision populates the head SHA entry.
# v6 (075 fix) adds ci_gate_rollup_signature on PersistedSession; v1-v5
# snapshots load with None and the first HOLD/BOUNCE/ESCALATE cycle re-posts.
# v7 (076) adds dispatcher-dedup fields on PersistedSession:
# idle_timeout_retries (per-(card,stage) rolling counter), pr_artefacts_recorded_at
# (FR-016 audit timestamp), multi_pr_divergence (FR-024 surfaced record),
# wedge_count_window (FR-020 promotion threshold tracking), and
# reconciliation_decisions_last_startup (per-card decision audit trail).  v1-v6
# snapshots load with all five fields at their safe empty defaults.
# v8 (089) adds local_fix_counter on PersistedSession (implementer local-test
# self-fix budget, parallel to bounce_counter); v1-v7 snapshots load with {}.
# v9 (090) adds inheritance_repair_counter (per-HEAD INHERITED-failure repair
# budget, parallel to bounce_counter/local_fix_counter) and repair_audit (the
# RepairDecisionRecord trail) on PersistedSession; v1-v8 snapshots load with {}
# and [] respectively.  An empty counter means zero attempts taken (not
# unlimited) — the configured per-head budget still applies (FR-026, SC-009).
# v10 (095) adds env_blocked on PersistedSession (per-card ENV_BLOCKED
# hold/notification-dedup state: head_sha, pattern_id, cause, action); v1-v9
# snapshots load with None so the first ENV_BLOCKED hold notifies once and
# repopulates it.  Carries only check/infra identifiers — never secret values.
# v11 (096) adds top-level last_known_main_sha (the main SHA the rebase trigger
# last reconciled against — persisted so a cross-restart main advance is seen as
# drift) and per-card last_rebase_attempt on PersistedSession (anti-thrash marker:
# main_sha, head_sha, outcome); v1-v10 snapshots load with None for both, so the
# first post-upgrade reconciliation heals any conflicting branch.  SHAs / branch
# names / outcome strings only — never secret values.
# v12 (123) adds three fields on PersistedSession: content_feedback_cycles and
# transient_error_cycles (the split bounce budget — content-driven feedback vs
# infra/transient failures, each with an independent exhaustion check) and
# assessor_open_questions (answered assessor Q&A carried across bounce cycles,
# a list of {"question", "answer"} dicts, injected as prior_clarifications on
# assessor re-dispatch — deliberately distinct from the existing
# open_questions: list[str] blocked-card diagnostic surface).  v1-v11 snapshots
# load with 0/0/[]; a legacy feedback_cycle_count value is migrated into
# content_feedback_cycles on load (see the model_validator below).
# v13 (124) adds docs/wiki wiki-init fields on EnvCacheStateSnapshot:
# wiki_initialized (durable marker that the seed wiki merged), wiki_attempts +
# wiki_exhausted (the FR-017 circuit breaker), and last_wiki_init_at/succeeded/
# error. v1-v12 snapshots load with False/0/None so a symphony with no wiki yet
# initializes on first cycle (when the gate is enabled). wiki_in_flight is
# transient and never persisted (rederived False after restart, like
# bootstrap_in_flight).
# v14 (125) adds three fields on PersistedSession: stage_verdicts
# (per-verdict-stage {head_sha, verdict, recorded_at} slots — the stage-verdict
# memory that lets dispatch skip re-running a stage whose passing verdict
# already covers the current PR head), plus processed_issue_comment_ids
# (bounded list of already-classified issue-comment IDs) and
# last_issue_comment_id (the since_id fetch watermark) — both per-card, since
# the comment router reads the active card's linked issue — so restarts stop
# re-classifying processed comments.  v1-v13 snapshots load with {}/[]/None.
# SHAs, marker strings and numeric comment IDs only — never secret values.
# v15 (126) adds three fields on PersistedSession: feedback_ledger (the
# terminal-success-floor feedback contract: per-item {id, raiser, origin_sha,
# body_digest, disposition, dispute_reason, re_raised, round_status} records,
# pruned to the current + previous round), feedback_origin_sha (the head the
# current feedback round was raised against — the implementer progress floor's
# comparison reference) and noop_success_retries (bounded strengthened-
# re-dispatch counter).  v1-v14 snapshots load with []/None/0.  Body digests
# are capped at 200 chars — never full comment bodies, never secret values.
# v17 (165) adds two fields on PersistedSession: blueprint (the architect
# workflow's validated blueprint plus size, blueprint_hash and created_at; the
# single source the implementer, documenter and QA briefs are projected from)
# and documenting_side (the out-of-lifecycle documenter run: status,
# blueprint_hash, session_id, head_sha, result_reason). v1-v16 snapshots load
# with None for both. Plan text and SHAs only; never secret values.
# v18 (166) adds assessment on PersistedSession: the assessor workflow's
# structured product reading (goal, expected_behavior, out_of_scope, questions,
# assumptions, criteria with their source, and carried clarifications) plus
# assessment_hash and created_at timestamp. v1-v17 snapshots load with None;
# a malformed record (missing goal or ready field) drops to None on load, never
# failing the snapshot. Only the goal and ready fields are required for
# validation; other missing fields trigger the drop. Assessment text and
# hash only; never secret values.
# v19 (169) adds review_findings on PersistedSession: the reviewer workflow's
# structured findings record (changed_files, findings, dispositions, coverage
# pass outcome, verdict, and posting result) lifted when the reviewer reports
# changes_requested. v1-v18 snapshots load with None; a malformed record
# (not a dict, missing changed_files or verdict) drops to None on load, never
# failing the snapshot. Review findings are cleared when the reviewer is
# dispatched (reset_review_findings_for_reviewer) and injected into the
# implementing stage only (inject_review_findings). Finding anchors and verdict
# text only; never secret values.
MIN_SUPPORTED_SCHEMA_VERSION: int = 1

WorkflowPhase = Literal[
    "idle",
    "dispatching",
    "monitoring_agent",
    "monitoring_performer",
    "monitoring_pr",
    "merging",
    "relay_feedback",
    "blocked",
    "recovery",
    "system_error",
]


class RepairDecisionRecord(BaseModel):
    """One decision in the L3 INHERITED-failure repair audit trail (090, FR-023).

    Frozen + ``extra="forbid"``: every repair attempt appends an immutable record
    of what the autonomy layer decided and why, so an operator can reconstruct the
    full dispatch → guard → outcome sequence for any head SHA from the snapshot
    alone (data-model.md §9).  ``decided_at`` is stamped by the node that appends
    the record (not a pure path), hence a plain ISO-8601 string.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    head_sha: str
    attempt: int  # 1-based; the dispatch this decision belongs to
    kind: Literal[
        "dispatch",
        "static_guard",
        "reviewer",
        "acceptance",
        "rejection",
        "escalation",
    ]
    is_safe: bool | None = None  # set for static_guard / reviewer kinds
    flagged_patterns: list[str] = Field(default_factory=list)  # guard reasons, if any
    detail: str | None = None  # escalation/rejection reason, free text
    decided_at: str  # ISO-8601 stamp (set by the node, not a pure path)


class StageVerdict(BaseModel):
    """One recorded passing verdict for a lifecycle stage (125, FR-001).

    Single slot per verdict stage on ``PersistedSession.stage_verdicts`` —
    overwritten by each new passing verdict.  ``recorded_at`` is observability
    only; skip decisions compare ``head_sha`` against the live remote head,
    never times.  Frozen + ``extra="forbid"`` like RepairDecisionRecord.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    head_sha: str = Field(min_length=1)
    verdict: str = Field(min_length=1)
    recorded_at: str  # ISO-8601 stamp (set by the node, not a pure path)


class DocumentingSideRun(BaseModel):
    """Early documenter identity and outcome, deduplicated by blueprint and findings.

    Runner job_id restores HTTP polling after restart. Unknown writer status
    retains the lock until a poll or confirmed stop proves quiescence.
    """

    model_config = ConfigDict(extra="ignore")

    status: Literal["pending", "running", "done", "failed"] = "pending"
    blueprint_hash: str
    writer_active: bool = False
    findings_hash: str | None = None
    paths: list[str] = Field(default_factory=list)
    dispatched_at: datetime | None = None
    session_id: str | None = None
    job_id: str | None = None
    head_sha: str | None = None
    result_reason: str | None = None


class FeedbackItemRecord(BaseModel):
    """One feedback item in the terminal-success-floor ledger (126).

    Stamped at bounce time; disposition updated from the implementer's
    completion contract; rounds resolved by the raising stage's next verdict.
    ``body_digest`` is capped at stamp time (≤200 chars) — never the full
    comment body.  ``extra="forbid"`` like StageVerdict/RepairDecisionRecord;
    malformed entries are dropped at load (bad entry == no entry).
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    raiser: str = Field(min_length=1)  # raising stage, or "ci" for CI-gate items
    origin_sha: str = ""  # "" when unresolvable at bounce time -> floor fails open
    body_digest: str = ""
    disposition: Literal[
        "open",
        "addressed",
        "disputed",
        "dispute_accepted",
        "dispute_rejected",
        "superseded",
    ] = "open"
    dispute_reason: str = ""
    re_raised: bool = False
    round_status: Literal["current", "previous"] = "current"


class PersistedSession(BaseModel):
    """Durable per-card session state (065 Fix 7b).

    Mirrors the subset of `CardSession` fields whose loss across restart would
    change behaviour (most importantly `performer_stage`, which decides whether
    a re-adopted IN_PROGRESS card resumes at the implementer or closer).
    Transient fields (performer_events, performer_metrics, workspace_path,
    agent_dispatch) are intentionally omitted — they are re-derived from the
    live performer container or rebuilt from scratch.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    card_id: str
    performer_stage: str | None = None
    phase: str | None = None
    lifecycle_completed_at: datetime | None = None
    processed_review_ids: list[str] = Field(default_factory=list)
    # 128: dedup marker for stale-review surfacing — {gating_review_id: head_oid}
    # at the moment we re-requested/notified. Re-fire only when absent or the
    # head has advanced past the recorded oid. Backward-compatible default.
    surfaced_stale_reviews: dict[str, str] = Field(default_factory=dict)
    open_questions: list[str] = Field(default_factory=list)
    card_clarifications: list[dict] = Field(default_factory=list)
    relay_feedback: list[dict] = Field(default_factory=list)
    system_error_count: int = 0
    system_error_reason: str | None = None
    system_error_notified: bool = False
    requirements_changed: bool = False
    # 331: identity of the requirements the persisted blueprint was planned
    # against. Lets a restart reuse a still-valid plan instead of re-running
    # the architect. v1-v22 snapshots load with None and simply re-plan once.
    blueprint_signature: str | None = None
    # 069: per-card blocked-notification watermark.  Mirrors
    # ``CardSession.last_blocked_notified_at`` so a restart does not lose the
    # dedup gate that prevents re-spamming Slack and re-posting the GitHub
    # reminder comment for a card that was already announced as blocked.
    # Optional / default ``None`` keeps v1 snapshots loading unchanged.
    last_blocked_notified_at: datetime | None = None
    # 069 FR-004: per-card watermark for Slack ``card_blocked`` delivery.
    # ``last_blocked_notified_at`` is written on every pass through
    # handle_blocked (it doubles as the check_board cutoff + GitHub 24h dedup
    # gate) and therefore cannot prove Slack actually went out.  This field is
    # written ONLY when notify.dispatch() succeeds for ``card_blocked``, so
    # FR-004's post-restart suppression has a truthful signal that survives
    # the in-memory NotificationHistory loss across restarts.
    last_blocked_slack_delivered_at: datetime | None = None
    # 072 FR-072-8..11: head-delta audit trail. ``head_at_dispatch`` is the
    # branch HEAD captured the first time this card was dispatched to a
    # performer in its current lifecycle pass; ``head_at_last_turn`` is the
    # most recent ``head_after`` reported by a terminal performer response.
    # Optional / default ``None`` keeps v1/v2 snapshots loading unchanged.
    head_at_dispatch: str | None = None
    head_at_last_turn: str | None = None
    # 074 FR-011: per-card persona-scope classification (schema v4+).  Optional /
    # default ``None`` keeps v1-v3 snapshots loading unchanged; the next cycle's
    # classify_scope node recomputes from scratch.  Stored as a plain dict for
    # JSON portability — the TypedDict shape lives in ``coordinare.session``.
    persona_scope: dict[str, Any] | None = None
    # 075: per-HEAD CI-gate bounce counter (schema v5+).  Keyed by head SHA;
    # MUST round-trip through session ↔ disk or escalation logic forgets the
    # bounce count across daemon restarts.  Optional / default ``{}`` keeps
    # v1-v4 snapshots loading unchanged.
    bounce_counter: dict[str, int] = Field(default_factory=dict)
    # 089: per-HEAD implementer local-test self-fix counter (schema v8+).
    # Parallel to bounce_counter — keyed by head SHA, never reads/writes it
    # (SC-004).  Optional / default ``{}`` keeps v1-v7 snapshots loading.
    local_fix_counter: dict[str, int] = Field(default_factory=dict)
    # 090: per-HEAD INHERITED-failure repair counter (schema v9+).  Parallel to
    # bounce_counter / local_fix_counter — keyed by head SHA, never reads/writes
    # either.  Empty/absent means zero attempts taken for any head (NOT
    # unlimited): the configured ``max_repair_attempts_per_head`` budget still
    # applies (FR-026, SC-009).  Optional / default ``{}`` keeps v1-v8 snapshots
    # loading unchanged.
    inheritance_repair_counter: dict[str, int] = Field(default_factory=dict)
    # 090 (schema v9+): immutable L3 repair-decision audit trail (FR-023).  Each
    # dispatch/guard/outcome appends a RepairDecisionRecord so the full repair
    # sequence for any head survives daemon restarts.  Optional / default ``[]``
    # keeps v1-v8 snapshots loading unchanged.
    repair_audit: list[RepairDecisionRecord] = Field(default_factory=list)
    # 075 fix (schema v6+): signature of the last CI-gate rollup comment posted
    # so notify.py dedup survives daemon restarts.  None = not yet posted.
    ci_gate_rollup_signature: str | None = None
    # 076 (schema v7+): dispatcher-dedup + lifecycle-correctness state.  All
    # five fields default to safe empty values so v1-v6 snapshots load
    # unchanged.  See ``specs/076-qa-cycle/data-model.md`` §8 for semantics.
    #
    # Per-(card_id, performer_stage) idle-timeout retry counter (FR-019);
    # keyed by ``f"{card_id}:{performer_stage}"``.  Serialised as plain dicts
    # for JSON portability; the typed model lives in
    # ``coordinare.services.dispatcher_dedup_models.IdleTimeoutRetryRecord``.
    last_progress_at: datetime | None = None
    last_progress_fingerprint: str | None = None
    # 343: the workflow step this card is in and when it entered it. Persisted
    # so the answer survives a daemon restart and outlives the performer job,
    # which is exactly when "where did it get to?" is asked.
    workflow_step: str | None = None
    workflow_step_entered_at: datetime | None = None
    workflow_step_trail: list[dict[str, Any]] = Field(default_factory=list)
    idle_timeout_retries: dict[str, dict[str, Any]] = Field(default_factory=dict)
    # Audit timestamp set by ``monitor_performer._record_pr_artefacts``
    # whenever a successful turn's PR fields are written through to state
    # (FR-016).  None means the session has not yet had a successful turn
    # that produced GitHub artefacts.
    pr_artefacts_recorded_at: datetime | None = None
    # Surfaced when FR-024 multi-PR detection finds >1 open PR for this card.
    # Plain dict for JSON portability; typed model is
    # ``dispatcher_dedup_models.MultiPRDivergence``.  None = no divergence.
    multi_pr_divergence: dict[str, Any] | None = None
    # Per-card rolling list of wedge-detection timestamps (FR-020 promotion
    # threshold).  Trimmed to the configured ``wedge_block_window_hours``
    # window on every write.  Empty list = no recent wedges.
    # Per-card rolling list of wedge-detection timestamps, keyed by
    # ``card_id`` (data-model §8).  Per-card so card A's wedges don't
    # trip card B's BLOCKED promotion threshold.
    wedge_count_window: dict[str, list[datetime]] = Field(default_factory=dict)

    @field_validator("wedge_count_window", mode="before")
    @classmethod
    def _coerce_legacy_flat_wedge_window(cls, v: object) -> object:
        """Backward compatibility (076 dev-build snapshots).

        An earlier iteration of 076 stored ``wedge_count_window`` as a
        flat ``list[datetime]`` rather than the spec-final
        ``dict[str, list[datetime]]``.  If we encounter such a snapshot,
        reset to an empty dict — the wedge history is lost but the
        daemon can load and proceed.  Without this validator,
        Pydantic raises ValidationError on load and the daemon cannot
        start.
        """
        if isinstance(v, list):
            return {}
        return v
    # Per-card reconciliation decision audit trail from the most recent
    # daemon startup (data-model §8).  Populated by
    # ``run_startup_reconciliation``; cleared at the end of the first poll
    # cycle so it doesn't suppress mid-run notifications (notification-dedup
    # contract).  Values match ``ReconciliationDecision`` enum strings.
    reconciliation_decisions_last_startup: dict[str, str] = Field(default_factory=dict)
    # 095: per-card ENV_BLOCKED hold + notification-dedup state.  None when the
    # card is not currently infra-blocked; otherwise the dict carries only
    # ``head_sha``, ``pattern_id``, ``cause`` and ``action`` — never secret
    # values.  Round-tripped so a still-active block does not re-notify the
    # operator after a daemon restart (notification-dedup contract, FR-006).
    env_blocked: dict[str, Any] | None = None
    # 096: per-card anti-thrash marker for the auto-rebase trigger.  Records the
    # most recent rebase attempt for this card as ``{main_sha, head_sha,
    # outcome}`` so a BLOCKED/FAILED conflict is not re-attempted every cycle
    # until the branch head or target main changes.  None = never attempted.
    # SHAs + an outcome enum string only — never secret values.
    last_rebase_attempt: dict[str, Any] | None = None
    # 123 (schema v12+): split bounce budget.  ``content_feedback_cycles`` counts
    # content-driven feedback rounds (reviewer/QA ``changes_requested``) and is
    # checked against ``config.max_feedback_cycles`` (default 5).
    # ``transient_error_cycles`` counts infra/transient failures
    # (env_blocked/system_error/unknown) and is checked against a separate limit
    # (3).  Splitting them stops a card that hit repeated infra failures from
    # being falsely escalated on content grounds and vice-versa.  Optional /
    # default ``0`` keeps v1-v11 snapshots loading unchanged; a legacy
    # ``feedback_cycle_count`` is migrated into ``content_feedback_cycles`` (see
    # ``_migrate_legacy_feedback_cycle_count``).
    content_feedback_cycles: int = 0
    transient_error_cycles: int = 0
    # 123 (schema v12+): answered assessor Q&A carried across bounce cycles.
    # Each entry is ``{"question": str, "answer": str}`` extracted from a
    # successful assessor performer result; injected as ``prior_clarifications``
    # on assessor re-dispatch so the assessor doesn't re-ask answered questions.
    # Deliberately DISTINCT from ``open_questions: list[str]`` above (the
    # blocked-card diagnostic surface).  Optional / default ``[]`` keeps v1-v11
    # snapshots loading unchanged.
    assessor_open_questions: list[dict] = Field(default_factory=list)
    # 125 (schema v13+): stage-verdict memory.  One StageVerdict slot per
    # verdict stage (reviewing/security/qa/documenting/closing_review) holding
    # the PR head SHA the stage's passing verdict was issued against.  Dispatch
    # skips a stage whose slot matches the LIVE remote head exactly (fail-open
    # on any mismatch/uncertainty).  Never holds implementing/assessing
    # entries.  Optional / default ``{}`` keeps v1-v12 snapshots loading
    # unchanged.  A malformed slot is dropped on load (bad slot == no slot ==
    # dispatch), never a crashed daemon — see the field validator below.
    stage_verdicts: dict[str, StageVerdict] = Field(default_factory=dict)
    # 125 (schema v13+): per-card issue-comment classification dedup, persisted
    # so a restart does not re-classify already-processed comments (US4).  The
    # comment router (route_issue_comments) is per-card — it reads the ACTIVE
    # card's linked issue — and both keys already round-trip session ↔ state
    # via _SESSION_FIELDS, so persistence lives here (per-card), NOT top-level.
    # The processed-ID list is bounded at save time (numerically largest 2000 —
    # GitHub comment IDs are monotonic, so largest == newest); restored as a
    # set into the session.  Numeric IDs only — never comment bodies.
    processed_issue_comment_ids: list[int] = Field(default_factory=list)
    pipeline_admitted: bool = False
    last_issue_comment_id: int | None = None
    # 126 (schema v15+): terminal-success-floor state.  feedback_ledger holds
    # the per-item feedback contract (stamped at bounce, disposed by the
    # implementer completion, adjudicated by the raiser); feedback_origin_sha
    # is the head the CURRENT round was raised against (the implementer floor
    # compares the completion's settled head against it); noop_success_retries
    # bounds the strengthened re-dispatch on a no-op "done" (one retry, then
    # operator hold).  Defaults keep v1-v13 snapshots loading unchanged.
    feedback_ledger: list[FeedbackItemRecord] = Field(default_factory=list)
    feedback_origin_sha: str | None = None
    noop_success_retries: int = 0
    # 141 (schema v21+): attempt telemetry.  last_attempt_id is the UUID
    # returned by AttemptLog.open_attempt at dispatch time; written back to
    # PersistedSession immediately so it survives a daemon restart between
    # open_attempt and close_attempt (FR-008 / A-008).  last_attempt_log_path
    # is the JSONL file path snapshotted at open time — on recovery,
    # AttemptLog.reopen_attempt uses it so the end row lands in the same file
    # as the start row even after a midnight rollover + restart.  Optional /
    # default None keeps v1-v20 snapshots loading unchanged.
    last_attempt_id: str | None = None
    last_attempt_log_path: str | None = None
    last_attempt_failure_source: str | None = None
    # 165 (schema v17+): the architect blueprint and the documenter side run.
    # blueprint is the validated Blueprint (data-model.md) plus size,
    # blueprint_hash and created_at; briefs are projected from it at dispatch
    # and never stored. Defaults keep v1-v16 snapshots loading unchanged.
    documentation_findings: dict[str, Any] = Field(default_factory=dict)

    @field_validator("documentation_findings", mode="before")
    @classmethod
    def _clean_documentation_findings(cls, value: Any) -> dict[str, Any]:
        from coordinare.services.documentation_findings import clean_findings
        return clean_findings(value)

    blueprint: dict[str, Any] | None = None
    documenting_side: DocumentingSideRun | None = None
    # 166 (schema v18+): the assessor's structured assessment for one card.
    # Contains goal, expected_behavior, out_of_scope, questions, assumptions,
    # criteria with their source, carried clarifications, and assessment_hash.
    # Defaults keep v1-v17 snapshots loading unchanged. A malformed record
    # (missing goal or ready field, or not a dict) loads as None, never as
    # a snapshot load failure.
    assessment: dict[str, Any] | None = None
    # 169 (schema v19+): the reviewer's structured findings for one card.
    # Contains changed_files with hunks, diff_truncated flag, survey commands/
    # refusals, findings (before gate, dropped, after recheck), dispositions,
    # coverage pass state, verdict, covered files, post result and URL, and
    # workflow metrics. Defaults keep v1-v18 snapshots loading unchanged.
    # A malformed record (not a dict, missing changed_files or verdict) loads
    # as None, never as a snapshot load failure. Cleared when reviewer is
    # dispatched; injected into implementing stage only. Findings and verdict
    # text only; never secret values.
    review_findings: dict[str, Any] | None = None

    @field_validator("documenting_side", mode="before")
    @classmethod
    def _drop_corrupt_documenting_side(cls, v: object) -> object:
        """165: a malformed side-run record loads as None (no run recorded ==
        the side run may be dispatched again for the current blueprint hash),
        never as a snapshot load failure."""
        if v is None or isinstance(v, DocumentingSideRun):
            return v
        if not isinstance(v, dict) or not v.get("blueprint_hash"):
            return None
        return v

    @field_validator("assessment", mode="before")
    @classmethod
    def _drop_corrupt_assessment(cls, v: object) -> object:
        """166: a malformed assessment record loads as None (no assessment
        recorded == the assessor may be dispatched again), never as a snapshot
        load failure. Required fields: goal (str) and ready (bool)."""
        if v is None:
            return v
        if not isinstance(v, dict):
            return None
        if not v.get("goal") or "ready" not in v:
            return None
        return v

    @field_validator("review_findings", mode="before")
    @classmethod
    def _drop_corrupt_review_findings(cls, v: object) -> object:
        """169: a malformed review_findings record loads as None (no findings
        recorded == the reviewer may be dispatched again), never as a snapshot
        load failure. Required fields: changed_files (list) and verdict (str)."""
        if v is None:
            return v
        if not isinstance(v, dict):
            return None
        if not isinstance(v.get("changed_files"), list) or not isinstance(
            v.get("verdict"), str
        ):
            return None
        return v

    @field_validator("feedback_ledger", mode="before")
    @classmethod
    def _drop_corrupt_feedback_items(cls, v: object) -> object:
        """126: tolerate corrupted ledger entries — drop them instead of
        failing the whole snapshot load (a dropped item means the floor simply
        has less to enforce: the safe direction)."""
        if not isinstance(v, list):
            return []
        cleaned: list[Any] = []
        for entry in v:
            if isinstance(entry, FeedbackItemRecord):
                cleaned.append(entry)
                continue
            if not isinstance(entry, dict):
                continue
            try:
                cleaned.append(FeedbackItemRecord(**entry))
            except (ValidationError, TypeError):
                continue
        return cleaned

    @field_validator("stage_verdicts", mode="before")
    @classmethod
    def _drop_corrupt_stage_verdicts(cls, v: object) -> object:
        """125: tolerate corrupted slots — drop them instead of failing the
        whole snapshot load.  A dropped slot simply means the stage dispatches
        (the safe direction)."""
        if not isinstance(v, dict):
            return {}
        cleaned: dict[str, Any] = {}
        for stage, entry in v.items():
            if isinstance(entry, StageVerdict):
                cleaned[str(stage)] = entry
                continue
            if not isinstance(entry, dict):
                logger.warning(
                    "state_store.stage_verdict_dropped",
                    stage=str(stage),
                    reason="not_a_dict",
                )
                continue
            try:
                cleaned[str(stage)] = StageVerdict(**entry)
            except (ValidationError, TypeError) as exc:
                # Surface the drop so snapshot corruption is visible rather than
                # silently swallowed (the stage just re-dispatches — safe — but
                # an operator should see that a recorded verdict was lost).
                logger.warning(
                    "state_store.stage_verdict_dropped",
                    stage=str(stage),
                    reason=type(exc).__name__,
                )
                continue
        return cleaned

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_feedback_cycle_count(cls, data: object) -> object:
        """123 (schema v12): migrate legacy ``feedback_cycle_count`` on load.

        Pre-123 flat state tracked a single ``feedback_cycle_count`` counter that
        conflated content feedback and infra failures.  It was never a persisted
        ``PersistedSession`` field, but a snapshot written by a transitional
        build (or a hand-authored fixture) may still carry it.  When
        ``content_feedback_cycles`` is absent or 0 and a positive legacy
        ``feedback_cycle_count`` is present, seed the new content counter from it
        so an in-flight card's content budget is preserved across the upgrade.
        """
        if not isinstance(data, dict):
            return data
        legacy = data.get("feedback_cycle_count")
        current = data.get("content_feedback_cycles")
        if (current is None or current == 0) and isinstance(legacy, int) and not isinstance(legacy, bool) and legacy > 0:
            data = dict(data)
            data["content_feedback_cycles"] = legacy
        return data


class EnvCacheStateSnapshot(BaseModel):
    """Durable subset of EnvCacheState (Fix 3 / 073).

    Persists the content-hash fingerprint and last-bootstrap outcome so a
    coordinare restart with unchanged env-spec files does not re-trigger the
    expensive env_bootstrap performer job (which can take 10+ minutes and
    holds the serialize_env_bootstrap gate).

    Transient fields are deliberately omitted so a crash mid-bootstrap does
    not leave a permanently-stuck flag on disk:
      - bootstrap_in_flight: rederived (always False after restart)
      - pending_sha: rederived from the next SHA fetch
      - runtime_health_failed: rederived on next performer report
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    symphony_name: str
    sanitised_name: str
    cache_dir: str  # Path serialised as str for JSON portability
    readme_sha: str | None = None
    last_bootstrap_at: datetime | None = None
    last_bootstrap_succeeded: bool | None = None
    # 077: persist the failure reason so the dashboard shows it after a restart
    # and the feedback-injection retry survives a restart.
    last_bootstrap_error: str | None = None
    cache_dir_ready: bool = False
    # 088 (FR-009): bootstrap circuit-breaker budget. Persisted so a restart
    # does not reset a failing bootstrap's attempt count; old snapshots load
    # with the defaults (no migration).
    bootstrap_attempts: int = 0
    bootstrap_exhausted: bool = False
    # 092 (FR-016/FR-017): agent-discovered test-env file PATH (e.g.
    # ".coordinare/test.env"), persisted only when no symphony-level test_env
    # config block is set. PATH ONLY — never the loaded KEY=VALUE pairs. A reused
    # cache reloads variables from this source file at runtime, so no literal
    # secret is ever written to disk. Old snapshots load with None (no migration).
    test_env_source: str | None = None
    # 124 (US2, schema v13): docs/wiki wiki-init durable state. Old snapshots load
    # with the defaults (no migration). wiki_in_flight is transient (not here).
    wiki_initialized: bool = False
    wiki_attempts: int = 0
    wiki_exhausted: bool = False
    last_wiki_init_at: datetime | None = None
    last_wiki_init_succeeded: bool | None = None
    last_wiki_init_error: str | None = None
    # 173 (schema v20): the two card-less intake roles.  Old snapshots load with
    # these defaults (no migration).  ``*_in_flight`` is transient (not here).
    advocate_attempts: int = 0
    advocate_exhausted: bool = False
    last_advocate_run_at: datetime | None = None
    last_advocate_succeeded: bool | None = None
    last_advocate_error: str | None = None
    last_advocate_issues_seen: int = 0
    curator_attempts: int = 0
    curator_exhausted: bool = False
    last_curator_run_at: datetime | None = None
    last_curator_succeeded: bool | None = None
    last_curator_error: str | None = None
    last_curator_issues_seen: int = 0


class WorkflowSnapshot(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    schema_version: int = Field(default=CURRENT_SCHEMA_VERSION)
    snapshot_at: datetime
    phase: WorkflowPhase

    active_card_id: str | None = None
    active_card_title: str | None = None
    active_card_column: str | None = None

    pr_url: str | None = None
    pr_node_id: str | None = None

    agent_session_id: str | None = None

    open_questions: list[str] = Field(default_factory=list)
    card_clarifications: list[dict] = Field(default_factory=list)
    active_card_issue_id: str | None = None
    # 045: Persist the rest of the card fields so restore-from-snapshot doesn't
    # dispatch with ``issue_number=0`` / empty description after a restart —
    # that was producing PRs with null bodies (no ``Closes #N`` linkage).
    # ``ge=1`` matches the JSON-schema contract (``minimum: 1``) so corrupted
    # or pre-045 snapshots with ``issue_number: 0`` fail validation loudly
    # rather than round-tripping and re-introducing the null-body bug.
    active_card_issue_number: int | None = Field(default=None, ge=1)
    active_card_issue_url: str | None = None
    active_card_description: str | None = None
    active_card_acceptance_criteria: list[str] = Field(default_factory=list)
    # 053: Preserve lifecycle position across daemon restarts so an in-flight
    # card does not restart from stage 1 (e.g., assessing) every time.
    performer_stage: str | None = None
    lifecycle_sequence: list[str] = Field(default_factory=list)
    last_blocked_notified_at: datetime | None = None
    lifecycle_completed_at: datetime | None = None
    processed_review_ids: list[str] = Field(default_factory=list)  # stored as list, used as set
    # 128: stale-review surfacing dedup marker for the active card (mirrors the
    # per-card PersistedSession field). v<16 snapshots load with an empty dict.
    surfaced_stale_reviews: dict[str, str] = Field(default_factory=dict)
    # 065 Fix 7b: multi-card session persistence.  Without this, a restart in
    # multi-card mode loses every per-card stage and re-adopts each IN_PROGRESS
    # card as a fresh "implementing" session, demoting closer cards back to the
    # implementer.  v1 snapshots simply load with an empty dict.
    active_sessions: dict[str, PersistedSession] = Field(default_factory=dict)
    # 073 Fix 3: per-symphony env-cache fingerprint persistence.  Without this,
    # every coordinare restart re-runs env_bootstrap because EnvCacheService
    # initialises readme_sha=None for every symphony, then check_and_trigger
    # sees the SHA "change" and dispatches a fresh bootstrap.  Persisting the
    # readme_sha (plus the last-success bookkeeping) lets the next cycle skip
    # bootstrap when the env-spec files on the symphony are unchanged.  v1/v2
    # snapshots load with an empty dict and rely on the first cycle's SHA
    # fetch to repopulate.
    env_cache: dict[str, EnvCacheStateSnapshot] = Field(default_factory=dict)
    # 096: the main-branch SHA the rebase trigger last reconciled against.
    # Persisted (run-global, not per-card) so a main advance that happened while
    # the daemon was down is seen as drift on the first post-restart cycle instead
    # of being silently adopted as the new baseline.  None = unknown (pre-096
    # snapshots, or a fresh run).  A git SHA only — never a secret value.
    last_known_main_sha: str | None = None


class StateLoadError(ValueError):
    """Raised by StateStore.load() when a state file exists but cannot be safely used."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"state load failed [{reason}]: {detail}")


class StateStore:
    """Atomic JSON file store for WorkflowSnapshot persistence."""

    def __init__(self, path: Path, metrics: CoordinareMetrics) -> None:
        self._path = path
        self._metrics = metrics
        self.last_snapshot: WorkflowSnapshot | None = None

    def verify_writable(self) -> None:
        """Probe write to the state file's parent directory. Raises OSError on failure."""
        parent = self._path.parent
        parent.mkdir(parents=True, exist_ok=True)
        probe = parent / ".coordinare_write_probe"
        try:
            probe.write_bytes(b"")
        finally:
            probe.unlink(missing_ok=True)

    async def save(self, snapshot: WorkflowSnapshot) -> None:
        """Atomically write snapshot to disk via temp-file + os.replace().

        On OSError (e.g. disk full): increments failure counter, logs warning, does not raise.
        """
        start = time.monotonic()
        tmp_path: str | None = None
        try:
            data = snapshot.model_dump_json(indent=2).encode("utf-8")
            fd = tempfile.NamedTemporaryFile(  # noqa: SIM115
                dir=self._path.parent, delete=False, suffix=".tmp"
            )
            tmp_path = fd.name
            try:
                fd.write(data)
                fd.flush()
                os.fsync(fd.fileno())
            finally:
                fd.close()
            os.replace(tmp_path, self._path)
            tmp_path = None  # rename succeeded, no cleanup needed

            elapsed = time.monotonic() - start
            self._metrics.state_write_duration_seconds.observe(elapsed)
            self._metrics.state_last_written_timestamp.set(
                snapshot.snapshot_at.timestamp()
            )
            self.last_snapshot = snapshot
        except OSError as exc:
            self._metrics.state_write_failures_total.inc()
            logger.warning(
                "state_write_failed",
                error=str(exc),
                path=str(self._path),
                msg="running without persistence",
            )
        finally:
            if tmp_path is not None:
                Path(tmp_path).unlink(missing_ok=True)

    async def load(self) -> WorkflowSnapshot | None:
        """Read and validate the state file.

        Returns None if file does not exist.
        Raises StateLoadError on corrupt, schema mismatch, or validation errors.
        """
        if not self._path.exists():
            return None

        try:
            raw = self._path.read_bytes()
        except OSError as exc:
            raise StateLoadError(reason="corrupt", detail=str(exc)) from exc

        try:
            snapshot = WorkflowSnapshot.model_validate_json(raw)
        except Exception as exc:
            raise StateLoadError(
                reason="corrupt", detail=str(exc)
            ) from exc

        if (
            snapshot.schema_version < MIN_SUPPORTED_SCHEMA_VERSION
            or snapshot.schema_version > CURRENT_SCHEMA_VERSION
        ):
            raise StateLoadError(
                reason="schema_mismatch",
                detail=(
                    f"supported [{MIN_SUPPORTED_SCHEMA_VERSION}..{CURRENT_SCHEMA_VERSION}], "
                    f"got v{snapshot.schema_version}. "
                    f"If downgrading coordinare, restore a snapshot from a "
                    f"compatible version or upgrade back to the version "
                    f"that wrote this snapshot."
                ),
            )

        self.last_snapshot = snapshot
        return snapshot

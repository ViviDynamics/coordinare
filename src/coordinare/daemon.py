from __future__ import annotations

import asyncio
import contextlib
import copy
import signal
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from time import monotonic, perf_counter
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import uuid4

import structlog
from pydantic import ValidationError

from coordinare.graph.nodes.check_board import NON_SLOT_PHASES, PASSIVE_PHASES, check_board
from coordinare.graph.nodes.github_retry import (
    clear_deferred_github_operation,
    defer_github_operation,
    github_operation_ready,
    is_transient_github_outage_error,
)
from coordinare.graph.state import (
    CoordinareState,
    DaemonPhase,
    _rederive_current_card,
    _retire_active_session,
    _set_current_card,
    initial_state,
)
from coordinare.lib.runtime_events import build_runtime_event
from coordinare.metrics import METRICS
from coordinare.models.dependency import DependencyStatus
from coordinare.observability import HEALTH, HealthStatus, bind_cycle_id, clear_cycle_id
from coordinare.resilience import CircuitOpenError
from coordinare.services.board_provider import (
    GitHubProjectsBoardProvider,
    board_of,
)
from coordinare.services.closed_pr import is_closed_pr_block
from coordinare.services.dependency import build_graph as _build_dep_graph
from coordinare.services.owned_writers import has_owned_writers, stop_owned_writers
from coordinare.services.pipeline_budget import has_live_side_writer
from coordinare.services.rebase import fetch_main_sha, repo_url_from_config, run_rebase_round
from coordinare.services.reconciliation import (
    reconcile_board_state,
    recover_documenting_side_session,
)
from coordinare.session import _SESSION_FIELDS, CardSession, session_to_state, state_to_session
from coordinare.state_store import (
    DocumentingSideRun,
    EnvCacheStateSnapshot,
    FeedbackItemRecord,
    PersistedSession,
    RepairDecisionRecord,
    StageVerdict,
    StateLoadError,
    StateStore,
    WorkflowPhase,
    WorkflowSnapshot,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from coordinare.dashboard import DashboardStore
    from coordinare.models.dependency import DependencyGraph
    from coordinare.models.env_cache import BootstrapJobPayload
    from coordinare.services.intake_dispatch import IntakeRole

logger = structlog.get_logger(__name__)

# 076 — Daemon-process identity captured once at module-init for use as the
# `coordinare.daemon_started_at` Docker label on every performer container the
# daemon launches.  The reconciliation pass uses this on subsequent restarts
# to distinguish "containers I launched" from "containers from a prior daemon
# run that should be reaped."
# The value must satisfy the K8s label-value rules (≤63 chars of
# [A-Za-z0-9_.-], starting and ending alphanumeric) because the same labels
# ride Kubernetes Pod manifests: an ISO8601 isoformat() string carries ':' and
# '+' and the K8s API rejects such Pods with a 422. Docker accepts both
# spellings, so one label-safe timestamp keeps the two runtimes identical.
_DAEMON_STARTED_AT: str = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def get_daemon_started_at() -> str:
    """Return the daemon's start time as a K8s-label-safe UTC timestamp
    (``YYYYMMDDTHHMMSSZ``, spec 076 FR-009)."""
    return _DAEMON_STARTED_AT


#: Eligibility verdicts. Named because two of them are compared against string
#: literals at call sites: review noted that a bare "blocked_column" in the
#: fanout and in _compute_eligibility are two spellings that must agree, with
#: nothing to catch a typo -- the branch would simply never match, and the
#: blocked-card recovery this exists for would silently stop happening.
#: Typed, so a new verdict has to be added here rather than appearing as a
#: string nothing matches. The comment this replaces listed four and the code
#: already had five.
EligibilityReason = Literal[
    "eligible", "blocked_column", "backlog_column", "dependency_blocked", "missing_card", "kicked_back", "pipeline_capacity",
]

ELIGIBLE: EligibilityReason = "eligible"
BLOCKED_COLUMN: EligibilityReason = "blocked_column"
BACKLOG_COLUMN: EligibilityReason = "backlog_column"
DEPENDENCY_BLOCKED: EligibilityReason = "dependency_blocked"
MISSING_CARD: EligibilityReason = "missing_card"
KICKED_BACK: EligibilityReason = "kicked_back"

#: How many BLOCKED sessions may run their graph in one cycle. Each one is a
#: full graph tick with a state deep-copy and a fanout permit, and the whole
#: point of polling them is to notice an un-block comment -- which is not
#: latency-sensitive. Without a cap the per-cycle cost grows with the blocked
#: backlog, which is exactly the board this feature exists to recover, so N
#: blocked cards would crowd out the eligible siblings they run alongside.
#: Rotating the window means every blocked card is still polled within
#: ceil(N / BLOCKED_POLL_MAX_PER_CYCLE) cycles.
BLOCKED_POLL_MAX_PER_CYCLE = 3


@dataclass
class SessionEligibility:
    """Per-cycle eligibility verdict for one active session."""

    card_id: str
    eligible: bool
    reason: EligibilityReason
    blockers: list[int] = field(default_factory=list)


@dataclass
class AsyncSessionTickResult:
    """Result of one concurrent session invocation."""

    card_id: str
    ok: bool
    session_state: dict[str, Any]
    skipped: bool = False
    error: str | None = None
    duration_ms: int = 0
    global_updates: dict[str, Any] | None = None


_PHASE_PRIORITY: dict[str, int] = {
    "system_error": 9,
    "blocked": 8,
    "recovery": 7,
    "merging": 6,
    "relay_feedback": 5,
    "dispatching": 4,
    "monitoring_performer": 3,
    "monitoring_pr": 2,
    "monitoring_agent": 1,
    "idle": 0,
}


def _coerce_counter(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    try:
        return max(0, int(value))
    except (ValueError, OverflowError):
        return 0


def _validated_counter_map(raw: Any) -> dict[str, int]:
    """Validate a per-key int counter map (reject bools/non-finite/overflow)."""
    counters: dict[str, int] = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            # Reject bools (isinstance(True, int) is True) and non-finite
            # floats so a corrupted snapshot can't crash startup on int().
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                continue
            try:
                counters[str(k)] = int(v)
            except (ValueError, OverflowError):
                continue
    return counters


def _validated_nonneg_counter_map(raw: Any) -> dict[str, int]:
    """Per-key int counter map that additionally requires values >= 0 (390)."""
    counters: dict[str, int] = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            # 390: same defensive read as bounce_counter below — a corrupted
            # snapshot must not crash startup, and an unreadable entry is
            # treated as budget unspent (costs one dispatch, not the daemon).
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                continue
            try:
                parsed = int(v)
            except (ValueError, OverflowError):
                continue
            if parsed >= 0:
                counters[str(k)] = parsed
    return counters


def _persist_repair_audit(raw: Any) -> list[RepairDecisionRecord]:
    """090-L3: validate each repair-audit entry against RepairDecisionRecord
    and drop any that fail. A corrupt entry is skipped, never crash-on-load:
    PersistedSession's own coercion raises on a malformed list item rather
    than dropping it, which would fail the whole snapshot load.
    """
    repair_audit: list[RepairDecisionRecord] = []
    if isinstance(raw, (list, tuple)):
        for r in raw:
            if not isinstance(r, dict):
                continue
            try:
                repair_audit.append(RepairDecisionRecord(**r))
            except (ValidationError, TypeError):
                continue
    return repair_audit


def _persist_env_blocked(raw: Any) -> dict[str, Any] | None:
    """095: per-card ENV_BLOCKED hold/dedup state.  Persist a dict of
    identifiers and diagnostic strings (head_sha/pattern_id/cause/action) so a
    still-active block does not re-notify the operator after a restart
    (FR-006).  The dedup check keys on head_sha + pattern_id, so a dict
    missing or corrupting EITHER would silently break dedup; degrade the
    WHOLE thing to None (re-notify once) unless both are non-empty strings.
    cause/action are best-effort strings; 263 also carries retry history.
    """
    env_blocked: dict[str, Any] | None = None
    if isinstance(raw, dict):
        hs = raw.get("head_sha")
        pid = raw.get("pattern_id")
        if isinstance(hs, str) and hs and isinstance(pid, str) and pid:
            env_blocked = {"head_sha": hs, "pattern_id": pid}
            for k in ("cause", "action", "blocked_at"):
                v = raw.get(k)
                if isinstance(v, str):
                    env_blocked[k] = v
            # 263: retain the outage identity and attempted retries across
            # restarts; dropping them allows a second retry on the same head.
            for k in ("check_names", "retried_checks"):
                values = raw.get(k)
                if isinstance(values, list):
                    env_blocked[k] = [v for v in values if isinstance(v, str) and v]
            jobs = raw.get("retried_jobs")
            if isinstance(jobs, list):
                env_blocked["retried_jobs"] = [v for v in jobs if type(v) is int and v > 0]
    return env_blocked


def _persist_last_rebase_attempt(raw: Any) -> dict[str, Any] | None:
    """096: per-card auto-rebase anti-thrash marker. Persist only when all
    three keys are non-empty strings; anything malformed degrades to None
    (re-attempt allowed) so a corrupt marker never wedges a card.
    """
    last_rebase_attempt: dict[str, Any] | None = None
    if isinstance(raw, dict):
        _m, _h, _o = raw.get("main_sha"), raw.get("head_sha"), raw.get("outcome")
        if all(isinstance(x, str) and x for x in (_m, _h, _o)):
            last_rebase_attempt = {"main_sha": _m, "head_sha": _h, "outcome": _o}
    return last_rebase_attempt


def _persist_assessor_questions(raw: Any) -> list[dict[str, Any]]:
    """123: answered assessor Q&A — normalize to well-formed {"question","answer"}
    dicts so a malformed entry never fails the whole snapshot load AND never
    reaches dispatch_performer's prior_clarifications injection missing a key.
    """
    assessor_open_questions: list[dict[str, Any]] = []
    if isinstance(raw, (list, tuple)):
        for item in raw:
            if isinstance(item, dict) and item.get("question"):
                assessor_open_questions.append(
                    {
                        "question": str(item.get("question", "")),
                        "answer": str(item.get("answer", "")),
                    },
                )
    return assessor_open_questions


def _persist_stage_verdicts(raw: Any) -> dict[str, StageVerdict]:
    """125: stage-verdict memory — validate each slot via StageVerdict and
    drop malformed ones here (a bad slot == no slot == dispatch), so a
    corrupt entry never fails the whole snapshot save/load.
    """
    stage_verdicts: dict[str, StageVerdict] = {}
    if isinstance(raw, dict):
        for sv_stage, sv_entry in raw.items():
            if not isinstance(sv_entry, dict):
                continue
            try:
                stage_verdicts[str(sv_stage)] = StageVerdict(**sv_entry)
            except (ValidationError, TypeError):
                continue
    return stage_verdicts


def _persist_comment_ids(raw: Any) -> list[int]:
    """125: per-card issue-comment dedup watermark.  Bound the processed-ID
    list to the numerically largest 2000 (GitHub comment IDs are
    monotonic → largest == newest); reject bools/non-ints defensively.
    """
    comment_ids: list[int] = []
    if isinstance(raw, (set, list, tuple)):
        for cid_val in raw:
            # GitHub comment IDs are integers. Accept only int (never
            # bool, never float): int(42.9) would silently coerce to an
            # unrelated id (42) and corrupt the dedup watermark. Matches
            # the last_issue_comment_id handling in the scalar fields.
            if isinstance(cid_val, bool) or not isinstance(cid_val, int):
                continue
            comment_ids.append(cid_val)
    return sorted(set(comment_ids))[-2000:]


def _persist_feedback_ledger(raw: Any) -> list[FeedbackItemRecord]:
    """126: terminal-success-floor state — validate ledger entries via
    FeedbackItemRecord and drop malformed ones (bad entry == no entry ==
    the floor has less to enforce, the safe direction).
    """
    feedback_ledger: list[FeedbackItemRecord] = []
    if isinstance(raw, (list, tuple)):
        for fb_entry in raw:
            if not isinstance(fb_entry, dict):
                continue
            try:
                feedback_ledger.append(FeedbackItemRecord(**fb_entry))
            except (ValidationError, TypeError):
                continue
    return feedback_ledger


def _persist_documenting_side(raw: Any) -> DocumentingSideRun | None:
    """165: the side-run record is validated here so a corrupt entry drops to
    None instead of failing the snapshot save."""
    documenting_side: DocumentingSideRun | None = None
    if isinstance(raw, DocumentingSideRun):
        documenting_side = raw
    elif isinstance(raw, dict) and raw.get("blueprint_hash"):
        try:
            documenting_side = DocumentingSideRun(**raw)
        except (ValidationError, TypeError):
            documenting_side = None
    return documenting_side


def _persist_lifecycle_fields(sess: dict[str, Any]) -> dict[str, Any]:
    """Validate the lifecycle-stamp session fields for snapshot persistence."""
    completed_raw = sess.get("lifecycle_completed_at")
    completed = completed_raw if isinstance(completed_raw, datetime) else None
    processed_ids_raw = sess.get("processed_review_ids") or ()
    processed_ids: list[str]
    if isinstance(processed_ids_raw, (set, list, tuple)):
        processed_ids = sorted({str(r) for r in processed_ids_raw})
    else:
        processed_ids = []
    # 128: per-card stale-review dedup marker
    ssr_raw = sess.get("surfaced_stale_reviews") or {}
    surfaced_stale = (
        {str(k): str(v) for k, v in ssr_raw.items()} if isinstance(ssr_raw, dict) else {}
    )
    questions_raw = sess.get("open_questions") or ()
    clarifications_raw = sess.get("card_clarifications") or ()
    relay_raw = sess.get("relay_feedback") or ()
    last_notified_raw = sess.get("last_blocked_notified_at")
    last_notified = last_notified_raw if isinstance(last_notified_raw, datetime) else None
    last_slack_raw = sess.get("last_blocked_slack_delivered_at")
    last_slack = last_slack_raw if isinstance(last_slack_raw, datetime) else None
    head_dispatch_raw = sess.get("head_at_dispatch")
    head_dispatch = (
        head_dispatch_raw if isinstance(head_dispatch_raw, str) and head_dispatch_raw else None
    )
    head_last_raw = sess.get("head_at_last_turn")
    head_last = head_last_raw if isinstance(head_last_raw, str) and head_last_raw else None
    persona_scope_raw = sess.get("persona_scope")
    persona_scope = persona_scope_raw if isinstance(persona_scope_raw, dict) else None
    ci_gate_rollup_sig_raw = sess.get("ci_gate_rollup_signature")
    ci_gate_rollup_sig = (
        ci_gate_rollup_sig_raw
        if isinstance(ci_gate_rollup_sig_raw, str) and ci_gate_rollup_sig_raw
        else None
    )
    return {
        "completed": completed,
        "processed_ids": processed_ids,
        "surfaced_stale": surfaced_stale,
        "questions_raw": questions_raw,
        "clarifications_raw": clarifications_raw,
        "relay_raw": relay_raw,
        "last_notified": last_notified,
        "last_slack": last_slack,
        "head_dispatch": head_dispatch,
        "head_last": head_last,
        "persona_scope": persona_scope,
        "ci_gate_rollup_sig": ci_gate_rollup_sig,
        # 123: split bounce budget counters — coerce defensively (reject
        # bools/non-int) so a corrupt snapshot can't crash startup on int().
        "content_feedback_cycles": _coerce_counter(sess.get("content_feedback_cycles")),
        "transient_error_cycles": _coerce_counter(sess.get("transient_error_cycles")),
    }


def _persist_review_fields(sess: dict[str, Any]) -> dict[str, Any]:
    """Validate the review/attempt session fields for snapshot persistence."""
    origin_raw = sess.get("feedback_origin_sha")
    feedback_origin_sha = origin_raw if isinstance(origin_raw, str) and origin_raw else None
    noop_raw = sess.get("noop_success_retries")
    noop_success_retries = (
        max(0, int(noop_raw))
        if isinstance(noop_raw, int) and not isinstance(noop_raw, bool)
        else 0
    )
    # 141: persist in-flight attempt telemetry IDs so close_attempt can
    # write to the correct JSONL file after a daemon restart (A-008).
    last_attempt_id_raw = sess.get("last_attempt_id")
    last_attempt_id_val = (
        str(last_attempt_id_raw)
        if isinstance(last_attempt_id_raw, str) and last_attempt_id_raw
        else None
    )
    last_attempt_log_path_raw = sess.get("last_attempt_log_path")
    last_attempt_log_path_val = (
        str(last_attempt_log_path_raw)
        if isinstance(last_attempt_log_path_raw, str) and last_attempt_log_path_raw
        else None
    )
    # 165: the blueprint is a plain dict (validated on the performer side);
    # the side-run record is validated in _persist_documenting_side.
    blueprint_raw = sess.get("blueprint")
    blueprint = dict(blueprint_raw) if isinstance(blueprint_raw, dict) and blueprint_raw else None
    # 166: the assessment is a plain dict (validated on the performer side);
    # a corrupt entry drops to None instead of failing the snapshot save.
    assessment_raw = sess.get("assessment")
    assessment = (
        dict(assessment_raw)
        if isinstance(assessment_raw, dict)
        and assessment_raw.get("goal")
        and "ready" in assessment_raw
        else None
    )
    # 169: the review_findings is a plain dict (validated on the performer
    # side); a corrupt entry drops to None instead of failing the snapshot save.
    review_findings_raw = sess.get("review_findings")
    review_findings = (
        dict(review_findings_raw)
        if isinstance(review_findings_raw, dict)
        and isinstance(review_findings_raw.get("changed_files"), list)
        and isinstance(review_findings_raw.get("verdict"), str)
        else None
    )
    last_comment_raw = sess.get("last_issue_comment_id")
    last_issue_comment_id: int | None = (
        int(last_comment_raw)
        if isinstance(last_comment_raw, int) and not isinstance(last_comment_raw, bool)
        else None
    )
    return {
        "feedback_origin_sha": feedback_origin_sha,
        "noop_success_retries": noop_success_retries,
        "last_attempt_id_val": last_attempt_id_val,
        "last_attempt_log_path_val": last_attempt_log_path_val,
        "blueprint": blueprint,
        "assessment": assessment,
        "review_findings": review_findings,
        "last_issue_comment_id": last_issue_comment_id,
    }


def _persist_one_session(card_id: str, sess: dict[str, Any]) -> PersistedSession:
    """Build the v2-snapshot-safe PersistedSession for one live session dict."""
    f = {**_persist_lifecycle_fields(sess), **_persist_review_fields(sess)}
    dispatch = sess.get("agent_dispatch")
    identity = dispatch if isinstance(dispatch, dict) else {}
    session_id = identity.get("session_id")
    performer_id = identity.get("performer_id")
    job_id = identity.get("job_id")
    card = sess.get("current_card") or {}
    artefacts = {
        key: card[key] for key in (
            "pr_url", "pr_node_id", "pr_number", "head_after", "pushed_branch", "plan_path",
        ) if isinstance(card, dict) and card.get(key) is not None
    }
    return PersistedSession(
        card_id=card_id,
        last_progress_at=sess.get("last_progress_at"),
        last_production_at=sess.get("last_production_at"),
        last_production_fingerprint=sess.get("last_production_fingerprint"),
        last_progress_fingerprint=sess.get("last_progress_fingerprint"),
        idle_timeout_retries=sess.get("idle_timeout_retries") or {},
        performer_stage=(sess.get("performer_stage") or None),
        phase=(sess.get("phase") or None),
        board_paused=bool(sess.get("board_paused")),
        board_pause_column=str(sess.get("board_pause_column") or ""),
        board_pause_resume_phase=str(sess.get("board_pause_resume_phase") or ""),
        reconciled_dispatch_pending=bool(sess.get("reconciled_dispatch_pending")),
        agent_session_id=session_id if isinstance(session_id, str) and session_id else None,
        agent_performer_id=performer_id if isinstance(performer_id, str) and performer_id else None,
        agent_job_id=job_id if isinstance(job_id, str) and job_id else None,
        agent_dispatch_at=(
            sess["agent_dispatch_at"]
            if isinstance(sess.get("agent_dispatch_at"), datetime)
            and sess["agent_dispatch_at"].tzinfo is not None else None
        ),
        lifecycle_completed_at=f["completed"],
        processed_review_ids=f["processed_ids"],
        surfaced_stale_reviews=f["surfaced_stale"],
        open_questions=[str(q) for q in f["questions_raw"] if q is not None]
        if isinstance(f["questions_raw"], (list, tuple, set))
        else [],
        consumed_loop_questions=[
            str(qf) for qf in (sess.get("consumed_loop_questions") or ()) if qf is not None
        ],
        card_clarifications=[dict(c) for c in f["clarifications_raw"] if isinstance(c, dict)]
        if isinstance(f["clarifications_raw"], (list, tuple))
        else [],
        dispatched_feedback=dict(sess.get("dispatched_feedback") or {}),
        pending_override=_dict_or_none(sess.get("pending_override")),
        pr_comment_tracking=dict(sess.get("pr_comment_tracking") or {}),
        pending_pr_handoff=_dict_or_none(sess.get("pending_pr_handoff")),
        pr_artefacts=artefacts,
        pr_artefacts_recorded_at=sess.get("pr_artefacts_recorded_at"),
        relay_feedback=[dict(r) for r in f["relay_raw"] if isinstance(r, dict)]
        if isinstance(f["relay_raw"], (list, tuple))
        else [],
        system_error_count=int(sess.get("system_error_count") or 0),
        system_error_reason=(sess.get("system_error_reason") or None),
        system_error_notified=bool(sess.get("system_error_notified")),
        requirements_changed=bool(sess.get("requirements_changed")),
        last_blocked_notified_at=f["last_notified"],
        last_blocked_slack_delivered_at=f["last_slack"],
        head_at_dispatch=f["head_dispatch"],
        head_at_last_turn=f["head_last"],
        persona_scope=f["persona_scope"],
        bounce_counter=_validated_counter_map(sess.get("bounce_counter")),
        no_progress_relays=_validated_nonneg_counter_map(sess.get("no_progress_relays")),
        local_fix_counter=_validated_counter_map(sess.get("local_fix_counter")),
        inheritance_repair_counter=_validated_counter_map(
            sess.get("inheritance_repair_counter"),
        ),
        repair_audit=_persist_repair_audit(sess.get("repair_audit")),
        ci_gate_rollup_signature=f["ci_gate_rollup_sig"],
        env_blocked=_persist_env_blocked(sess.get("env_blocked")),
        last_rebase_attempt=_persist_last_rebase_attempt(sess.get("last_rebase_attempt")),
        content_feedback_cycles=f["content_feedback_cycles"],
        transient_error_cycles=f["transient_error_cycles"],
        assessor_open_questions=_persist_assessor_questions(sess.get("assessor_open_questions")),
        stage_verdicts=_persist_stage_verdicts(sess.get("stage_verdicts")),
        documentation_findings=sess.get("documentation_findings") or {},
        blueprint=f["blueprint"],
        blueprint_signature=sess.get("blueprint_signature") or None,
        documenting_side=_persist_documenting_side(sess.get("documenting_side")),
        processed_issue_comment_ids=_persist_comment_ids(
            sess.get("processed_issue_comment_ids") or (),
        ),
        last_issue_comment_id=f["last_issue_comment_id"],
        pipeline_admitted=bool(sess.get("pipeline_admitted", False)),
        feedback_ledger=_persist_feedback_ledger(sess.get("feedback_ledger")),
        feedback_origin_sha=f["feedback_origin_sha"],
        noop_success_retries=f["noop_success_retries"],
        last_attempt_id=f["last_attempt_id_val"],
        last_attempt_log_path=f["last_attempt_log_path_val"],
        last_attempt_failure_source=sess.get("last_attempt_failure_source")
        if isinstance(sess.get("last_attempt_failure_source"), str)
        and sess.get("last_attempt_failure_source") in {"human", "qa_role", "grader"}
        else None,
        assessment=f["assessment"],
        review_findings=f["review_findings"],
        # 354: persist the queued-for-slot stamp so the board row's wait
        # elapsed survives a daemon restart.
        slot_queued_since=sess.get("slot_queued_since"),
    )


def _persist_active_sessions(active_sessions: dict[str, Any]) -> dict[str, PersistedSession]:
    """Convert live `active_sessions` into the v2-snapshot-safe shape (065 Fix 7b)."""
    out: dict[str, PersistedSession] = {}
    for card_id, sess in active_sessions.items():
        if not isinstance(sess, dict):
            continue
        cid = str(card_id)
        if not cid:
            continue
        out[cid] = _persist_one_session(cid, sess)
    return out


def _persist_env_cache(env_cache: dict[str, Any]) -> dict[str, EnvCacheStateSnapshot]:
    """Convert live `env_cache` into v3-snapshot-safe shape (073 Fix 3).

    Transient fields (bootstrap_in_flight, pending_sha, runtime_health_failed)
    are deliberately not serialised — they always reset on restart so a crash
    mid-bootstrap does not leave a permanently-stuck flag on disk.
    """
    out: dict[str, EnvCacheStateSnapshot] = {}
    for sym_name, ec_state in env_cache.items():
        if ec_state is None:
            continue
        # Tolerate either pydantic EnvCacheState or plain dict.
        get = (
            (lambda k, _s=ec_state: getattr(_s, k, None))
            if not isinstance(ec_state, dict)
            else ec_state.get
        )
        sym = str(sym_name)
        if not sym:
            continue
        cache_dir = get("cache_dir")
        out[sym] = EnvCacheStateSnapshot(
            symphony_name=str(get("symphony_name") or sym),
            sanitised_name=str(get("sanitised_name") or ""),
            cache_dir=str(cache_dir) if cache_dir is not None else "",
            readme_sha=get("readme_sha"),
            last_bootstrap_error=get("last_bootstrap_error"),
            last_bootstrap_at=get("last_bootstrap_at"),
            last_bootstrap_succeeded=get("last_bootstrap_succeeded"),
            cache_dir_ready=bool(get("cache_dir_ready") or False),
            bootstrap_attempts=int(get("bootstrap_attempts") or 0),
            bootstrap_exhausted=bool(get("bootstrap_exhausted") or False),
            # 092 (FR-016/FR-017): persist the discovered test-env PATH only.
            test_env_source=get("test_env_source"),
            # 124 (US2): docs/wiki wiki-init durable state (wiki_in_flight is
            # transient and deliberately not persisted).
            wiki_initialized=bool(get("wiki_initialized") or False),
            wiki_attempts=int(get("wiki_attempts") or 0),
            wiki_exhausted=bool(get("wiki_exhausted") or False),
            last_wiki_init_at=get("last_wiki_init_at"),
            last_wiki_init_succeeded=get("last_wiki_init_succeeded"),
            last_wiki_init_error=get("last_wiki_init_error"),
        )
    return out


def _dict_or_none(value: Any) -> dict[str, Any] | None:
    """Persisted-session round-trip helper: ``None`` stays ``None``, a
    mapping is shallow-copied into a plain dict."""
    return dict(value) if value is not None else None


def _restored_session_dict(
    card_id: str,
    persisted: PersistedSession,
    snapshot: WorkflowSnapshot,
    current_card: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build the live session dict for one persisted session (065 Fix 7b).

    The current_card payload in each session is rebuilt from the live board
    by check_board's re-adopt path; here we only need the durable
    behaviour-affecting fields (performer_stage, phase, error counters,
    lifecycle bookkeeping).  The session matching the snapshot's focus card
    is seeded with the top-level current_card; other sessions get a stub
    that check_board will replace from the live board.
    """
    session_id = persisted.agent_session_id
    if not session_id and card_id == snapshot.active_card_id:
        session_id = snapshot.agent_session_id
    dispatch = {"session_id": session_id} if session_id else {}
    if session_id and persisted.agent_performer_id:
        dispatch["performer_id"] = persisted.agent_performer_id
    if session_id and persisted.agent_job_id:
        dispatch["job_id"] = persisted.agent_job_id
    missing_identity = (
        not session_id and persisted.phase in {"monitoring_performer", "monitoring_agent"}
        and not persisted.pending_pr_handoff
    )
    session_dict: dict[str, Any] = {
        "board_paused": persisted.board_paused,
        "board_pause_column": persisted.board_pause_column,
        "board_pause_resume_phase": persisted.board_pause_resume_phase,
        "agent_dispatch": dispatch,
        "performer_stage": persisted.performer_stage,
        "phase": "dispatching" if missing_identity else persisted.phase,
        "agent_dispatch_at": persisted.agent_dispatch_at,
        "last_production_at": persisted.last_production_at,
        "last_production_fingerprint": persisted.last_production_fingerprint,
        "reconciled_dispatch_pending": persisted.reconciled_dispatch_pending or missing_identity,
        "lifecycle_completed_at": persisted.lifecycle_completed_at,
        "processed_review_ids": set(persisted.processed_review_ids),
        "surfaced_stale_reviews": dict(persisted.surfaced_stale_reviews),
        "open_questions": list(persisted.open_questions),
        "consumed_loop_questions": list(persisted.consumed_loop_questions),
        "card_clarifications": list(persisted.card_clarifications),
        "pending_override": _dict_or_none(persisted.pending_override),
        "pr_comment_tracking": dict(persisted.pr_comment_tracking),
        "pending_pr_handoff": _dict_or_none(persisted.pending_pr_handoff),
        "pr_artefacts_recorded_at": persisted.pr_artefacts_recorded_at,
        "relay_feedback": list(persisted.relay_feedback),
        "dispatched_feedback": dict(persisted.dispatched_feedback),
        "system_error_count": persisted.system_error_count,
        "system_error_reason": persisted.system_error_reason,
        "system_error_notified": persisted.system_error_notified,
        "requirements_changed": persisted.requirements_changed,
        "last_blocked_notified_at": persisted.last_blocked_notified_at,
        "last_blocked_slack_delivered_at": persisted.last_blocked_slack_delivered_at,
        "head_at_dispatch": persisted.head_at_dispatch,
        "head_at_last_turn": persisted.head_at_last_turn,
        "persona_scope": persisted.persona_scope,
        "bounce_counter": dict(persisted.bounce_counter),
        "local_fix_counter": dict(persisted.local_fix_counter),
        "inheritance_repair_counter": dict(persisted.inheritance_repair_counter),
        "repair_audit": [r.model_dump(mode="json") for r in persisted.repair_audit],
        "ci_gate_rollup_signature": persisted.ci_gate_rollup_signature,
        "ci_gate_advisory_failures": [],
                    # 095: restore per-card ENV_BLOCKED hold/dedup state so a
                    # still-active block does not re-notify after a restart.
                    "env_blocked": _dict_or_none(persisted.env_blocked),
                    # 096: restore the per-card auto-rebase anti-thrash marker so
                    # a BLOCKED conflict isn't re-attempted right after a restart.
                    "last_rebase_attempt": _dict_or_none(persisted.last_rebase_attempt),
        # 123: restore split bounce budget counters + assessor Q&A
        # carryover so they survive a daemon restart.
        "content_feedback_cycles": persisted.content_feedback_cycles,
        "transient_error_cycles": persisted.transient_error_cycles,
        "assessor_open_questions": [dict(q) for q in persisted.assessor_open_questions],
        # 125: restore stage-verdict memory (plain dicts at session
        # level) + the per-card issue-comment dedup watermark so a
        # restart neither re-runs passed stages nor re-classifies
        # processed comments.
        "stage_verdicts": {
            sv_stage: sv.model_dump(mode="json")
            for sv_stage, sv in persisted.stage_verdicts.items()
        },
        "processed_issue_comment_ids": set(persisted.processed_issue_comment_ids),
        "last_issue_comment_id": persisted.last_issue_comment_id,
        "pipeline_admitted": persisted.pipeline_admitted,
        # 126: restore the terminal-success-floor state so the
        # feedback contract and no-op budget survive restarts.
        "feedback_ledger": [
            fb.model_dump(mode="json") for fb in persisted.feedback_ledger
        ],
        "feedback_origin_sha": persisted.feedback_origin_sha,
        "noop_success_retries": persisted.noop_success_retries,
        # 141: restore in-flight attempt IDs so terminal nodes can
        # call close_attempt after a daemon restart (A-008).
        "last_attempt_id": persisted.last_attempt_id,
        "last_attempt_log_path": persisted.last_attempt_log_path,
        "last_attempt_failure_source": persisted.last_attempt_failure_source,
        # 165: the blueprint and the documenter side run survive a
        # restart so the remaining briefs and the side run's
        # once-per-hash rule still hold.
        "documentation_findings": dict(persisted.documentation_findings),
        "blueprint": dict(persisted.blueprint) if persisted.blueprint else None,
        "blueprint_signature": persisted.blueprint_signature,
        "last_progress_at": persisted.last_progress_at,
        "last_progress_fingerprint": persisted.last_progress_fingerprint,
        "idle_timeout_retries": dict(persisted.idle_timeout_retries),
        "documenting_side": (
            persisted.documenting_side.model_dump(mode="json")
            if persisted.documenting_side is not None
            else None
        ),
        # 354: restore the queued-for-slot stamp so a card that was
        # waiting on a saturated pool keeps its real wait elapsed.
        "slot_queued_since": persisted.slot_queued_since,
    }
    if card_id == snapshot.active_card_id and current_card:
        session_dict["current_card"] = current_card
    else:
        session_dict["current_card"] = {"id": card_id}
    session_dict["current_card"] = {
        **session_dict["current_card"], **persisted.pr_artefacts,
    }
    return session_dict


def _reopen_attempt_logs(
    restored_sessions: dict[str, dict[str, Any]], attempt_log: Any,
) -> None:
    """141 A-008: re-register any in-flight attempt with AttemptLog so
    close_attempt writes the end row to the correct JSONL file even
    after a midnight rollover + restart.
    """
    if attempt_log is None:
        return
    from pathlib import Path as _Path

    for _card_id, _sess in restored_sessions.items():
        _aid = _sess.get("last_attempt_id")
        _alp = _sess.get("last_attempt_log_path")
        _card = _sess.get("current_card") or {}
        _task_id = str(_card.get("id", "") or _card_id)
        if isinstance(_aid, str) and _aid and isinstance(_alp, str) and _alp:
            attempt_log.reopen_attempt(_aid, _task_id, _Path(_alp))


def _pr_comment_tracking_signature(session: Mapping[str, Any]) -> tuple[Any, ...]:
    tracking = session.get("pr_comment_tracking")
    tracking = tracking if isinstance(tracking, dict) else {}
    versions = tracking.get("versions")
    versions = versions if isinstance(versions, dict) else {}
    return (
        str(tracking.get("pr_node_id") or ""), str(tracking.get("updated_since") or ""),
        tuple(sorted((str(k), str(v)) for k, v in versions.items())),
    )


def _recovery_signature(session: Mapping[str, Any]) -> tuple[Any, ...]:
    """Fingerprint durable reconciliation changes without runner payloads."""
    dispatch = session.get("agent_dispatch")
    dispatch = dispatch if isinstance(dispatch, dict) else {}
    side = session.get("documenting_side")
    side = side if isinstance(side, dict) else {}
    override = session.get("pending_override")
    override = override if isinstance(override, dict) else {}
    card = session.get("current_card") or {}
    handoff = session.get("pending_pr_handoff") or {}
    return (
        str(session.get("phase") or ""), bool(session.get("reconciled_dispatch_pending")),
        bool(session.get("board_paused")), str(session.get("board_pause_column") or ""),
        str(session.get("board_pause_resume_phase") or ""),
        *(str(dispatch.get(key) or "") for key in ("session_id", "performer_id", "job_id")),
        *(str(side.get(key) or "") for key in ("session_id", "job_id", "status")),
        bool(side.get("writer_active")),
        (str(override.get("action") or ""), str(override.get("target_stage") or ""), bool(override.get("applied"))),
        _pr_comment_tracking_signature(session),
        tuple(str(card.get(key) or "") for key in ("pr_url", "pr_node_id", "pr_number", "head_after", "pushed_branch", "plan_path")),
        (str(handoff.get("stage") or ""), str(handoff.get("completed_at") or ""),
         str(handoff.get("resumed_board_column") or "")),
        tuple(str(session.get(key) or "") for key in (
            "agent_dispatch_at", "last_production_at", "last_production_fingerprint",
        )),
    )


def _synthesize_v1_session_log(active_card_id: str, phase: str) -> None:
    logger.info(
        "state_store.v1_snapshot_rehydrated",
        active_card_id=active_card_id,
        phase=phase,
    )


def _synthesize_v1_session(
    snapshot: WorkflowSnapshot, current_card: dict[str, Any] | None,
) -> dict[str, Any]:
    """066 FR-005 / T004: v1-snapshot synthesis.  Pre-Fix-7 snapshots
    populated active_card_id + per-card top-level fields but had no
    active_sessions payload.  Synthesize a single-entry session so
    the unified pickup path sees a v2-shaped state.  check_board's
    re-adopt path will refresh the session from the live board on
    the first cycle.
    """
    missing_identity = not snapshot.agent_session_id and snapshot.phase in {"monitoring_performer", "monitoring_agent"}
    return {
        "current_card": current_card,
        "agent_dispatch": {"session_id": snapshot.agent_session_id} if snapshot.agent_session_id else {},
        "reconciled_dispatch_pending": missing_identity,
        "performer_stage": snapshot.performer_stage or "implementing",
        "phase": "dispatching" if missing_identity else snapshot.phase,
        "lifecycle_completed_at": snapshot.lifecycle_completed_at,
        "processed_review_ids": set(snapshot.processed_review_ids),
        "surfaced_stale_reviews": dict(snapshot.surfaced_stale_reviews),
        "open_questions": list(snapshot.open_questions),
        "card_clarifications": list(snapshot.card_clarifications),
        "relay_feedback": [],
        "system_error_count": 0,
        "system_error_reason": None,
        "system_error_notified": False,
        "requirements_changed": False,
        "last_blocked_notified_at": snapshot.last_blocked_notified_at,
        "last_blocked_slack_delivered_at": None,
        "head_at_dispatch": None,
        "head_at_last_turn": None,
        "persona_scope": None,
        # 125 (schema v13): synthesized v1 sessions start with the
        # same empty verdict/watermark state as a fresh card so the
        # session shape matches _SESSION_FIELDS (adversarial-review
        # fix — the v2+ restore path above already sets these).
        "stage_verdicts": {},
        "processed_issue_comment_ids": set(),
        "last_issue_comment_id": None,
        # 126 (schema v15): same fresh-card defaults as above.
        "feedback_ledger": [],
        "feedback_origin_sha": None,
        "noop_success_retries": 0,
        # 165: fresh-card defaults.
        "documentation_findings": {},
        "blueprint": None,
        "blueprint_signature": None,
        "documenting_side": None,
    }


def _rehydrate_env_cache(state: CoordinareState, snapshot: WorkflowSnapshot) -> None:
    """073 Fix 3: rehydrate env_cache readme_sha + bookkeeping onto the
    live EnvCacheState entries that EnvCacheService.initialise() already
    populated with readme_sha=None.  Without this, every restart re-runs
    env_bootstrap because check_and_trigger sees the SHA "change".
    Transient flags (bootstrap_in_flight, pending_sha, runtime_health_failed)
    are intentionally left at their initialise() defaults so a crash
    mid-bootstrap does not leave a stuck flag on disk.
    """
    if not snapshot.env_cache:
        return
    live_env_cache = state.get("env_cache")
    if not isinstance(live_env_cache, dict):
        return
    for sym_name, persisted in snapshot.env_cache.items():
        live = live_env_cache.get(sym_name)
        if live is None:
            # Symphony in snapshot is no longer configured — skip.
            continue
        # Live entry is a pydantic EnvCacheState; mutate the
        # rehydratable fields in place.
        try:
            live.readme_sha = persisted.readme_sha
            live.last_bootstrap_at = persisted.last_bootstrap_at
            live.last_bootstrap_succeeded = persisted.last_bootstrap_succeeded
            live.last_bootstrap_error = persisted.last_bootstrap_error
            live.cache_dir_ready = persisted.cache_dir_ready
            # 088 (FR-009): the breaker budget survives restarts.
            live.bootstrap_attempts = persisted.bootstrap_attempts
            live.bootstrap_exhausted = persisted.bootstrap_exhausted
            # 124 (US3): the wiki-init marker + breaker survive
            # restarts so an initialized symphony is never re-seeded.
            live.wiki_initialized = getattr(persisted, "wiki_initialized", False)
            live.wiki_attempts = getattr(persisted, "wiki_attempts", 0)
            live.wiki_exhausted = getattr(persisted, "wiki_exhausted", False)
            live.last_wiki_init_at = getattr(persisted, "last_wiki_init_at", None)
            live.last_wiki_init_succeeded = getattr(
                persisted, "last_wiki_init_succeeded", None,
            )
            live.last_wiki_init_error = getattr(persisted, "last_wiki_init_error", None)
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning(
                "state_store.env_cache_rehydrate_failed",
                symphony=sym_name,
                error=str(exc),
            )


def _seed_symphony_runtime_state(
    state: CoordinareState, snapshot: WorkflowSnapshot,
) -> dict[str, dict[str, Any]] | None:
    """Also seed the owning SymphonyRuntimeState. In multi-symphony mode the
    per-symphony swap in _conduct_single_symphony reads sym_state.active_card
    and sym_state.previous_phase as the source of truth — if those are not
    primed from the snapshot, cycle 1 clobbers the just-restored top-level
    state with None / default and the card is never re-adopted. The
    snapshot has no project-number field, so we can only safely map when
    there is exactly one symphony; otherwise leave it to the in-graph
    re-adopt path (check_board) to pick the card off the live board.

    Returns the legacy sessions that could not be attributed to a symphony
    (multi-symphony restore), or None when everything was seeded.
    """
    sym_states = state.get("symphony_states") or {}
    if len(sym_states) == 1:
        (sym_state,) = sym_states.values()
        sym_state.active_sessions = dict(state.get("active_sessions") or {})
        current_card = state.get("current_card")
        if current_card is not None and sym_state.active_card is None:
            sym_state.active_card = current_card
        if sym_state.previous_phase is None:
            sym_state.previous_phase = snapshot.phase
        return None
    if sym_states:
        # Project item IDs are board-specific. Retain legacy sessions until
        # a successful read proves which board owns each ID.
        return dict(state.get("active_sessions") or {})
    return None


def _derive_global_phase(active_sessions: dict[str, Any]) -> DaemonPhase:
    """Derive the global daemon phase from the highest-priority session phase."""
    if not active_sessions:
        return "idle"
    best = "idle"
    best_pri = 0
    for sess in active_sessions.values():
        p = str(sess.get("phase") or "idle")
        pri = _PHASE_PRIORITY.get(p, 0)
        if pri > best_pri:
            best = p
            best_pri = pri
    return cast("DaemonPhase", best)


# Global state keys mutated by graph nodes (e.g. check_board) that are shared
# across all sessions in a cycle.  After the concurrent fanout, these are merged
# back into self._state from the first successful result so they're not lost.
#: Per-cycle bookkeeping dicts that `check_board` creates with
#: ``state.setdefault(key, {})`` and that MUST be shared across the concurrent fanout.
#:
#: `check_board` is a graph node, so it runs once per session, and each session gets a
#: SHALLOW copy of the daemon state (``session_state = dict(self._state)``). If one of
#: these keys is absent before the fanout, every session's ``setdefault`` builds its own
#: dict and no session can see another's marks — issue #247, where two sessions recovered
#: the same blocked card in one cycle and made duplicate GitHub calls.
#:
#: Seeding them here fixes both halves at once. A shallow copy shares the VALUE object, so
#: every session mutates one dict: marks are visible immediately within the cycle, and the
#: daemon's own state carries them to the next cycle with no merge step. That matters
#: because the global merge-back is first-writer-wins, not a union, so merging alone would
#: still have dropped every session but one.
SHARED_CYCLE_MARKER_KEYS: tuple[str, ...] = (
    # 129: anti-thrash marker, so a blocked card is recovered once per daemon run.
    "_recovery_attempts",
    # dependency-announcement latch, so a blocked-by note is posted once, not per cycle.
    "_dep_announcements",
)


def seed_shared_cycle_markers(state: CoordinareState) -> None:
    """Ensure every shared per-cycle marker map exists BEFORE the fanout.

    Idempotent, and deliberately non-destructive: an existing dict is left in place (it
    holds live marks) rather than replaced. A non-mapping value — which a restored
    snapshot could carry — is repaired to an empty dict, because ``setdefault`` would
    otherwise hand the node back a scalar it then tries to index.
    """
    for key in SHARED_CYCLE_MARKER_KEYS:
        if not isinstance(state.get(key), dict):
            state[key] = {}  # type: ignore[literal-required]


_GLOBAL_STATE_KEYS: tuple[str, ...] = (
    "last_known_main_sha",
    "last_rebase_round",
    "last_poll_at",
    "github_retry_queue",
    "github_retry_after",
    # 428: observer retune overrides + audit are symphony-scoped flat state
    # written by the monitor's observer phase; without these they would be
    # discarded by the fanout's global merge before the next dispatch.
    "observer_retunes",
    "observer_retune_audit",
    # phase is NOT included here; it is derived explicitly from active_sessions
    # after the merge loop to avoid misreporting the daemon as idle when only
    # the first completed session had phase="idle" while others are still active.
)


def _pick_stable_active_card_id(active_sessions: dict[str, Any]) -> str | None:
    """066 FR-010: choose an active_card_id when the previous pointer is gone.

    Picks the session with the earliest ``picked_up_at`` so the dashboard
    top-level fields don't ping-pong between siblings as sessions are picked
    up / retired during a cycle.  Sessions without a parseable timestamp sort
    after dated ones; among undated (or all-missing) the lexicographically
    smallest ``card_id`` wins.  Card IDs are stable identifiers, so this gives
    a deterministic choice across runs.

    Both datetime and ISO-string ``picked_up_at`` values are normalised to UTC
    before lex-sorting, so the ordering compares instants — not local strings.
    Naive datetimes are assumed UTC.  Unparseable ISO strings remain in the
    dated bucket and sort against each other by raw value.  Mixing all shapes
    in the same session set is safe: every dated branch produces a string key,
    so all dated sessions remain mutually comparable.
    """
    if not active_sessions:
        return None

    def _normalise_dt(dt: datetime) -> str:
        aware = dt if dt.tzinfo else dt.replace(tzinfo=UTC)
        return aware.astimezone(UTC).isoformat()

    def _key(item: tuple[str, Any]) -> tuple[int, str]:
        card_id, sess = item
        picked = sess.get("picked_up_at") if isinstance(sess, dict) else None
        if isinstance(picked, datetime):
            return (0, _normalise_dt(picked))
        if isinstance(picked, str) and picked:
            try:
                return (0, _normalise_dt(datetime.fromisoformat(picked)))
            except ValueError:
                # Unparseable string — keep it in the dated bucket but sort by
                # raw value so two unparseable strings still compare against
                # each other deterministically.
                return (0, picked)
        return (1, card_id)

    return min(active_sessions.items(), key=_key)[0]


def _has_in_review_work(session: dict[str, Any], column: str) -> bool:
    """Whether a review card retains performer work despite its board column."""
    if column.strip().lower() not in {"in review", "in_review"}:
        return False
    phase = session.get("phase")
    if phase in {"monitoring_performer", "monitoring_agent"}:
        dispatch = session.get("agent_dispatch")
        return isinstance(dispatch, dict) and bool(dispatch.get("session_id"))
    return phase == "dispatching" and bool(
        session.get("relay_feedback") or session.get("pending_override")
        or session.get("reconciled_dispatch_pending"),
    )


def _compute_eligibility(
    card_id: str,
    session: dict[str, Any],
    board_snapshot: dict[str, list[str]],
    dep_graph: DependencyGraph | None,
) -> SessionEligibility:
    """Derive per-cycle eligibility for a session from board state."""
    card = session.get("current_card") or {}
    card_item_id = str(card.get("content_id") or card.get("id") or "")
    if session.get("board_paused") or not card_item_id:
        reason = KICKED_BACK if session.get("board_paused") else MISSING_CARD
        return SessionEligibility(card_id=card_id, eligible=False, reason=reason)

    blocked_cards = board_snapshot.get("BLOCKED", [])
    if card_item_id in blocked_cards:
        return SessionEligibility(card_id=card_id, eligible=False, reason=BLOCKED_COLUMN)

    if dep_graph is not None:
        deps = dep_graph.by_dependent.get(card_item_id, [])
        unresolved = [d for d in deps if d.status != DependencyStatus.SATISFIED]
        if unresolved:
            return SessionEligibility(
                card_id=card_id,
                eligible=False,
                reason=DEPENDENCY_BLOCKED,
                blockers=[d.blocker_issue_number for d in unresolved],
            )

    # 065 US3: kick-back detection. If a performer is actively running for
    # this session (phase=monitoring_performer) but the operator has moved
    # the card out of IN_PROGRESS on the board, the session is orphaned —
    # the performer will keep producing results the daemon must discard.
    # Only treat as kick-back when board_snapshot is populated AND the card
    # is observably in some non-IN_PROGRESS column (so a missing board, or
    # a card not yet visible to the board fetch, doesn't trip a false
    # orphan). The skip-loop converts this into a dispatcher.performer_orphaned
    # log so the operator can see the strand.
    if session.get("phase") == "monitoring_performer" and board_snapshot and not session.get("pending_pr_handoff"):
        in_progress = board_snapshot.get("IN_PROGRESS", [])
        # A refused review-feedback board move can leave a live performer
        # in IN_REVIEW; it still needs its graph tick for monitoring/recovery.
        if card_item_id not in in_progress and card_item_id not in board_snapshot.get("IN_REVIEW", []):
            in_other_column = any(
                card_item_id in cards
                for col, cards in board_snapshot.items()
                if col != "IN_PROGRESS" and isinstance(cards, list)
            )
            if in_other_column:
                return SessionEligibility(card_id=card_id, eligible=False, reason=KICKED_BACK)

    # The board is authoritative even when the cached card still says IN_REVIEW.
    if card_item_id in board_snapshot.get("BACKLOG", []):
        return SessionEligibility(card_id=card_id, eligible=False, reason=BACKLOG_COLUMN)

    return SessionEligibility(card_id=card_id, eligible=True, reason=ELIGIBLE)


def _log_session_skip(
    card_id: str,
    session: dict[str, Any],
    elig: SessionEligibility,
) -> None:
    """Emit the per-cycle skip log for an ineligible session.

    ``kicked_back`` is escalated to ``dispatcher.performer_orphaned`` to
    satisfy 065 US3 FR-009/b — the operator must see a structured record
    naming the stranded performer.  Other skip reasons emit the existing
    ``session_skipped`` info log unchanged.
    """
    if elig.reason == KICKED_BACK:
        # The session dict doesn't persist a separate performer_id; the
        # performer_stage uniquely identifies the responsible pool slot.
        logger.warning(
            "dispatcher.performer_orphaned",
            performer_id=session.get("performer_stage") or "",
            card_id=card_id,
            reason=elig.reason,
        )
        return
    logger.info(
        "session_skipped",
        card_id=card_id,
        reason=elig.reason,
        blockers=elig.blockers,
    )


# Maps (previous_phase, current_phase) tuples to canonical metric transition labels.
# Transitions not listed here are not recorded (e.g. recovery→idle, relay_feedback→*).
_PHASE_TRANSITION_METRIC: dict[tuple[str, str], str] = {
    ("idle", "dispatching"): "idle_to_dispatch",
    ("dispatching", "monitoring_agent"): "dispatch_to_monitor",
    ("dispatching", "monitoring_pr"): "dispatch_to_monitor",
    ("monitoring_agent", "merging"): "monitor_to_merge",
    ("monitoring_pr", "merging"): "monitor_to_merge",
    ("monitoring_agent", "blocked"): "monitor_to_blocked",
    ("monitoring_pr", "blocked"): "monitor_to_blocked",
    ("blocked", "idle"): "blocked_to_idle",
}

# Maps circuit-breaker service names to the HEALTH subsystem they represent.
# Services not listed here are not registered as health probes and are skipped.
# When the circuit is open and poll=0 (webhook-only mode), the daemon cannot
# rely on _wait_for_next_cycle() for recovery — no webhook will arrive if
# GitHub is down. Use a fixed backoff so the circuit can probe-recover.
_CIRCUIT_OPEN_BACKOFF_SECONDS: int = 60
_BOOTSTRAP_POLL_MAX_ATTEMPTS: int = 720  # 720 x 10 s = 7200 s ~= 2 h
# A single status poll can fail transiently (e.g. a 30 s HTTP timeout while the
# bootstrap container is mid-compile under host load) without the bootstrap
# actually being dead. Tolerate a few *consecutive* such failures before
# declaring the bootstrap failed; the counter resets on any successful poll.
_BOOTSTRAP_POLL_MAX_CONSECUTIVE_FAILURES: int = 3


def _bootstrap_progress_lines(log_lines: list[str]) -> list[str]:
    """076 (live QA #150): the meaningful subset of an env_bootstrap container's
    logs, for idle detection.

    Coordinare polls ``GET /jobs/<id>`` every cycle, flooding the container's
    stdout with uvicorn access lines that grow whether or not the bootstrap is
    doing any work.  Filtering them out leaves the real progress signal — LLM
    ``shim request`` lines, ``service_inference`` steps, and install command
    output — which only changes when the bootstrap actually advances.  A frozen
    result across the idle window means the container is hung, not just slow.
    """
    return [ln for ln in log_lines if "/jobs/" not in ln and not ln.lstrip().startswith("INFO:")]


@dataclass
class _BootstrapPollState:
    """Loop-local state threaded through the bootstrap poll helpers."""

    last_logs_snapshot: list[str] = field(default_factory=list)
    last_progress: list[str] | None = None
    last_progress_attempt: int = 0
    consecutive_failures: int = 0


@dataclass
class _BootstrapBackend:
    """Resolved performer-backend parameters for one env_bootstrap dispatch."""

    backend: str = "codex"
    model: str | None = None
    effort: str | None = None
    temperature: float | None = None
    base_url: str | None = None
    api_key_env: str | None = None
    auth_token_env: str | None = None
    orchestration: dict[str, Any] | None = None


async def _docker_logs_lines(container_id: str, tail: int) -> list[str]:
    """Run ``docker logs --tail <tail>`` and return the decoded lines."""
    proc = await asyncio.create_subprocess_exec(
        "docker",
        "logs",
        "--tail",
        str(tail),
        container_id,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout_b, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
    return stdout_b.decode(errors="replace").splitlines()


async def _snapshot_container_logs(
    container_id: str | None, last_snapshot: list[str],
) -> list[str]:
    """Snapshot container logs for idle detection.  Snapshot *before*
    check_status, because a terminal status causes HTTPPerformerService to
    stop and ``--rm``-remove the container, after which ``docker logs``
    returns nothing.  ``--tail 500`` keeps enough history that the
    meaningful (non-poll) lines don't scroll out of the idle-detection
    window under poll-noise.
    """
    if not container_id:
        return last_snapshot
    try:
        snap = await _docker_logs_lines(container_id, 500)
        if snap:
            return snap[-500:]
    except Exception:
        pass
    return last_snapshot


async def _bootstrap_failure_logs(
    svc: Any,
    job_id: str,
    container_id: str | None,
    last_logs_snapshot: list[str],
) -> list[str]:
    """Collect the log tail attached to a terminal bootstrap failure:
    the pre-poll snapshot, then a direct ``docker logs`` pull, then the
    service's agent-log buffer as a last resort.
    """
    logs_tail: list[str] = list(last_logs_snapshot[-60:]) if last_logs_snapshot else []
    # Pull logs directly from the bootstrap container by id.
    # The shared `_log_buffer` is unreliable here because a
    # single HTTPPerformerService is used for both bootstrap
    # and implementing dispatches, so the implementing job's
    # poll task may have overwritten the buffer before we
    # observed bootstrap's terminal status.
    bootstrap_cid: str | None = container_id
    if bootstrap_cid is None:
        try:
            active_jobs = getattr(svc, "_active_jobs", {}) or {}
            job_obj = active_jobs.get(job_id)
            if job_obj is not None:
                bootstrap_cid = getattr(job_obj, "container_id", None)
        except Exception:
            bootstrap_cid = None
    if bootstrap_cid and not logs_tail:
        try:
            lines = await _docker_logs_lines(bootstrap_cid, 100)
            logs_tail = list(lines)[-60:]
        except Exception as exc:
            logs_tail = [f"<docker logs failed: {exc}>"]
    if not logs_tail and hasattr(svc, "get_agent_logs"):
        try:
            raw_logs = svc.get_agent_logs()
            if isinstance(raw_logs, list):
                logs_tail = [str(line) for line in raw_logs[-40:]]
            elif isinstance(raw_logs, str):
                logs_tail = raw_logs.splitlines()[-40:]
        except Exception as exc:
            logs_tail = [f"<get_agent_logs failed: {exc}>"]
    return logs_tail


_CIRCUIT_TO_HEALTH_SUBSYSTEM: dict[str, str] = {
    "github": "github",
    "agent": "agent",
    "agent_ssh": "agent",
}


class RuntimeExecutionError(RuntimeError):
    def __init__(self, *, phase: str, step: str, cause: Exception) -> None:
        super().__init__(f"{phase} failure in {step}: {cause}")
        self.phase = phase
        self.step = step
        self.cause = cause


#: 139 — how often a card that is still stuck is logged again. A stall that
#: persists is worth repeating occasionally; repeating it every cycle is noise.
_STALL_LOG_COOLDOWN_SECONDS = 3600.0

#: 139 — the size at which expired stall keys are swept. Not a hard cap: memory
#: here is bounded by how many distinct cards can be stuck within the cooldown,
#: which is bounded by the board.
_MAX_LOGGED_STALLS = 512


def should_log_stall(
    seen: dict[str, float], key: str, now: float, cooldown: float = _STALL_LOG_COOLDOWN_SECONDS,
) -> bool:
    """Should this stall be logged now? Records the decision in *seen*.

    Extracted so the policy can be tested directly. Asserting on the source text
    of the watchdog — which is what the first version of these tests did — cannot
    tell a correct policy from a plausible-looking one, and this policy had two
    wrong versions before this one:

    * remembering keys and clearing the dict when it grew. Once live stalls
      exceeded the bound, every remembered key became loggable again on the next
      cycle;
    * evicting the oldest key instead. Same outcome by a different route, because
      the eviction cascades — the evicted key logs, evicting the next.

    Bounding by count cannot work when the working set exceeds the bound. Bounding
    by *time* is bounded by the board instead, and a card that is still stuck
    reminding an operator once an hour is better than one line and then silence.
    """
    last = seen.get(key)
    if last is not None and now - last < cooldown:
        return False
    seen[key] = now
    if len(seen) > _MAX_LOGGED_STALLS:
        expired = [k for k, at in seen.items() if now - at >= cooldown]
        for k in expired:
            del seen[k]
    return True


def resolve_stuck_card(state: CoordinareState) -> dict[str, Any]:
    """398: the card whose identity a card_stuck alert should carry.

    The ``current_card`` mirror is absent exactly when no card is pinned — the
    common case in ``dispatching``. When exactly one session is in flight it
    names the stuck card; with zero or several there is no single answer, and
    guessing one would be wrong.
    """
    card = state.get("current_card") or {}
    if card:
        return card
    sessions = state.get("active_sessions") or {}
    if len(sessions) == 1:
        only = next(iter(sessions.values()))
        sess_card = only.get("current_card") if isinstance(only, dict) else None
        if isinstance(sess_card, dict) and sess_card:
            return sess_card
    return {}


def stuck_dedup_key(card_id: str, phase: str, phase_entered: Any) -> str:
    """398: the one dedup key policy for the stall log and the notification.

    With a card identity, key on it — two cards stuck in the same phase are
    different stalls. Without one (the phase was entered before any card was
    adopted; ``dispatching`` is the common case), key on the phase entry
    instead: every cardless stall in a phase used to degrade to a single
    ``stuck::phase`` key, so the second cardless stall was suppressed, not
    just anonymous.
    """
    if card_id:
        return f"stuck:{card_id}:{phase}"
    return f"stuck:(no-card):{phase}:{phase_entered}"


async def _preflight_seed_main_sha_cache(
    state: CoordinareState, github: Any, active_sessions: dict[str, Any],
) -> None:
    """Pre-seed _main_sha_cache so concurrent sessions share one ls-remote
    result instead of each making an independent call, then run the
    main-advance detection + rebase round once for the whole cycle.
    """
    _config = state.get("config")
    if _config is None or not hasattr(github, "_current_token"):
        return
    # suppress is scoped only to _current_token() — fetch_main_sha
    # and rebase failures are logged explicitly so they're visible.
    _token = ""
    with contextlib.suppress(Exception):
        _token = await github._current_token()
    if not _token:
        return
    _repo_url = repo_url_from_config(_config)
    if not _repo_url:
        return
    try:
        _sha = await fetch_main_sha(_repo_url, _token)
    except Exception:
        logger.warning(
            "multi_session.preflight.sha_fetch_failed", exc_info=True,
        )
        _sha = None
    if _sha:
        state["_main_sha_cache"] = _sha  # type: ignore[typeddict-unknown-key]
        # Run main-advance detection once in preflight so
        # every per-session check_board copy inherits the
        # updated last_known_main_sha and skips its own
        # run_rebase_round — preventing N parallel rebase
        # rounds when main advances with N active sessions.
        _prev_sha = state.get("last_known_main_sha")
        if _prev_sha is None:
            state["last_known_main_sha"] = _sha
        elif _sha != _prev_sha:
            logger.info(
                "multi_session.preflight.main_head_changed",
                old_sha=_prev_sha[:8],
                new_sha=_sha[:8],
            )
            state["last_known_main_sha"] = _sha
            try:
                _rr = await run_rebase_round(
                    active_sessions,
                    _sha,
                    _repo_url,
                    _token,
                    notification_service=state.get(
                        "notification_service",
                    ),
                    github=github,
                    human_reviewers=state.get("human_reviewers"),
                    board_snapshot=state.get("board_snapshot"),
                )
                state["last_rebase_round"] = _rr.to_dict()
                # Mirror check_board's conflict-resolution
                # handoff: route the first BLOCKED job back
                # to implementing so relay_feedback fires.
                from coordinare.models.rebase import RebaseOutcome
                from coordinare.services.rebase import (
                    prepare_conflict_resolution,
                )

                for _job in _rr.jobs:
                    if _job.outcome == RebaseOutcome.BLOCKED:
                        _sess = active_sessions.get(_job.card_id)
                        if isinstance(_sess, dict):
                            prepare_conflict_resolution(
                                _job,
                                _sess,
                                human_reviewers=state.get(
                                    "human_reviewers",
                                ),
                            )
                        break
            except Exception:
                logger.warning(
                    "multi_session.preflight.rebase_round_failed",
                    exc_info=True,
                )


def _mirror_board_metadata(state: CoordinareState, board: dict[str, Any]) -> None:
    """062 Fix 4: propagate per-card metadata so dashboard swimlane
    can render titles + GitHub links in multi-session mode.
    check_board sets these too, but they're not in _GLOBAL_STATE_KEYS
    so per-session mutations are dropped after the fanout merge.
    """
    for _meta_src, _meta_dst in (
        ("titles", "_board_titles"),
        ("issue_numbers", "_board_issue_numbers"),
        ("issue_urls", "_board_issue_urls"),
        ("pr_urls", "_board_pr_urls"),
    ):
        _meta_val = board.get(_meta_src)
        if isinstance(_meta_val, dict):
            state[_meta_dst] = _meta_val  # type: ignore[literal-required]


def _has_fresh_board_snapshot(state: CoordinareState) -> bool:
    """Only this cycle's successful, well-formed poll can change pause intent."""
    board = state.get("_board_cache")
    snapshot = board.get("snapshot") if isinstance(board, dict) else None
    return _valid_board_snapshot(snapshot) and snapshot == state.get("board_snapshot")


def _valid_board_snapshot(snapshot: Any) -> bool:
    """An empty successful snapshot proves absence; malformed data does not."""
    return isinstance(snapshot, dict) and all(
        isinstance(column, str) and isinstance(ids, list)
        and all(isinstance(item_id, str) for item_id in ids)
        for column, ids in snapshot.items()
    )


async def _preflight_poll_board(
    state: CoordinareState, github: Any, active_sessions: dict[str, Any],
) -> None:
    """Pre-flight: poll the board once so all concurrent sessions share the
    cache and eligibility can be computed before the fanout.  Respects the
    same github_operation_ready/backoff state used by check_board so
    multi-session mode doesn't bypass transient-outage handling.
    """
    ready, retry_in = github_operation_ready(state, "poll_board")
    if not ready:
        logger.info(
            "multi_session.pre_poll_deferred",
            retry_in_seconds=round(retry_in, 1),
        )
        return
    try:
        provider = board_of(state, github)
        if provider is None:
            logger.info("multi_session.pre_poll_no_board")
            return
        board = await provider.poll_board()
        state["_board_cache"] = board  # type: ignore[typeddict-unknown-key]
        clear_deferred_github_operation(state, "poll_board")
        state["last_poll_at"] = datetime.now(UTC)
        snapshot = board.get("snapshot")
        if isinstance(snapshot, dict):
            state["board_snapshot"] = snapshot
        _mirror_board_metadata(state, board)
        await _preflight_seed_main_sha_cache(state, github, active_sessions)
    except Exception as _poll_exc:
        # Transient upstream GitHub failures (5xx, timeouts, DNS) are
        # routine — log a single-line warning without the traceback so
        # operators aren't alarmed by what's effectively a retry signal.
        if is_transient_github_outage_error(_poll_exc):
            logger.warning(
                "multi_session.pre_poll_failed",
                error_type=type(_poll_exc).__name__,
                error=str(_poll_exc)[:300],
                transient=True,
            )
            defer_github_operation(state, operation="poll_board", error=_poll_exc)
        else:
            logger.warning("multi_session.pre_poll_failed", exc_info=True)


def _compute_session_eligibilities(
    state: CoordinareState, active_sessions: dict[str, Any], max_cards: int,
) -> dict[str, SessionEligibility]:
    """Build the dependency graph from the pre-fetched board (when cached),
    compute eligibility for every session, apply pipeline-budget selection,
    and record skip reasons for ineligible sessions.
    """
    # NOTE: This uses build_graph only — it does not run resolve_off_board_dependencies,
    # so sessions blocked by a now-closed off-board issue may be conservatively skipped
    # this cycle.  The full resolution runs inside each session's check_board tick and
    # will correct the dep state by the following cycle.
    dep_graph: DependencyGraph | None = None
    cached_board = state.get("_board_cache")
    if cached_board is not None:
        try:
            dep_graph = _build_dep_graph(cast("dict[str, Any]", cached_board))
        except Exception:
            logger.warning("multi_session.dep_graph_failed", exc_info=True)

    board_snapshot: dict[str, list[str]] = state.get("board_snapshot") or {}

    eligibilities: dict[str, SessionEligibility] = {
        card_id: _compute_eligibility(card_id, session, board_snapshot, dep_graph)
        for card_id, session in active_sessions.items()
    }

    from coordinare.services.pipeline_budget import select_pipelines

    selected = select_pipelines(
        active_sessions, max_cards,
        {cid for cid, eligibility in eligibilities.items() if eligibility.eligible},
    )
    state["_pipeline_selected"] = selected
    for cid, eligibility in eligibilities.items():
        if eligibility.eligible and cid not in selected:
            eligibilities[cid] = SessionEligibility(
                card_id=cid, eligible=False, reason="pipeline_capacity",
            )

    # Record skip reasons for ineligible sessions.
    skip_reasons: dict[str, dict[str, Any]] = {}
    for card_id, elig in eligibilities.items():
        if not elig.eligible:
            skip_reasons[card_id] = {
                "reason": elig.reason,
                "detail": None,
                "blockers": elig.blockers,
            }
            _log_session_skip(card_id, active_sessions[card_id], elig)
    state["session_skip_reasons"] = skip_reasons
    return eligibilities


async def _all_ineligible_fallback(
    state: CoordinareState, graph: Any, active_sessions: dict[str, Any],
) -> CoordinareState:
    """Fallback when every session is ineligible this cycle (e.g. all
    BLOCKED / dependency_blocked): run a single full graph invocation so
    check_board can still pick up new sessions from open slots or do other
    per-cycle maintenance.  Without this, check_board never fires and
    available slots go unfilled until at least one existing session becomes
    eligible.  Returns the (possibly replaced) state.
    """
    # The symphony swap restores the card pointer, not its flat fields.
    # Without hydration, fallback writes aggregate defaults (assessing,
    # blueprint=None) over a completed plan when the last worker blocks.
    if any(sess.get("board_paused") for sess in active_sessions.values()):
        # Admission and board maintenance must continue, but the worker graph
        # cannot run on a paused focus. A neutral flat view keeps check_board
        # from retiring the paused card through focus-specific cleanup.
        maintenance = dict(state)
        for field in _SESSION_FIELDS:
            maintenance.pop(field, None)
        maintenance.update(current_card=None, active_card_id=None, phase="idle", agent_dispatch={})
        updated = await check_board(cast("CoordinareState", maintenance))
        # check_board changes the shared session map directly. Copy only
        # global maintenance fields back; preserve the authoritative focus.
        for key, value in updated.items():
            if key not in _SESSION_FIELDS and key != "active_card_id":
                cast("dict[str, Any]", state)[key] = value
        _finalize_multi_session(state, state.get("active_sessions") or active_sessions)
        return state
    focus = state.get("active_card_id")
    if focus in active_sessions:
        session_to_state(active_sessions[focus], state)
        state["active_card_id"] = focus
        _rederive_current_card(state)
    state = await graph.ainvoke(state)
    # 069: mirror flat-state mutations back onto active_sessions[active_card_id]
    # so the next cycle's _derive_global_phase / slot_manager.sync_from_sessions
    # see the same view as the per-session fanout writeback at line 911.  Without
    # this, a same-cycle readopt+dispatch leaves session.phase="dispatching"
    # while flat state["phase"]="monitoring_performer"; _derive_global_phase
    # then clobbers the flat phase back to "dispatching" and slot_manager
    # releases the slot, orphaning the implementer container.
    active_card_id = state.get("active_card_id")
    if active_card_id:
        active_sessions_after = state.get("active_sessions") or {}
        if active_card_id in active_sessions_after:
            active_sessions_after[active_card_id] = state_to_session(state)
    # If the graph settled into a passive phase (monitoring_pr),
    # clear active_card_id so route_issue_comments doesn't poll the
    # card's issue on every cycle.  check_board rescans the full board
    # each cycle and re-points active_card_id when it needs to handle
    # or dispatch a card.  066 FR-010: current_card is derived.
    if state.get("phase") in PASSIVE_PHASES:
        state["active_card_id"] = None
    _rederive_current_card(state)
    return state


@dataclass
class _FanoutContext:
    """Stable inputs for one fanout round of per-session graph ticks."""

    state: CoordinareState
    active_sessions: dict[str, Any]
    eligibilities: dict[str, SessionEligibility]
    blocked_to_poll: set[str]
    graph: Any
    semaphore: asyncio.Semaphore


def _prepare_session_state(
    ctx: _FanoutContext, card_id: str,
) -> tuple[CoordinareState, dict[str, dict[str, Any]]]:
    """Snapshot the stable pre-fanout state for one session's graph tick and
    deep-copy only the session being invoked.  Returns the per-session state
    plus the shallow sibling snapshots captured before ainvoke.
    """
    session_state = cast("CoordinareState", dict(ctx.state))
    if session_state.get("github_retry_queue") is not None:
        session_state["github_retry_queue"] = list(
            session_state["github_retry_queue"],
        )
    # Deep-copy only the session being invoked (inside the semaphore
    # so the concurrency bound also limits peak copy memory).
    # Siblings are shallow-copied from the stable pre-fanout
    # active_sessions; nodes only mutate top-level sibling keys so
    # shallow isolation is sufficient.
    session_state["active_sessions"] = {
        cid: (copy.deepcopy(sess) if cid == card_id else dict(sess))
        for cid, sess in ctx.active_sessions.items()
    }
    session_to_state(session_state["active_sessions"][card_id], session_state)
    # 066 FR-010 / T006: identify the active session so the
    # per-session graph step can re-derive current_card from
    # active_sessions[active_card_id].
    session_state["active_card_id"] = card_id
    _rederive_current_card(session_state)
    # Snapshot sibling sessions before ainvoke.  Graph nodes such
    # as prepare_conflict_resolution can mutate session dicts
    # in-place; capturing shallow copies here lets us detect
    # real mutations post-ainvoke by value comparison.
    pre_fanout_siblings: dict[str, dict[str, Any]] = {
        k: dict(v)
        for k, v in session_state["active_sessions"].items()
        if k != card_id
    }
    return session_state, pre_fanout_siblings


def _collect_session_updates(
    ctx: _FanoutContext,
    card_id: str,
    session: dict[str, Any],
    updated: CoordinareState,
    pre_fanout_siblings: dict[str, dict[str, Any]],
) -> tuple[CardSession, dict[str, Any]]:
    """Post-ainvoke merge for one session tick: fold in-place session
    mutations, capture new sibling sessions + cross-session mutations, and
    select the global state keys.  Returns (updated_session, g_updates).
    """
    updated_session = state_to_session(updated)
    # Merge in any mutations that nodes made directly to
    # updated["active_sessions"][card_id] without mirroring them
    # back onto the flat state fields (e.g. prepare_conflict_resolution
    # routing a BLOCKED card by writing phase/performer_stage directly
    # into the session dict).  Only apply an in-place value when the
    # flat field was NOT independently updated — flat mutations take
    # priority so that nodes using the canonical flat-field path are
    # not overwritten by a stale pre-fanout deep copy.
    _in_place_session = (updated.get("active_sessions") or {}).get(card_id)
    if _in_place_session:
        for _f in _SESSION_FIELDS:
            if _f not in _in_place_session:
                continue
            _pre_val = session.get(_f)
            if updated_session.get(_f) == _pre_val:
                # flat field unchanged — apply in-place mutation if any
                _ip_val = _in_place_session[_f]
                if _ip_val != _pre_val:
                    updated_session[_f] = _ip_val  # type: ignore[literal-required]
    # Capture new sessions added by check_board so they survive
    # the fanout merge.  Only keys not present before dispatch
    # are considered new to avoid overwriting concurrent updates.
    updated_sessions = updated.get("active_sessions") or {}
    new_sessions = {
        k: v for k, v in updated_sessions.items() if k not in ctx.active_sessions
    }
    # Capture mutations to other existing sessions (e.g. prepare_conflict_resolution
    # routing a BLOCKED session back to dispatching).  Only include sessions
    # that actually changed vs the pre-fanout snapshot so that an unmodified
    # deep-copy of a sibling can't clobber a real mutation applied by another
    # concurrent task via last-writer-wins in cross_mutations.update(cm).
    cross_session = {
        k: v
        for k, v in updated_sessions.items()
        if k != card_id and k in ctx.active_sessions and v != pre_fanout_siblings.get(k)
    }
    g_updates: dict[str, Any] = {
        k: updated[k] for k in _GLOBAL_STATE_KEYS if k in updated  # type: ignore[literal-required]
    }
    if new_sessions:
        g_updates["_new_sessions"] = new_sessions
    if cross_session:
        g_updates["_cross_session_mutations"] = cross_session
    return updated_session, g_updates


async def _invoke_session_tick(
    ctx: _FanoutContext, card_id: str, session: dict[str, Any],
) -> AsyncSessionTickResult:
    """Run one session's graph tick under the fanout semaphore."""
    elig = ctx.eligibilities[card_id]
    # A BLOCKED-column session is ineligible for a dispatch slot, but its
    # graph must still run each cycle so check_board's un-block
    # comment-poll executes: otherwise a blocked card can never be
    # un-blocked by a fresh issue comment while any sibling is eligible
    # (the all-ineligible fallback that would run the poll never fires).
    # phase=blocked keeps it exempt from the *dispatch-slot* accounting
    # in NON_SLOT_PHASES, so it never occupies a performer slot. It does
    # take a fanout permit and pay a state deep-copy, and review pointed
    # out that the part which scales is the count, not the latency: every
    # blocked card is still an active session, so an accumulating blocked
    # backlog -- the exact board this recovers -- would add an unbounded
    # number of graph ticks per cycle. _blocked_sessions_to_poll caps and
    # rotates them instead. Skipped ones stay recorded in
    # session_skip_reasons for operator visibility.
    if not elig.eligible and card_id not in ctx.blocked_to_poll:
        return AsyncSessionTickResult(
            card_id=card_id, ok=True, session_state=session, skipped=True,
        )
    pre_session = dict(session)
    async with ctx.semaphore:
        # State prep runs inside the semaphore so the concurrency bound
        # also limits peak memory from simultaneous deep-copies.
        # ctx.state is stable throughout the fanout (mutated only after
        # all results are merged), so sessions that acquire the semaphore
        # at different times still snapshot the same pre-fanout state.
        t0 = perf_counter()
        try:
            session_state, pre_fanout_siblings = _prepare_session_state(ctx, card_id)
            updated = await ctx.graph.ainvoke(session_state)
            updated_session, g_updates = _collect_session_updates(
                ctx, card_id, session, updated, pre_fanout_siblings,
            )
            return AsyncSessionTickResult(
                card_id=card_id,
                ok=True,
                session_state=cast("dict[str, Any]", updated_session),
                duration_ms=int((perf_counter() - t0) * 1000),
                global_updates=g_updates,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("session_graph_error", card_id=card_id, exc_info=True)
            return AsyncSessionTickResult(
                card_id=card_id,
                ok=False,
                error=str(exc),
                session_state=pre_session,
                duration_ms=int((perf_counter() - t0) * 1000),
            )


def _merge_retry_queue_entry(
    merged_retry_queue: list[dict[str, Any]], entry: dict[str, Any],
) -> None:
    """Dedupe one retry-queue entry into the merged queue: keep the highest
    attempt; on a tie, the later retry_at so two sessions that deferred the
    same op at the same attempt (but slightly different wall-clock times)
    don't shorten the backoff.
    """
    op = entry.get("operation")
    existing = next(
        (
            e
            for e in merged_retry_queue
            if isinstance(e, dict) and e.get("operation") == op
        ),
        None,
    )
    if existing is None:
        merged_retry_queue.append(entry)
        return
    entry_attempt = int(entry.get("attempt", 0))
    existing_attempt = int(existing.get("attempt", 0))
    # Keep the most conservative entry: higher attempt wins;
    # on a tie, keep the later retry_at so two sessions that
    # deferred the same op at the same attempt (but slightly
    # different wall-clock times) don't shorten the backoff.
    # Compare as datetime objects — retry_at is always a
    # datetime in-memory; str() comparison is fragile across
    # tz representations.
    entry_ra = entry.get("retry_at")
    existing_ra = existing.get("retry_at")
    later_retry_at = (
        isinstance(entry_ra, datetime)
        and isinstance(existing_ra, datetime)
        and entry_ra > existing_ra
    )
    if entry_attempt > existing_attempt or (
        entry_attempt == existing_attempt and later_retry_at
    ):
        merged_retry_queue[merged_retry_queue.index(existing)] = entry


def _record_unconfirmed_board_pause(session: dict[str, Any]) -> None:
    """Retain a closed-PR guard while warning about an uncertain stop."""
    if not is_closed_pr_block(session.get("system_error_reason")):
        session["system_error_reason"] = "Board paused; worker termination is unconfirmed"


def _observe_handoff_board_column(session: dict[str, Any], column: str) -> None:
    """Expire a resume receipt once a fresh board read leaves that column."""
    handoff = session.get("pending_pr_handoff") or {}
    resumed = handoff.get("resumed_board_column")
    if resumed and column in {"TODO", "BACKLOG", "IN_PROGRESS", "IN_REVIEW", "BLOCKED", "DONE"} and column != resumed:
        handoff.pop("resumed_board_column", None)


def _handoff_needs_board_pause(session: dict[str, Any], column: str) -> bool:
    """A completed turn can pause without any remaining worker identity."""
    handoff = session.get("pending_pr_handoff") or {}
    return bool(handoff) and column in {"TODO", "BACKLOG"} and column != handoff.get("resumed_board_column")


def _is_manual_board_block(session: dict[str, Any], column: str) -> bool:
    """A new Blocked move holds working intent without inventing a blocker."""
    return (
        column == "BLOCKED"
        and session.get("phase") in {"dispatching", "monitoring_performer", "monitoring_agent", "monitoring_pr", "merging"}
        and not session.get("open_questions")
        and not session.get("system_error_reason")
        and not session.get("env_blocked")
        and (session.get("latest_ci_gate_decision") or {}).get("verdict") != "escalate"
    )


def _capture_board_pause_resume_phase(session: dict[str, Any]) -> None:
    """Remember foreground intent before cancellation changes ownership/phase."""
    if session.get("board_paused"):
        return
    phase = str(session.get("phase") or "idle")
    if phase == "monitoring_performer" and not session.get("pending_pr_handoff"):
        phase = "dispatching"
    session["board_pause_resume_phase"] = phase


def _resume_board_paused_session(session: dict[str, Any]) -> None:
    """Resume foreground intent, retaining the closed-PR reopen check."""
    phase = str(session.get("board_pause_resume_phase") or "dispatching")
    session["board_pause_resume_phase"] = ""
    if is_closed_pr_block(session.get("system_error_reason")):
        return
    session.update(phase=phase, reconciled_dispatch_pending=phase == "dispatching", system_error_reason=None)


def _preserve_paused_terminal_ownership(session: dict[str, Any], column: str | None) -> bool:
    """Keep terminal writers visible to startup recovery and owned cleanup."""
    if not session.get("board_paused") or (column is not None and column.strip().upper() != "DONE"):
        return False
    if not has_owned_writers(session):
        return False
    session["phase"] = "monitoring_performer"
    return True


async def _release_session_resources(
    state: CoordinareState,
    sess: dict[str, Any],
) -> None:
    """Issue #489: best-effort release-time teardown for a session the
    daemon is about to drop.

    A session is removed on completion OR missing-card eviction, but the
    076 service-side cleanup only fires when the job reports terminal — a
    job wedged mid-run leaves its container alive with no session behind
    it. Before removal, ask the session's performer service (keyed by
    ``performer_stage``) to stop the pod still tracked for the session's
    ``agent_dispatch.session_id``. Best-effort: a teardown failure is
    logged and swallowed; it must never keep the session alive.
    """
    if not isinstance(sess, dict):
        return
    dispatch = sess.get("agent_dispatch")
    session_id = (
        dispatch.get("session_id") if isinstance(dispatch, dict) else None
    )
    if not session_id:
        return
    stage = sess.get("performer_stage")
    # Resolve the service that dispatched the session: the ID-keyed registry
    # is authoritative (SlotManager may have picked any service in a stage
    # pool, and performer_services[stage] is only the stage default), with
    # the stage lookup as a legacy fallback for dispatches that recorded no
    # performer_id.
    dispatch_result = dispatch if isinstance(dispatch, dict) else {}
    performer_id = dispatch_result.get("performer_id")
    by_id = state.get("performer_services_by_id") or {}
    service = (
        by_id.get(performer_id)
        if performer_id and isinstance(by_id, dict) else None
    )
    if service is None:
        services = state.get("performer_services") or {}
        service = (
            services.get(stage)
            if isinstance(services, dict) and isinstance(stage, str) else None
        )
    release = getattr(service, "release_session", None)
    if not callable(release):
        return
    try:
        await release(session_id)
        logger.info(
            "session_release_teardown",
            session_id=str(session_id),
            performer_stage=stage,
        )
    except Exception as exc:
        logger.warning(
            "session_release_teardown_failed",
            session_id=str(session_id),
            performer_stage=stage,
            error=str(exc),
        )


async def _release_done_session_resources(state: CoordinareState, session: dict[str, Any]) -> None:
    """Hand all retained paused writers to ownership-aware terminal cleanup."""
    await _release_session_resources(state, session)
    if session.get("board_paused") and has_live_side_writer(session):
        await _release_session_resources(state, {
            "performer_stage": "documenting", "agent_dispatch": session["documenting_side"],
        })


async def _retire_nonfocused_paused_terminal(state: CoordinareState, board: dict[str, Any]) -> None:
    """Retire terminal paused siblings without replacing the current focus."""
    sessions = state.get("active_sessions") or {}
    for card_id, session in list(sessions.items()):
        if card_id == state.get("active_card_id") or not session.get("board_paused"):
            continue
        column = CoordinareDaemon._find_card_column(board, card_id, session)
        if column != "DONE" and (column is not None or not _has_fresh_board_snapshot(state)):
            continue
        # Share only the authoritative session map. Retirement's focus and
        # feedback resets belong to this card, not the live sibling's mirrors.
        retirement_state = state.copy()
        card = {**(session.get("current_card") or {}), "id": card_id}
        retirement_state["active_card_id"] = card_id
        cast("dict[str, Any]", retirement_state)["active_card"] = card
        retirement_state["current_card"] = card
        result = reconcile_board_state(
            retirement_state, {"DONE": [card_id]} if column == "DONE" else {},
            allow_paused_missing=column is None,
        )
        retired = result.get("retired_session")
        if isinstance(retired, dict):
            await _release_done_session_resources(state, retired)


async def _merge_fanout_results(
    state: CoordinareState,
    active_sessions: dict[str, Any],
    eligibilities: dict[str, SessionEligibility],
    results: list[AsyncSessionTickResult],
) -> None:
    """Merge global state updates from results.  Non-session, non-queue keys
    come from the first successful result.  github_retry_queue is merged
    across ALL results (dedupe by operation, keep highest attempt) so that
    deferred entries from any session are not silently dropped.  New sessions
    added by check_board are also merged from ALL results so no slot is lost.
    Ops that were present at fanout start but are absent in any successful
    result are treated as cleared and removed from the merged queue.
    """
    pre_fanout_ops: set[str] = {
        e["operation"]
        for e in (state.get("github_retry_queue") or [])
        if isinstance(e, dict) and e.get("operation")
    }
    cleared_ops: set[str] = set()
    first_global_merged = False
    merged_retry_queue: list[dict[str, Any]] | None = None
    cross_mutations: dict[str, dict[str, Any]] = {}
    for result in results:
        if not result.ok or result.skipped or not result.global_updates:
            continue
        if not first_global_merged:
            for k, v in result.global_updates.items():
                if k not in (
                    "_new_sessions",
                    "_cross_session_mutations",
                    "github_retry_queue",
                    "github_retry_after",
                ):
                    state[k] = v  # type: ignore[literal-required]
            first_global_merged = True
        rq = result.global_updates.get("github_retry_queue")
        if isinstance(rq, list):
            session_ops = {
                e["operation"] for e in rq if isinstance(e, dict) and e.get("operation")
            }
            cleared_ops.update(pre_fanout_ops - session_ops)
            if merged_retry_queue is None:
                merged_retry_queue = list(rq)
            else:
                for entry in rq:
                    if isinstance(entry, dict):
                        _merge_retry_queue_entry(merged_retry_queue, entry)
        cm = result.global_updates.get("_cross_session_mutations") or {}
        cross_mutations.update(cm)  # last-writer-wins across concurrent results
        new_sessions = result.global_updates.get("_new_sessions") or {}
        for cid, sess in new_sessions.items():
            if cid not in active_sessions:
                active_sessions[cid] = sess
                logger.info("new_session_registered", card_id=cid)
    if merged_retry_queue is not None:
        if cleared_ops:
            merged_retry_queue = [
                e
                for e in merged_retry_queue
                if not (isinstance(e, dict) and e.get("operation") in cleared_ops)
            ]
        state["github_retry_queue"] = merged_retry_queue
        retry_ats = [
            e["retry_at"]
            for e in merged_retry_queue
            if isinstance(e, dict) and isinstance(e.get("retry_at"), datetime)
        ]
        state["github_retry_after"] = min(retry_ats) if retry_ats else None

    # Apply cross-session mutations as a baseline before direct results so
    # each session's own tick result takes precedence over mutations from a
    # sibling session's graph run (e.g. prepare_conflict_resolution targeting
    # a BLOCKED session that was skipped this cycle).
    for cid, mutated_sess in cross_mutations.items():
        if cid in active_sessions:
            active_sessions[cid] = mutated_sess

    await _merge_session_results(state, active_sessions, eligibilities, results)


async def _merge_session_results(
    state: CoordinareState,
    active_sessions: dict[str, Any],
    eligibilities: dict[str, SessionEligibility],
    results: list[AsyncSessionTickResult],
) -> None:
    """Merge session results back into active_sessions; collect completions."""
    completed_ids: list[str] = []
    for result in results:
        if result.skipped:
            # missing_card sessions have no card to resume — remove them so
            # the slot doesn't linger forever.
            if eligibilities[result.card_id].reason == MISSING_CARD:
                logger.warning("session_missing_card_evicted", card_id=result.card_id)
                completed_ids.append(result.card_id)
            continue
        if result.ok:
            # Only overwrite on success; preserves any cross-session
            # mutations applied above for sessions whose own tick failed.
            # A side poll can finish while fanout holds an older copy. Preserve
            # its terminal record instead of resurrecting the running snapshot.
            live_side = (active_sessions.get(result.card_id) or {}).get("documenting_side")
            result_side = result.session_state.get("documenting_side")
            if (isinstance(live_side, dict) and isinstance(result_side, dict)
                    and live_side.get("session_id") == result_side.get("session_id")
                    and live_side.get("status") in {"done", "failed"}
                    and (result_side.get("status") == "running" or result_side.get("writer_active"))):
                result.session_state["documenting_side"] = dict(live_side)
            active_sessions[result.card_id] = result.session_state
            sess = result.session_state
            if sess.get("phase", "idle") == "idle" and sess.get("current_card") is None:
                completed_ids.append(result.card_id)

    for card_id in completed_ids:
        sess = active_sessions.get(card_id) or {}
        await _release_session_resources(state, sess)
        del active_sessions[card_id]
        logger.info("session_completed", card_id=card_id)


def _finalize_multi_session(
    state: CoordinareState, active_sessions: dict[str, Any],
) -> None:
    """End-of-cycle writeback: publish sessions, derive the global phase,
    keep a stable active_card_id pointer, and re-derive current_card.
    """
    state["active_sessions"] = active_sessions
    # Derive global phase from the highest-priority session phase so the
    # dashboard never shows a stale or idle value while work is ongoing.
    state["phase"] = _derive_global_phase(active_sessions)
    # 066 FR-010: end-of-cycle re-derive.  Keep the previous active_card_id
    # pointer when its session is still present so the dashboard top-level
    # fields don't flicker between siblings on each cycle.  When the prior
    # pointer is gone (card completed), pick the session with the *earliest*
    # picked_up_at — a stable key that survives dict-insertion reordering
    # and won't ping-pong between siblings as sessions advance.  Fall back
    # to lexicographic card_id when picked_up_at is missing (handled inside
    # the picker).  The next per-session check_board invocation
    # re-establishes active_card_id via the unified pickup path regardless.
    active_id = state.get("active_card_id")
    if not active_id or active_id not in active_sessions:
        state["active_card_id"] = (
            _pick_stable_active_card_id(active_sessions)
        )
    _rederive_current_card(state)


class CoordinareDaemon:
    def __init__(
        self,
        graph: Any,
        *,
        run_mode: str = "shell",
        poll_interval_seconds: float = 30,
        heartbeat_interval_seconds: int = 30,
        max_cycles: int | None = None,
        sleep_func: Any = asyncio.sleep,
        state_store: StateStore | None = None,
        idle_threshold_seconds: int = 1800,
        dashboard_store: DashboardStore | None = None,
        webhook_trigger: asyncio.Event | None = None,
    ) -> None:
        # Rotates the BLOCKED-session poll window; see _blocked_sessions_to_poll.
        self._blocked_poll_cursor: int = 0
        self._graph = graph
        self._run_mode = run_mode
        self._poll_interval_seconds = poll_interval_seconds
        self._heartbeat_interval_seconds = heartbeat_interval_seconds
        self._max_cycles = max_cycles
        self._sleep = sleep_func
        self._running = False
        self._stop_event = asyncio.Event()
        self._webhook_trigger: asyncio.Event = webhook_trigger or asyncio.Event()
        self._state: CoordinareState = initial_state()
        # 139: when each stall key was last logged, so a card that stays stuck
        # reminds rather than reprints every cycle.
        self._logged_stalls: dict[str, float] = {}
        self._session_stuck_alerts: dict[tuple[str, str], float] = {}
        self._stuck_observed_starts: dict[tuple[str, str], datetime] = {}
        self._cycle_active = False
        self._stop_during_cycle = False
        self._state_store = state_store
        self._idle_threshold_seconds = idle_threshold_seconds
        self._dashboard_store = dashboard_store
        self._main_task: asyncio.Task[None] | None = None
        self._config_reload_trigger: asyncio.Event = asyncio.Event()
        # 060: References to in-flight bootstrap poll tasks (prevents GC).
        self._bootstrap_poll_tasks: set[asyncio.Task[None]] = set()
        # 124 (US2): manual wiki-init requests from the dashboard button, drained
        # once per cycle; the WikiInitService brain (auto-merge + state) is reused,
        # but the trigger here is operator-initiated (no auto-gate), so it never
        # holds other dispatch. Poll-task refs kept to prevent GC.
        self._wiki_init_requests: set[str] = set()
        self._wiki_init_poll_tasks: set[asyncio.Task[None]] = set()
        # 173: poll tasks for the two card-less intake runs.
        self._intake_poll_tasks: set[asyncio.Task[None]] = set()
        # 165: completion polls for documenter side runs (out-of-lifecycle).
        self._documenting_side_tasks: set[asyncio.Task[Any]] = set()
        from coordinare.services.wiki_init import WikiInitService

        self._wiki_init_svc = WikiInitService()  # auto-gate disabled; manual trigger only
        # 088 (US5): completion handlers (env-cache bootstrap) flush the
        # snapshot immediately — a bootstrap finishing moves no lifecycle
        # signature, so the signature-gated save above would otherwise defer
        # the success to the NEXT stage transition and a restart in that
        # window would rewind it.
        self._state["snapshot_save_fn"] = self._flush_snapshot

    @property
    def running(self) -> bool:
        return self._running

    @property
    def state(self) -> CoordinareState:
        return self._state

    @property
    def state_store(self) -> StateStore | None:
        return self._state_store

    def _lifecycle_signature(self) -> tuple[Any, ...]:
        """Lightweight fingerprint of lifecycle-relevant state.

        Changes whenever a card's stage advances, so it can trigger snapshot
        persistence even when the daemon ``phase`` is unchanged (``phase`` stays
        ``monitoring_performer`` across an entire card lifecycle). Includes the
        per-session ``performer_stage`` so multi-symphony / shared-pool stage
        progress — held in ``active_sessions`` rather than at top level — is
        captured too. Cheap and must not raise.
        """
        card = self._state.get("current_card")
        card_id = card.get("id") if isinstance(card, dict) else None
        sessions = self._state.get("active_sessions")
        if isinstance(sessions, dict):
            session_stages: tuple[Any, ...] = tuple(
                sorted(
                    (
                        str(cid), str((s or {}).get("performer_stage") or ""),
                        str((s or {}).get("last_attempt_id") or ""),
                        str((s or {}).get("last_attempt_failure_source") or ""),
                        _recovery_signature(s or {}),
                    )
                    for cid, s in sessions.items()
                    if isinstance(s, dict) or s is None
                ),
            )
        else:
            session_stages = ()
        return (
            str(self._state.get("phase") or ""),
            str(card_id or ""),
            str(self._state.get("performer_stage") or ""),
            session_stages,
            _recovery_signature(self._state) if not sessions else (),
        )

    async def _flush_snapshot(self) -> None:
        """088 (US5): save the snapshot NOW, bypassing the lifecycle-signature
        gate. Wired into state as ``snapshot_save_fn`` for completion handlers
        whose updates (e.g. ``last_bootstrap_succeeded``) don't move the
        signature. Defensive: runs as a fire-and-forget task, must not raise.
        """
        if self._state_store is None:
            return
        try:
            await self._state_store.save(self._build_snapshot())
        except Exception as exc:
            logger.warning("daemon.snapshot_flush_failed", error=str(exc))

    def _build_snapshot(self) -> WorkflowSnapshot:
        card = self._state.get("current_card")
        card_dict = card if isinstance(card, dict) else {}
        dispatch = self._state.get("agent_dispatch")
        dispatch_dict = dispatch if isinstance(dispatch, dict) else {}
        raw_questions = self._state.get("open_questions")
        questions = [str(q) for q in raw_questions] if isinstance(raw_questions, list) else []
        raw_clarifications = self._state.get("card_clarifications")
        clarifications = list(raw_clarifications) if isinstance(raw_clarifications, list) else []
        last_notified = self._state.get("last_blocked_notified_at")
        performer_stage = self._state.get("performer_stage")
        # 077: in shared-pool / multi-symphony mode the live stage lives in the
        # active session (top-level current_card/performer_stage stay stale, as
        # active_card_id is None here). Prefer the active session's stage so the
        # persisted top-level field is current — handle_blocked / handle_system_error
        # read state["performer_stage"] (defaulting to "assessing") and would
        # otherwise resume an interrupted card at the wrong stage.
        _snap_sessions = self._state.get("active_sessions")
        if isinstance(_snap_sessions, dict) and _snap_sessions:
            _snap_active_id = _pick_stable_active_card_id(_snap_sessions)
            _snap_sess = _snap_sessions.get(_snap_active_id) if _snap_active_id else None
            if isinstance(_snap_sess, dict) and _snap_sess.get("performer_stage"):
                performer_stage = _snap_sess.get("performer_stage")
        lifecycle_sequence = self._state.get("lifecycle_sequence")

        # Coerce a value to a non-empty string or None.  Critical: ``str(None)``
        # returns the literal string ``"None"`` which then survives the
        # ``or None`` guard because it's truthy — that bug previously wrote
        # ``"pr_url": "None"`` into the snapshot, breaking PR lookups on
        # restart.  Convert None/empty to None FIRST, then stringify.
        def _str_or_none(value: Any) -> str | None:
            if value is None or value == "":
                return None
            return str(value)

        # 045: Persist issue_number/url/description/acceptance_criteria so
        # restore after a restart doesn't dispatch with issue_number=0 (which
        # caused PRs to open without ``Closes #N`` linkage).
        raw_issue_number = card_dict.get("issue_number") if card_dict else None
        issue_number = (
            raw_issue_number if isinstance(raw_issue_number, int) and raw_issue_number > 0 else None
        )
        raw_ac = card_dict.get("acceptance_criteria") if card_dict else None
        acceptance_criteria = [str(c) for c in raw_ac] if isinstance(raw_ac, list) else []

        return WorkflowSnapshot(
            snapshot_at=datetime.now(UTC),
            phase=self._state.get("phase", "idle"),
            active_card_id=_str_or_none(card_dict.get("id")) if card_dict else None,
            active_card_title=_str_or_none(card_dict.get("title")) if card_dict else None,
            active_card_column=_str_or_none(card_dict.get("status")) if card_dict else None,
            active_card_issue_id=_str_or_none(card_dict.get("issue_id")) if card_dict else None,
            active_card_issue_number=issue_number,
            active_card_issue_url=_str_or_none(card_dict.get("issue_url")) if card_dict else None,
            active_card_description=_str_or_none(card_dict.get("description"))
            if card_dict
            else None,
            active_card_acceptance_criteria=acceptance_criteria,
            pr_url=_str_or_none(card_dict.get("pr_url")) if card_dict else None,
            pr_node_id=_str_or_none(card_dict.get("pr_node_id")) if card_dict else None,
            agent_session_id=_str_or_none(dispatch_dict.get("session_id"))
            if dispatch_dict
            else None,
            open_questions=questions,
            card_clarifications=clarifications,
            performer_stage=str(performer_stage)
            if isinstance(performer_stage, str) and performer_stage
            else None,
            lifecycle_sequence=[str(stage) for stage in lifecycle_sequence]
            if isinstance(lifecycle_sequence, list)
            else [],
            last_blocked_notified_at=last_notified if isinstance(last_notified, datetime) else None,
            lifecycle_completed_at=self._state.get("lifecycle_completed_at")
            if isinstance(self._state.get("lifecycle_completed_at"), datetime)
            else None,
            processed_review_ids=sorted(self._state.get("processed_review_ids") or set()),
            surfaced_stale_reviews=dict(self._state.get("surfaced_stale_reviews") or {}),
            active_sessions=_persist_active_sessions({
                **getattr(self, "_unassigned_restored_sessions", {}),
                **(self._state.get("active_sessions") or {}),
            }),
            env_cache=_persist_env_cache(self._state.get("env_cache") or {}),
            # 096: persist the rebase baseline so a main advance that happened
            # while the daemon was down is seen as drift on the next startup.
            last_known_main_sha=(
                self._state.get("last_known_main_sha")
                if isinstance(self._state.get("last_known_main_sha"), str)
                else None
            ),
        )

    def _restore_from_snapshot(self, snapshot: WorkflowSnapshot) -> None:
        self._state["phase"] = snapshot.phase
        self._state["open_questions"] = list(snapshot.open_questions)
        self._state["card_clarifications"] = list(snapshot.card_clarifications)
        if snapshot.lifecycle_sequence:
            self._state["lifecycle_sequence"] = list(snapshot.lifecycle_sequence)
        if snapshot.performer_stage:
            self._state["performer_stage"] = snapshot.performer_stage
        self._state["last_blocked_notified_at"] = snapshot.last_blocked_notified_at
        self._state["lifecycle_completed_at"] = snapshot.lifecycle_completed_at
        self._state["processed_review_ids"] = set(snapshot.processed_review_ids)
        self._state["surfaced_stale_reviews"] = dict(snapshot.surfaced_stale_reviews)
        # 096: restore the rebase baseline (FR-001) so the first check_board
        # cycle compares the live main against the pre-restart value and fires
        # the rebase on genuine cross-restart drift, instead of re-baselining.
        self._state["last_known_main_sha"] = snapshot.last_known_main_sha
        if snapshot.active_card_id:
            _set_current_card(
                self._state,
                {
                    "id": snapshot.active_card_id,
                    "issue_id": snapshot.active_card_issue_id or "",
                    "issue_number": snapshot.active_card_issue_number or 0,
                    "issue_url": snapshot.active_card_issue_url or "",
                    "title": snapshot.active_card_title or "",
                    "description": snapshot.active_card_description or "",
                    "acceptance_criteria": list(snapshot.active_card_acceptance_criteria),
                    "status": snapshot.active_card_column or "",
                    "pr_url": snapshot.pr_url,
                    "pr_node_id": snapshot.pr_node_id,
                },
            )
        if snapshot.agent_session_id:
            self._state["agent_dispatch"] = {"session_id": snapshot.agent_session_id}

        # 065 Fix 7b: restore per-card sessions from the v2 snapshot.  The
        # current_card payload in each session is rebuilt from the live board
        # by check_board's re-adopt path; here we only need the durable
        # behaviour-affecting fields (performer_stage, phase, error counters,
        # lifecycle bookkeeping).  v1 snapshots have an empty active_sessions
        # dict, so this loop is a no-op and the existing single-card flat
        # restore (above) drives recovery.
        current_card = self._state.get("current_card")
        if snapshot.active_sessions:
            restored_sessions = {
                card_id: _restored_session_dict(card_id, persisted, snapshot, current_card)
                for card_id, persisted in snapshot.active_sessions.items()
            }
            self._state["active_sessions"] = restored_sessions
            # 141 A-008: re-register any in-flight attempt with AttemptLog so
            # close_attempt writes the end row to the correct JSONL file even
            # after a midnight rollover + restart.
            _reopen_attempt_logs(restored_sessions, self._state.get("attempt_log"))
        elif snapshot.active_card_id and current_card:
            _synthesize_v1_session_log(snapshot.active_card_id, snapshot.phase)
            self._state["active_sessions"] = {
                snapshot.active_card_id: _synthesize_v1_session(snapshot, current_card),
            }

        _rehydrate_env_cache(self._state, snapshot)

        # 066 FR-010 / T006: set active_card_id and re-derive the mirror so
        # the post-restore state satisfies the I3 invariant.  Subsequent
        # cycles maintain it via check_board and _invoke_multi_session.
        if snapshot.active_card_id:
            self._state["active_card_id"] = snapshot.active_card_id
        _rederive_current_card(self._state)

        unassigned = _seed_symphony_runtime_state(self._state, snapshot)
        if unassigned is not None:
            self._unassigned_restored_sessions = unassigned

    @staticmethod
    def _infer_phase_from_board_column(column: str) -> WorkflowPhase:
        normalized = column.strip().lower()
        if normalized in {"in progress", "in_progress"}:
            return "monitoring_agent"
        if normalized in {"in review", "in_review"}:
            return "monitoring_pr"
        if normalized == "blocked":
            return "blocked"
        return "idle"

    async def _reconcile_with_board(self, snapshot: WorkflowSnapshot) -> None:
        """Query the live board and reconcile restored state against it."""
        if len(self._state.get("symphony_states") or {}) > 1:
            logger.warning("restart_reconcile.ambiguous_session_ownership",
                           session_count=len(snapshot.active_sessions))
            return
        github = self._state.get("github_service")
        # In multi-symphony mode the global github_service exists but is not
        # initialized (project_id=0, no field_cache — project_number lives
        # per-symphony). Fall back to any per-symphony service that is ready.
        if not getattr(github, "project_id", None):
            sym_svcs = self._state.get("symphony_github_services") or {}
            github = next(
                (svc for svc in sym_svcs.values() if getattr(svc, "project_id", None)),
                None,
            )
            if github is None:
                return
        try:
            provider = board_of(self._state, github)
            if provider is None:
                logger.warning("restart_reconcile.no_board_provider")
                return
            board = await provider.poll_board()
            board_snapshot = board.get("snapshot")
            if not _valid_board_snapshot(board_snapshot):
                logger.warning("restart_reconcile.invalid_board_snapshot")
                return
            board_snapshot = cast("dict[str, list[str]]", board_snapshot)
            if not snapshot.active_card_id:
                self._reconcile_sessions_with_board(board_snapshot, snapshot)
                return
            found_column: str | None = None
            for column, card_ids in board_snapshot.items():
                if isinstance(card_ids, list) and snapshot.active_card_id in card_ids:
                    found_column = column
                    break

            focus_session = (self._state.get("active_sessions") or {}).get(snapshot.active_card_id) or {}
            if _preserve_paused_terminal_ownership(focus_session, found_column):
                self._state["phase"] = "monitoring_performer"
            elif found_column is None or found_column.upper() == "DONE":
                logger.warning(
                    "board_contradicts_snapshot",
                    active_card_id=snapshot.active_card_id,
                    found_column=found_column,
                )
                self._state["phase"] = "idle"
                _retire_active_session(self._state)
            else:
                focus_session = (self._state.get("active_sessions") or {}).get(snapshot.active_card_id)
                intent = focus_session if isinstance(focus_session, dict) else self._state
                inferred = (
                    snapshot.phase if _has_in_review_work(cast("dict[str, Any]", intent), found_column)
                    else self._infer_phase_from_board_column(found_column)
                )
                if inferred != snapshot.phase:
                    logger.info(
                        "board_reconciliation_advanced",
                        active_card_id=snapshot.active_card_id,
                        snapshot_phase=snapshot.phase,
                        board_column=found_column,
                        inferred_phase=inferred,
                    )
                    self._state["phase"] = inferred
                else:
                    logger.info(
                        "board_reconciliation_confirmed",
                        active_card_id=snapshot.active_card_id,
                        phase=snapshot.phase,
                    )
            # 094: the block above reconciles only the top-level focus card.
            # In multi-card mode the authoritative state is in active_sessions,
            # whose phase is restored verbatim — so a session restored
            # BLOCKED/idle for a card the board has moved on (the #158 wedge)
            # is never corrected. Reconcile every restored session against the
            # same (already-fetched) board snapshot: board wins.
            self._reconcile_sessions_with_board(board_snapshot, snapshot)
        except Exception as exc:
            logger.warning(
                "board_reconciliation_skipped",
                error=str(exc),
            )
        finally:
            sym_states = self._state.get("symphony_states") or {}
            if len(sym_states) == 1:
                (runtime,) = sym_states.values()
                runtime.active_sessions = dict(self._state.get("active_sessions") or {})
                runtime.active_card = self._state.get("current_card")
                runtime.previous_phase = self._state.get("phase", "idle")

    def _reconcile_sessions_with_board(
        self, board_snapshot: dict[str, list[str]], snapshot: WorkflowSnapshot | None = None,
    ) -> None:
        """094: reconcile each restored per-card session's phase against the
        live board (board is source of truth).

        - A non-in-flight session whose board-inferred phase differs from its
          persisted phase is corrected (board wins) and emits
          ``restart_reconcile.session_corrected``.
        - A legitimately in-flight session (``monitoring_pr`` /
          ``monitoring_performer``) whose board column is consistent with that
          phase is preserved untouched (FR-004) — its PR/performer context is
          never reset.
        - A genuinely-blocked card stays blocked (FR-005): BLOCKED infers
          ``blocked`` == persisted, so no correction fires.
        - A card DONE or absent from this (successful) board read is retired
          (FR-010); if it was the top-level focus, the focus is cleared.

        Reuses the board snapshot already fetched by the caller — no extra
        round-trip — and runs inside the caller's try/except so a failed board
        read changes nothing (FR-009). Distinct from the spec-076 container
        reconciler (``run_startup_reconciliation``): that adopts/reaps Docker
        containers; this corrects board column/phase.
        """
        sessions = self._state.get("active_sessions") or {}
        self._reconcile_session_phases(sessions, board_snapshot, snapshot)

        # 094 (FR-010): once every session has been reconciled, re-derive the
        # top-level focus phase from the corrected session set so it is
        # consistent immediately — the top-level block above ran BEFORE these
        # per-session corrections, so its self._state["phase"] can be stale
        # (e.g. it inferred monitoring_agent for an IN_PROGRESS focus card whose
        # session is legitimately preserved as monitoring_performer). Guarded on
        # a non-empty set so the focus-only path (no active_sessions) keeps the
        # top-level block's result. _derive_global_phase reflects the
        # highest-priority live session, the same value the first cycle would
        # compute — this just makes it true at reconcile time, not one cycle late.
        if sessions:
            focus = sessions.get(self._state.get("active_card_id") or "")
            if isinstance(focus, dict):
                session_to_state(cast("CardSession", focus), self._state)
            self._state["phase"] = _derive_global_phase(sessions)

    def _reconcile_terminal_startup_session(
        self, sessions: dict[str, Any], card_id: str, session: dict[str, Any], column: str | None,
    ) -> bool:
        if column is not None and column.strip().upper() != "DONE":
            return False
        if _preserve_paused_terminal_ownership(session, column):
            return True
        logger.info(
            "restart_reconcile.session_retired", card_id=card_id,
            prior_phase=session.get("phase"), board_column=column,
            symphony=self._state.get("current_symphony"),
        )
        del sessions[card_id]
        if self._state.get("active_card_id") == card_id:
            _retire_active_session(self._state)
        return True

    def _reconcile_session_phases(
        self, sessions: dict[str, Any], board_snapshot: dict[str, list[str]], snapshot: WorkflowSnapshot | None,
    ) -> None:
        """094: per-card phase reconciliation loop (board is source of truth)."""
        # The board column that is *consistent* with each in-flight phase. If
        # the live column matches, the session is genuinely mid-flight and is
        # preserved; if it differs, the card advanced and is moved forward.
        inflight_consistent = {
            "monitoring_performer": {"in progress", "in_progress"},
            "monitoring_pr": {"in review", "in_review"},
        }
        symphony = self._state.get("current_symphony")
        for card_id in list(sessions.keys()):
            session = sessions.get(card_id)
            if not isinstance(session, dict):
                continue
            column = self._find_card_column(board_snapshot, card_id, session)
            prior_phase = session.get("phase")
            # prior_column is best-effort: only the focus card's column is
            # persisted (in the snapshot), so it is None for every other card.
            # The board column is the divergence's NEW side; the persisted
            # divergence itself is captured by prior_phase, logged for every card.
            if (
                snapshot is not None
                and card_id == snapshot.active_card_id
                and snapshot.active_card_column
            ):
                prior_column = snapshot.active_card_column
            else:
                prior_column = None

            if self._reconcile_terminal_startup_session(sessions, card_id, session, column):
                continue
            column = cast("str", column)

            if session.get("board_paused"):
                continue
            _observe_handoff_board_column(session, column.strip().upper())
            dispatch = session.get("agent_dispatch") or {}
            if _is_manual_board_block(session, column.strip().upper()) or (column.strip().upper() in {"TODO", "BACKLOG"} and (
                dispatch.get("session_id") or has_live_side_writer(session)
                or _handoff_needs_board_pause(session, column.strip().upper())
            )):
                _capture_board_pause_resume_phase(session)
                session["board_paused"] = True
                session["board_pause_column"] = column.strip().upper()
                # Keep the worker in the startup adoption/stop set. Inferring
                # idle here would orphan it before runtime reconciliation.
                session["phase"] = "monitoring_performer"
            if session.get("board_paused"):
                continue
            if session.get("pending_pr_handoff") and column.strip().upper() in {"TODO", "IN_PROGRESS", "IN_REVIEW"}:
                continue
            normalized = column.strip().lower()
            # Review feedback can be working even while a refused board move
            # leaves the card in review. Preserve its durable routing intent.
            if _has_in_review_work(session, column):
                continue
            # Preserve a still-valid in-flight session (FR-004).
            if (
                prior_phase in inflight_consistent
                and normalized in inflight_consistent[prior_phase]
            ):
                continue

            inferred = self._infer_phase_from_board_column(column)
            if inferred == prior_phase:
                continue  # already consistent — no correction (FR-008 convergence)

            # Correct the phase only. Per-cycle eligibility reads the LIVE board
            # (not any column cached on the session), so the phase is the field
            # that un-wedges the card; there is nothing else to refresh here.
            session["phase"] = inferred
            logger.info(
                "restart_reconcile.session_corrected",
                card_id=card_id,
                prior_phase=prior_phase,
                prior_column=prior_column,
                board_column=column,
                corrected_phase=inferred,
                symphony=symphony,
            )

    @staticmethod
    def _find_card_column(
        board_snapshot: dict[str, list[str]], card_id: str, session: dict[str, Any] | None = None,
    ) -> str | None:
        """Return the live board column for a card, or None if absent.

        Matches the session key (the project item id, the same id space the
        board snapshot and ``active_card_id`` use) and, as a fallback, the
        session's ``current_card`` content-id/id.
        """
        candidates = {card_id}
        if isinstance(session, dict):
            cc = session.get("current_card") or {}
            candidates.add(str(cc.get("content_id") or ""))
            candidates.add(str(cc.get("id") or ""))
        candidates.discard("")
        for column, card_ids in board_snapshot.items():
            if isinstance(card_ids, list) and any(c in card_ids for c in candidates):
                return column
        return None

    async def _wait_for_next_cycle(self) -> None:
        """Wait for the next polling cycle, honouring webhook triggers and poll=0 mode."""
        poll = self._poll_interval_seconds
        if poll > 0:
            # Race the poll sleep against a webhook trigger so either can wake the loop.
            # Using self._sleep makes this injectable/mockable in tests.
            sleep_task = asyncio.ensure_future(self._sleep(poll))
            trigger_task = asyncio.ensure_future(self._webhook_trigger.wait())
            done, pending = await asyncio.wait(
                {sleep_task, trigger_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            # Only consume the trigger if it actually fired; a webhook arriving
            # just as the sleep expires should not be silently discarded.
            if trigger_task in done:
                self._webhook_trigger.clear()
        else:
            # Polling disabled — block until a webhook trigger or stop event fires
            trigger_task = asyncio.ensure_future(self._webhook_trigger.wait())
            stop_task = asyncio.ensure_future(self._stop_event.wait())
            done, pending = await asyncio.wait(
                {trigger_task, stop_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            if trigger_task in done:
                self._webhook_trigger.clear()

    def stop(self) -> None:
        if self._cycle_active:
            self._stop_during_cycle = True
        self._running = False
        self._stop_event.set()
        # Cancel the in-progress cycle only when stop() is called from outside
        # start() — i.e. signal handlers or external code.  When called from
        # within start() itself (max_cycles, mid-cycle graph callbacks) the
        # existing break/stop-event logic handles the exit and we must not
        # self-cancel, which would propagate CancelledError to the caller.
        if (
            self._main_task is not None
            and not self._main_task.done()
            and asyncio.current_task() != self._main_task
        ):
            self._main_task.cancel()

    def _max_concurrent_cards(self) -> int:
        """Return the configured concurrency limit (defaults to 1)."""
        config = self._state.get("config")
        if config is not None and hasattr(config, "max_concurrent_cards"):
            return max(1, int(config.max_concurrent_cards))
        return 1

    def _blocked_sessions_to_poll(
        self, eligibilities: dict[str, SessionEligibility],
    ) -> set[str]:
        """Which BLOCKED sessions get a graph tick this cycle.

        A blocked session is ineligible for a dispatch slot, but its graph must
        still run for check_board's un-block comment poll to execute -- otherwise
        a blocked card can never be un-blocked by a fresh issue comment while any
        sibling is eligible, because the all-ineligible fallback that would run
        the poll never fires.

        Running every blocked session every cycle makes that cost grow with the
        blocked backlog, without bound. Since an un-block comment is not
        latency-sensitive, a rotating window of at most
        ``BLOCKED_POLL_MAX_PER_CYCLE`` is enough: every blocked card is still
        polled within ceil(n / BLOCKED_POLL_MAX_PER_CYCLE) cycles, and the cost
        per cycle is constant however long the backlog gets.

        Sorted, so the rotation order is deterministic rather than dependent on
        dict insertion, which is what makes "every card is polled within N
        cycles" true rather than probable.
        """
        blocked = sorted(
            card_id
            for card_id, elig in eligibilities.items()
            if not elig.eligible and elig.reason == BLOCKED_COLUMN
        )
        if not blocked:
            return set()
        if len(blocked) <= BLOCKED_POLL_MAX_PER_CYCLE:
            return set(blocked)

        start = self._blocked_poll_cursor % len(blocked)
        window = {
            blocked[(start + offset) % len(blocked)]
            for offset in range(BLOCKED_POLL_MAX_PER_CYCLE)
        }
        # Advance by the window size rather than to the window's end, so the
        # cursor stays meaningful when the backlog shrinks between cycles.
        self._blocked_poll_cursor = start + BLOCKED_POLL_MAX_PER_CYCLE
        logger.debug(
            "daemon.blocked_poll_window",
            blocked_total=len(blocked),
            polled=sorted(window),
        )
        return window

    async def _invoke_multi_session(self) -> None:
        """Process each active session through the graph concurrently.

        Called only when max_concurrent_cards > 1 and there are active
        sessions.  Before dispatching, eligible sessions are filtered from
        ineligible ones (BLOCKED column, unsatisfied dependencies).  Eligible
        sessions are fanned out with asyncio.gather so they run concurrently
        within a single cycle.  Failures are isolated per session.

        The board is polled once in a pre-flight step and cached so concurrent
        sessions don't each trigger a GitHub API call.  The main-SHA cache is
        also pre-seeded so rebase detection fires at most once per cycle.
        """
        active_sessions: dict[str, Any] = self._state.get("active_sessions") or {}
        self._state.pop("_pipeline_selected", None)

        # 048: Sync SlotManager with current sessions to free stale slots
        # from crashed/expired performers before dispatching new ones.
        slot_mgr = self._state.get("slot_manager")
        if slot_mgr is not None and hasattr(slot_mgr, "sync_from_sessions"):
            slot_mgr.sync_from_sessions(active_sessions)

        self._state["_board_cache"] = None  # type: ignore[typeddict-unknown-key]
        self._state["_main_sha_cache"] = None  # type: ignore[typeddict-unknown-key]

        if not active_sessions:
            # No sessions yet — run one graph cycle to let check_board populate them
            self._state = await self._graph.ainvoke(self._state)
            # 069: mirror flat-state mutations onto active_sessions[active_card_id]
            # so the next cycle's _derive_global_phase / slot_manager.sync_from_sessions
            # see the same view as the per-session fanout writeback (line 911).
            # Without this, a same-cycle readopt+dispatch leaves the new session at
            # phase="dispatching" while flat state["phase"]="monitoring_performer"
            # — and slot_manager then releases the slot, orphaning the implementer.
            _active_card_id = self._state.get("active_card_id")
            if _active_card_id:
                _sessions_after = self._state.get("active_sessions") or {}
                if _active_card_id in _sessions_after:
                    _sessions_after[_active_card_id] = state_to_session(self._state)
            return

        # Pre-fanout: give every session ONE shared marker map per per-cycle key.
        # `dict(self._state)` is a shallow copy, so seeding here means every session
        # mutates the same dict instead of each setdefault-ing a private one (#247).
        # 173: this used to sit after the advocate scan, which reassigned
        # self._state; that scan is gone, so the only requirement left is that
        # the seeding happens before the fanout.
        seed_shared_cycle_markers(self._state)

        # Pre-flight: poll board once so all concurrent sessions share the cache
        # and eligibility can be computed before the fanout.
        github = self._state.get("github_service")
        if github is not None:
            await _preflight_poll_board(self._state, github, active_sessions)

        await self._reconcile_board_pauses(board_is_fresh=_has_fresh_board_snapshot(self._state))

        dispatchable = {
            cid for cid, session in active_sessions.items()
            if _has_fresh_board_snapshot(self._state)
            and self._find_card_column(self._state.get("board_snapshot") or {}, cid, session) in {"IN_PROGRESS", "IN_REVIEW"}
            and not is_closed_pr_block(session.get("system_error_reason"))
        }
        await self._dispatch_documenting_side_runs(
            self._state.get("symphony_github_services") or {},
            sessions=active_sessions, dispatchable_card_ids=dispatchable,
        )

        eligibilities = _compute_session_eligibilities(
            self._state, active_sessions, self._max_concurrent_cards(),
        )

        # Fallback: if every session is ineligible this cycle (e.g. all BLOCKED /
        # dependency_blocked), run a single full graph invocation so check_board
        # can still pick up new sessions from open slots or do other per-cycle
        # maintenance.  Without this, check_board never fires and available slots
        # go unfilled until at least one existing session becomes eligible.
        if not any(e.eligible for e in eligibilities.values()):
            self._state = await _all_ineligible_fallback(
                self._state, self._graph, active_sessions,
            )
            return

        ctx = _FanoutContext(
            state=self._state,
            active_sessions=active_sessions,
            eligibilities=eligibilities,
            blocked_to_poll=self._blocked_sessions_to_poll(eligibilities),
            graph=self._graph,
            semaphore=asyncio.Semaphore(self._max_concurrent_cards()),
        )
        results: list[AsyncSessionTickResult] = list(
            await asyncio.gather(
                *[
                    _invoke_session_tick(ctx, cid, sess)
                    for cid, sess in list(active_sessions.items())
                ],
            ),
        )

        await _merge_fanout_results(self._state, active_sessions, eligibilities, results)
        _finalize_multi_session(self._state, active_sessions)

    async def _resolve_symphony_effective_state(
        self, symphony_name: str, symphony_config: Any, sym_state: Any,
    ) -> tuple[Any, dict[str, Any]]:
        """Resolve the per-symphony effective config, route any unassigned
        restored sessions onto this symphony's board, and emit the
        at-capacity debug event.  Returns (effective_cfg, sym_sessions)."""
        _global_cfg = self._state.get("config")
        _effective_cfg = (
            symphony_config.effective_config(_global_cfg)
            if hasattr(symphony_config, "effective_config") and _global_cfg is not None
            else _global_cfg
        )
        _sym_sessions = (
            (getattr(sym_state, "active_sessions", None) or {}) if sym_state is not None else {}
        )
        pending = getattr(self, "_unassigned_restored_sessions", {})
        if pending and sym_state is not None:
            service = (self._state.get("symphony_github_services") or {}).get(symphony_name)
            if service is None:
                raise RuntimeError("Cannot route restored sessions without a board service")
            board = await service.poll_board()
            snapshot = board.get("snapshot")
            if not isinstance(snapshot, dict):
                raise RuntimeError("Cannot route restored sessions from an invalid board read")
            member_ids = {str(cid) for ids in snapshot.values()
                          if isinstance(ids, list) for cid in ids}
            for cid in list(pending):
                if cid in member_ids:
                    _sym_sessions[cid] = pending.pop(cid)
            sym_state.active_sessions = dict(_sym_sessions)
        _active_sym_count = sum(
            1 for sess in _sym_sessions.values() if sess.get("phase") not in NON_SLOT_PHASES
        )
        if (
            sym_state is not None
            and _effective_cfg is not None
            and hasattr(_effective_cfg, "max_concurrent_cards")
            and _active_sym_count >= _effective_cfg.max_concurrent_cards
        ):
            logger.debug(
                "symphony.dispatch_skipped.at_capacity",
                symphony=symphony_name,
                active=_active_sym_count,
                limit=_effective_cfg.max_concurrent_cards,
            )
            # Do NOT return here — existing sessions still need to be ticked by
            # the graph. The graph respects active_sessions count and will skip
            # new dispatch naturally while still monitoring in-flight work.
        return _effective_cfg, _sym_sessions

    def _swap_symphony_state_in(
        self,
        symphony_name: str,
        sym_state: Any,
        _sym_sessions: dict[str, Any],
        _effective_cfg: Any,
    ) -> tuple[Any, Any, Any, Any, Any]:
        """Swap the per-symphony graph keys into the flat state.  Returns the
        previous values needed by _restore_symphony_swaps: (prev_config,
        prev_github, prev_workspace_manager, sym_github, sym_workspace_manager)."""
        # Swap state["config"] to the per-symphony effective config so that
        # graph nodes and _invoke_multi_session() see the symphony's limits.
        _prev_config = self._state.get("config")
        # Swap state["github_service"] to the per-symphony service so that
        # graph nodes query the correct project board for this symphony.
        _sym_github_services = self._state.get("symphony_github_services") or {}
        _prev_github = self._state.get("github_service")
        _sym_github = _sym_github_services.get(symphony_name)
        _sym_workspace_managers = self._state.get("symphony_workspace_managers") or {}
        _prev_workspace_manager = self._state.get("workspace_manager")
        _sym_workspace_manager = _sym_workspace_managers.get(symphony_name)
        # Restore per-symphony active_sessions and other graph-scoped keys so
        # the graph sees this symphony's state, not the previous symphony's.
        self._state["active_sessions"] = dict(_sym_sessions)
        if sym_state is not None:
            _sym_card = sym_state.active_card
            _sym_card_id = str(_sym_card.get("id", "")) if isinstance(_sym_card, dict) else ""
            self._state["active_card_id"] = _sym_card_id or None
            _rederive_current_card(self._state)
            if sym_state.board_snapshot is not None:
                self._state["board_snapshot"] = sym_state.board_snapshot
            if sym_state.session_skip_reasons is not None:
                self._state["session_skip_reasons"] = sym_state.session_skip_reasons
            if sym_state.previous_phase is not None:
                self._state["phase"] = sym_state.previous_phase
        if _effective_cfg is not None and _effective_cfg is not _prev_config:
            self._state["config"] = _effective_cfg
        if _sym_github is not None:
            self._state["github_service"] = _sym_github
            # 149: the board follows the symphony's service. Leaving it behind
            # would have a multi-symphony run reading one board and writing
            # another — silently, since both are GitHub today.
            self._state["board_provider"] = GitHubProjectsBoardProvider(_sym_github)
        if _sym_workspace_manager is not None:
            self._state["workspace_manager"] = _sym_workspace_manager
        return (
            _prev_config,
            _prev_github,
            _prev_workspace_manager,
            _sym_github,
            _sym_workspace_manager,
        )

    def _restore_symphony_swaps(
        self,
        symphony_name: str,
        prev_config: Any,
        prev_github: Any,
        prev_workspace_manager: Any,
        sym_github: Any,
        sym_workspace_manager: Any,
    ) -> None:
        """Inner-finally restore of the per-symphony graph-key swaps."""
        self._state["config"] = prev_config
        if sym_github is not None:
            self._state["github_service"] = prev_github
            self._state["board_provider"] = GitHubProjectsBoardProvider(prev_github)
        if sym_workspace_manager is not None:
            self._state["workspace_manager"] = prev_workspace_manager

    def _update_symphony_state(self, symphony_name: str, sym_state: Any) -> None:
        """Mirror the finished cycle's results back onto the symphony state."""
        sym_state.cycle_count = getattr(sym_state, "cycle_count", 0) + 1
        sym_state.last_poll_at = datetime.now(UTC)
        board_snap = self._state.get("board_snapshot")
        if board_snap is not None:
            sym_state.board_snapshot = board_snap
        sym_state.active_sessions = dict(self._state.get("active_sessions") or {})
        sym_state.active_card = self._state.get("current_card")
        skip_reasons = self._state.get("session_skip_reasons")
        sym_state.session_skip_reasons = dict(skip_reasons) if skip_reasons else None
        # 062: Per-card metadata for the dashboard swimlane.
        sym_state.board_titles = dict(self._state.get("_board_titles") or {})
        sym_state.board_issue_numbers = dict(self._state.get("_board_issue_numbers") or {})
        sym_state.board_issue_urls = dict(self._state.get("_board_issue_urls") or {})
        sym_state.board_pr_urls = dict(self._state.get("_board_pr_urls") or {})
        # Per-symphony phase transition metric (labels each transition with the actual symphony)
        _prev_sym_phase = sym_state.previous_phase
        _cur_sym_phase = self._state.get("phase")
        if _cur_sym_phase != _prev_sym_phase:
            _sym_transition = _PHASE_TRANSITION_METRIC.get(
                (str(_prev_sym_phase), str(_cur_sym_phase)),
            )
            if _sym_transition is not None:
                METRICS.card_state_transitions_total.labels(
                    symphony=symphony_name,
                    transition_type=_sym_transition,
                ).inc()
            sym_state.previous_phase = _cur_sym_phase

    async def _conduct_single_symphony(
        self,
        symphony_name: str,
        symphony_config: Any,  # SymphonyConfig
    ) -> None:
        """Run one orchestration cycle for a single symphony (spec 057)."""
        from coordinare.observability import bind_symphony, clear_symphony

        bind_symphony(symphony_name)
        self._state["current_symphony"] = symphony_name
        _propagating = False
        # Save symphony-scoped graph keys before entering the try so the outer
        # finally can always restore them (prevents cross-symphony contamination).
        _prev_current_card = self._state.get("current_card")
        _prev_active_card_id = self._state.get("active_card_id")
        _prev_active_sessions = self._state.get("active_sessions")
        _prev_board_snapshot = self._state.get("board_snapshot")
        _prev_session_skip_reasons = self._state.get("session_skip_reasons")
        _prev_phase = self._state.get("phase")
        try:
            # Check max_concurrent_cards limit per symphony
            symphony_states = self._state.get("symphony_states") or {}
            sym_state = symphony_states.get(symphony_name)
            _effective_cfg, _sym_sessions = await self._resolve_symphony_effective_state(
                symphony_name, symphony_config, sym_state,
            )

            (
                _prev_config,
                _prev_github,
                _prev_workspace_manager,
                _sym_github,
                _sym_workspace_manager,
            ) = self._swap_symphony_state_in(
                symphony_name, sym_state, _sym_sessions, _effective_cfg,
            )
            try:
                # 066 T019/FR-004: _invoke_multi_session is the sole graph entry
                # path for both N=1 and N>1.  Empty-sessions case short-circuits
                # to a single graph cycle inside _invoke_multi_session.
                await self._invoke_multi_session()
            finally:
                self._restore_symphony_swaps(
                    symphony_name,
                    _prev_config,
                    _prev_github,
                    _prev_workspace_manager,
                    _sym_github,
                    _sym_workspace_manager,
                )

            # Update symphony state on success
            # Issue #516: reconcile BEFORE the mirror — a card the board
            # moved to DONE must have its session retired here, inside the
            # symphony cycle, or _update_symphony_state mirrors the zombie
            # back onto sym_state and the next cycle re-seeds it (still
            # holding the pickup slot).
            if sym_state is not None:
                await self._reconcile_board_state_with_release()
                self._update_symphony_state(symphony_name, sym_state)
        except (asyncio.CancelledError, CircuitOpenError):
            _propagating = True
            # Rebuild aggregate active_sessions from last-known-good symphony states
            # so state is not left in a per-symphony scoped view on abnormal exit.
            _agg: dict[str, dict[str, Any]] = {}
            for _ss in (self._state.get("symphony_states") or {}).values():
                _agg.update(getattr(_ss, "active_sessions", None) or {})
            self._state["active_sessions"] = _agg
            raise
        except Exception as exc:
            logger.error(
                "symphony.cycle_error",
                symphony=symphony_name,
                error=str(exc),
                exc_info=True,
            )
            sym_states = self._state.get("symphony_states") or {}
            s = sym_states.get(symphony_name)
            if s is not None:
                s.error_count = getattr(s, "error_count", 0) + 1
                s.last_error = str(exc)
            # Do NOT re-raise — other symphonies continue
        finally:
            clear_symphony()
            if not _propagating:
                self._state["current_symphony"] = None
            # Restore symphony-scoped graph keys so the next symphony starts clean.
            if _prev_active_sessions is not None:
                self._state["active_sessions"] = _prev_active_sessions
            self._state["active_card_id"] = _prev_active_card_id
            _rederive_current_card(self._state)
            self._state["board_snapshot"] = _prev_board_snapshot  # type: ignore[typeddict-item]
            self._state["session_skip_reasons"] = _prev_session_skip_reasons  # type: ignore[typeddict-item]
            self._state["phase"] = _prev_phase  # type: ignore[typeddict-item]

    def _preflight_reload_symphony_states(
        self, added: set[str], removed: set[str],
    ) -> dict[str, Any] | None:
        """Preflight: check active sessions BEFORE mutating any daemon state so
        an aborted reload cannot leave symphony_configs/config/config_version
        out of sync with the still-running symphony_states.  Returns the
        updated symphony-state map, or None when the reload must abort."""
        from coordinare.graph.state import SymphonyRuntimeState

        sym_states = dict(self._state.get("symphony_states") or {})
        for name in added:
            sym_states[name] = SymphonyRuntimeState(name=name)
        for name in removed:
            sym_state = sym_states.get(name)
            if sym_state is not None and getattr(sym_state, "active_sessions", None):
                logger.error(
                    "config_reload.aborted_active_sessions",
                    symphony=name,
                    active_sessions=list(sym_state.active_sessions.keys()),
                    reason="Reload would orphan in-flight sessions; retry once sessions complete",
                )
                return None
            sym_states.pop(name, None)
        return sym_states

    async def _rebuild_symphony_github_services(
        self, new_configs: dict[str, Any], coordinare_cfg: Any,
    ) -> dict[str, Any] | None:
        """Rebuild the full per-symphony GitHubService map on every successful
        reload so that changed project numbers or GitHub settings in existing
        symphonies are reflected, not just added/removed symphonies.
        Build new services first; only swap (and close old) once all are ready
        so a failed initialize() leaves the daemon in a consistent state.
        Returns the new service map, or None when there is no global service."""
        import contextlib

        from coordinare.auth import build_auth as _build_auth
        from coordinare.observability import bind_symphony, clear_symphony
        from coordinare.services.github import GitHubService as _GHSvc

        _global_gh = self._state.get("github_service")
        if _global_gh is None:
            return None
        _sym_svcs: dict[str, Any] = {}
        try:
            for _sym_name, _sym_cfg in new_configs.items():
                _new_eff = _sym_cfg.effective_config(coordinare_cfg.global_config)
                _r = _new_eff.resilience.github_retry
                _new_svc = _GHSvc(
                    auth=_build_auth(_new_eff),
                    org=_new_eff.github_org,
                    project_number=_new_eff.github_project_number,
                    endpoint=_new_eff.github_graphql_url,
                    circuit_breaker=cast("Any", _global_gh)._circuit_breaker,
                    retry_kwargs={
                        "attempts": _r.attempts,
                        "wait_initial": _r.wait_initial_seconds,
                        "wait_max": _r.wait_max_seconds,
                        "wait_jitter": _r.wait_jitter_seconds,
                        "wait_exp_base": _r.wait_exp_base,
                    },
                )
                _new_svc._project_name = _new_eff.project_name
                bind_symphony(_sym_name)
                try:
                    await _new_svc.initialize()
                finally:
                    clear_symphony()
                _sym_svcs[_sym_name] = _new_svc
        except Exception:
            # Initialization failed — close any partially-built services
            # and re-raise so the outer handler logs and keeps old state.
            for _partial in _sym_svcs.values():
                if hasattr(_partial, "aclose"):
                    with contextlib.suppress(Exception):
                        await _partial.aclose()
            raise

        # All new services ready — close old ones then swap atomically.
        _old_svcs: dict[str, Any] = self._state.get("symphony_github_services") or {}
        for _old_svc in _old_svcs.values():
            if hasattr(_old_svc, "aclose"):
                with contextlib.suppress(Exception):
                    await _old_svc.aclose()
        self._state["symphony_github_services"] = _sym_svcs
        return _sym_svcs

    def _rebuild_symphony_workspace_managers(
        self, new_configs: dict[str, Any], coordinare_cfg: Any, sym_svcs: dict[str, Any],
    ) -> None:
        """Rebuild per-symphony WorkspaceManager instances for the new config."""
        from coordinare.auth import build_auth as _build_auth
        from coordinare.workspace import WorkspaceManager as _WorkspaceManager

        _sym_wms: dict[str, Any] = {}
        for _sym_name2, _sym_cfg2 in new_configs.items():
            _wm_eff = _sym_cfg2.effective_config(coordinare_cfg.global_config)
            _sym_wms[_sym_name2] = _WorkspaceManager(
                _wm_eff,
                auth=_build_auth(_wm_eff),
                github_service=sym_svcs.get(_sym_name2),
            )
        self._state["symphony_workspace_managers"] = _sym_wms

    async def _handle_config_reload(self) -> None:
        """Reload configuration from disk and update symphony state (spec 057)."""
        config_path = self._state.get("config_path")
        if config_path is None:
            logger.warning("config_reload.no_path")
            return
        try:
            from coordinare.config import CoordinareConfiguration
            from coordinare.config_validation import (
                _load_raw_yaml,
                coerce_multi_symphony_raw,
                is_multi_symphony_config,
                validate_config,
                wrap_legacy_config,
            )

            validation = validate_config(config_path)
            if not validation.passed:
                errs = "; ".join(e.fix_hint for e in validation.errors)
                logger.error("config_reload.validation_failed", errors=errs)
                return

            raw = _load_raw_yaml(config_path)

            if is_multi_symphony_config(raw):
                coordinare_cfg = CoordinareConfiguration(**coerce_multi_symphony_raw(raw))
            else:
                wrapped = wrap_legacy_config(raw)
                coordinare_cfg = CoordinareConfiguration(**wrapped)

            old_names = set(self._state.get("symphony_configs") or {})
            new_configs = {s.name: s for s in coordinare_cfg.symphonies}
            new_names = set(new_configs)

            added = new_names - old_names
            removed = old_names - new_names

            sym_states = self._preflight_reload_symphony_states(added, removed)
            if sym_states is None:
                return

            _sym_svcs = await self._rebuild_symphony_github_services(new_configs, coordinare_cfg)
            if _sym_svcs is not None:
                # Rebuild per-symphony WorkspaceManager instances for the new config.
                self._rebuild_symphony_workspace_managers(
                    new_configs, coordinare_cfg, _sym_svcs,
                )

            # Atomic state swap: all config fields updated only after all
            # preflights and service builds have succeeded without raising.
            self._state["symphony_configs"] = new_configs
            self._state["coordinare_config"] = coordinare_cfg
            self._state["config"] = coordinare_cfg.global_config
            self._state["orchestra_config"] = coordinare_cfg.orchestra
            self._state["symphony_states"] = sym_states
            self._state["config_mode"] = (
                "multi_symphony" if is_multi_symphony_config(raw) else "legacy"
            )
            self._state["config_version"] = (self._state.get("config_version") or 0) + 1

            logger.info("config_reloaded", added=list(added), removed=list(removed))
        except Exception as exc:
            logger.error("config_reload.failed", error=str(exc), exc_info=True)

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.stop)
            except NotImplementedError:
                return

    def _emit(self, **event: Any) -> None:
        category = event.get("category", "activity")
        if category == "failure":
            logger.error("runtime_event", **event)
        elif category in {"heartbeat", "activity"}:
            logger.debug("runtime_event", **event)
        else:
            logger.info("runtime_event", **event)

    def _get_manifest_llm_chat(self) -> Any:
        """077: build (once) a JSON-chat callable for the env-manifest README
        pass, from the coordinare's conducting-brain config. Returns None when the
        brain isn't an OpenAI-compatible backend (→ deterministic-only manifest).
        """
        sentinel = object()
        cached = getattr(self, "_manifest_llm_chat", sentinel)
        if cached is not sentinel:
            return cached
        cfg = self._state.get("config")
        cc = getattr(cfg, "conducting", None) if cfg is not None else None
        if cc is None:
            return None  # config not ready yet — don't memoize a premature None
        chat = None
        if getattr(cc, "backend", None) == "openai_api":
            import os

            from coordinare.services.conducting import OpenAiApiBackend

            backend = OpenAiApiBackend(
                api_key=os.getenv(cc.api_key_env or "OPENAI_API_KEY"),
                model=cc.model or "gpt-4o-mini",
                max_tokens=cc.max_tokens,
                temperature=cc.temperature,
                base_url=cc.base_url,
                effort=cc.effort,
            )
            chat = backend.chat_json
        self._manifest_llm_chat = chat
        return chat

    async def _verify_env_cache_clean(
        self, symphony_name: str, svc: Any,
    ) -> tuple[bool | None, str]:
        """077: run the cache's ``verify.sh`` in a CLEAN consumer-context
        container — the performer image with ONLY the cache mounted read-only at
        the same path consumers use — so a broken consumer-facing ``activate.sh``
        can't false-pass via installs the bootstrap container did online.

        verify.sh sources activate.sh and asserts every dependency is runnable
        (it installs from the cache's local debs, no network), so this is a
        faithful "does the cache alone provide a working toolchain" check.

        Returns ``(passed, detail)``: ``passed`` is True/False when verify.sh
        ran, or None when it can't be run (absent script / docker error) — None
        is degraded and does NOT downgrade success, to avoid looping on infra
        errors. ``detail`` is a concise human-readable reason (the verify FAIL
        lines) for the dashboard.
        """
        from coordinare.services.env_cache import verify_env_cache_clean

        return await verify_env_cache_clean(self._state, symphony_name, svc)

    async def _poll_bootstrap_completion(
        self,
        svc: Any,
        job_id: str,
        symphony_name: str,
        env_cache_svc: Any,
        container_id: str | None = None,
    ) -> None:
        """Poll a bootstrap job to completion and fire on_bootstrap_complete.

        Runs for at most ``bootstrap_max_seconds`` (config; default 1 h, polled
        every 10 s).  Past that ceiling the bootstrap is declared failed AND its
        container reaped, so a slow/hung bootstrap can't gate the symphony for
        the legacy ~2 h.  ``bootstrap_max_seconds=0`` restores the legacy cap.
        """
        # 076 (live QA #150): resolve the wall-clock budget + idle timeout.
        _cfg = self._state.get("coordinare_config")
        _budget_s = int(getattr(_cfg, "bootstrap_max_seconds", 0) or 0)
        _idle_timeout_s = int(getattr(_cfg, "bootstrap_idle_timeout_seconds", 0) or 0)
        max_attempts = (_budget_s // 10) if _budget_s > 0 else _BOOTSTRAP_POLL_MAX_ATTEMPTS
        if max_attempts < 1:
            max_attempts = 1
        poll = _BootstrapPollState()
        for _attempt in range(max_attempts):
            await asyncio.sleep(10)
            poll.last_logs_snapshot = await _snapshot_container_logs(
                container_id, poll.last_logs_snapshot,
            )
            # 076 idle reap: if the bootstrap's meaningful log output hasn't
            # changed for ``bootstrap_idle_timeout_seconds``, it's hung (not just
            # slow) — reap it now rather than waiting out the wall-clock budget.
            if (
                container_id
                and _idle_timeout_s > 0
                and await self._bootstrap_idle_reap(
                    poll,
                    _attempt,
                    container_id,
                    symphony_name,
                    job_id,
                    env_cache_svc,
                    _idle_timeout_s,
                )
            ):
                return
            try:
                status_result = await svc.check_status(job_id)
            except Exception as exc:
                # A single status poll can fail transiently while the container
                # is still alive and building (notably a 30 s HTTP timeout under
                # host load). Treat such failures as non-terminal up to
                # _BOOTSTRAP_POLL_MAX_CONSECUTIVE_FAILURES consecutive misses;
                # only then declare the bootstrap failed. The counter resets the
                # moment any poll succeeds.
                poll.consecutive_failures += 1
                if poll.consecutive_failures < _BOOTSTRAP_POLL_MAX_CONSECUTIVE_FAILURES:
                    logger.warning(
                        "env_cache.bootstrap_poll_transient",
                        symphony=symphony_name,
                        error=str(exc),
                        consecutive_failures=poll.consecutive_failures,
                        max_consecutive_failures=_BOOTSTRAP_POLL_MAX_CONSECUTIVE_FAILURES,
                    )
                    continue
                logger.warning(
                    "env_cache.bootstrap_poll_failed",
                    symphony=symphony_name,
                    error=str(exc),
                    consecutive_failures=poll.consecutive_failures,
                )
                env_cache_svc.on_bootstrap_complete(
                    symphony_name,
                    False,
                    self._state,
                    error=f"bootstrap polling failed: {exc}",
                )
                return
            # A successful poll clears the transient-failure streak.
            poll.consecutive_failures = 0
            if status_result.get("status") not in ("working", None):
                await self._handle_bootstrap_terminal_status(
                    svc, job_id, symphony_name, env_cache_svc, container_id,
                    status_result, poll.last_logs_snapshot,
                )
                return
        await self._bootstrap_budget_exhausted(
            symphony_name, env_cache_svc, container_id, _budget_s, max_attempts,
        )

    async def _bootstrap_budget_exhausted(
        self,
        symphony_name: str,
        env_cache_svc: Any,
        container_id: str | None,
        budget_s: int,
        max_attempts: int,
    ) -> None:
        """076 (live QA #150): budget exhausted.  Reap the container explicitly —
        it was started with ``--rm`` but a hung agent never exits, so without
        an explicit stop it would linger and keep holding a backend slot."""
        logger.warning(
            "env_cache.bootstrap_poll_timeout",
            symphony=symphony_name,
            budget_seconds=budget_s or (_BOOTSTRAP_POLL_MAX_ATTEMPTS * 10),
            attempts=max_attempts,
        )
        if container_id:
            await self._reap_bootstrap_container(container_id, symphony_name)
        env_cache_svc.on_bootstrap_complete(
            symphony_name,
            False,
            self._state,
            error=(
                "bootstrap exceeded its time budget "
                f"({budget_s or _BOOTSTRAP_POLL_MAX_ATTEMPTS * 10}s) and was reaped"
            ),
        )

    async def _verify_bootstrap_in_clean_context(
        self, symphony_name: str, svc: Any, job_id: str,
    ) -> tuple[bool, str | None]:
        """077 authoritative clean-context verify: re-run verify.sh in a clean
        consumer container; a non-zero result downgrades success.  Returns the
        (ok, boot_err) pair to record."""
        _clean_ok, _clean_detail = await self._verify_env_cache_clean(
            symphony_name, svc,
        )
        if _clean_ok is False:
            logger.warning(
                "env_cache.clean_verify_failed",
                symphony=symphony_name,
                job_id=job_id,
                detail=_clean_detail,
            )
            return False, (
                f"env verification failed in a clean consumer context: {_clean_detail}"
            )
        return True, None

    async def _bootstrap_idle_reap(
        self,
        poll: _BootstrapPollState,
        attempt: int,
        container_id: str,
        symphony_name: str,
        job_id: str,
        env_cache_svc: Any,
        idle_timeout_s: int,
    ) -> bool:
        """076 idle reap: reap a hung bootstrap (no meaningful log progress for
        ``bootstrap_idle_timeout_seconds``).  Returns True when reaped."""
        _progress = _bootstrap_progress_lines(poll.last_logs_snapshot)
        if _progress != poll.last_progress:
            poll.last_progress = _progress
            poll.last_progress_attempt = attempt
            return False
        if (attempt - poll.last_progress_attempt) * 10 < idle_timeout_s:
            return False
        logger.warning(
            "env_cache.bootstrap_idle_reaped",
            symphony=symphony_name,
            job_id=job_id,
            idle_seconds=(attempt - poll.last_progress_attempt) * 10,
            idle_timeout_seconds=idle_timeout_s,
        )
        await self._reap_bootstrap_container(container_id, symphony_name)
        env_cache_svc.on_bootstrap_complete(
            symphony_name,
            False,
            self._state,
            error=(
                "bootstrap hung — no progress for "
                f"{(attempt - poll.last_progress_attempt) * 10}s; reaped"
            ),
        )
        return True

    async def _handle_bootstrap_terminal_status(
        self,
        svc: Any,
        job_id: str,
        symphony_name: str,
        env_cache_svc: Any,
        container_id: str | None,
        status_result: dict[str, Any],
        last_logs_snapshot: list[str],
    ) -> None:
        """Handle a terminal bootstrap status (success or failure)."""
        # 060/Option A: performer reports a terminal status when the
        # bootstrap session ends. "env_bootstrap_complete" is the
        # success marker emitted by the performer's env_bootstrap
        # role; "ok" is retained for backwards compat with the
        # earlier coordinare-driven contract. Anything else is failure.
        ok = status_result.get("status") in ("env_bootstrap_complete", "ok")
        if not ok:
            logs_tail = await _bootstrap_failure_logs(
                svc, job_id, container_id, last_logs_snapshot,
            )
            logger.warning(
                "env_cache.bootstrap_terminal_failure",
                symphony=symphony_name,
                job_id=job_id,
                status=status_result.get("status"),
                availability=status_result.get("availability"),
                error=status_result.get("error")
                or status_result.get("message")
                or status_result.get("reason"),
                status_keys=sorted(status_result.keys())
                if isinstance(status_result, dict)
                else None,
                logs_tail=logs_tail[-10:],
                logs_tail_truncated=len(logs_tail) > 10,
            )
        # 063 T026d: stamp the performer-reported service-inference
        # summary onto the env-cache state before marking the
        # bootstrap complete, so the dashboard surfaces what the
        # agent produced (or why it didn't run).
        try:
            env_cache_svc.record_inference_outcome(
                symphony_name,
                self._state,
                skipped_reason=status_result.get("inference_skipped_reason"),
                agent_version=status_result.get("inference_agent_version"),
                attempts=status_result.get("inference_attempts"),
                succeeded=status_result.get("inference_succeeded"),
                services=list(status_result.get("inference_services") or []),
                test_env_source=status_result.get("inference_test_env_source"),
            )
        except Exception as _exc:
            logger.warning(
                "env_cache.record_inference_outcome_error",
                symphony=symphony_name,
                error=str(_exc),
            )
        # 077: authoritative clean-context verify. The performer ran
        # verify.sh INSIDE its own bootstrap container, where the agent's
        # ad-hoc online installs can mask a broken consumer-facing
        # activate.sh (false pass — observed: chromium present in the
        # bootstrap container via online apt, but the activate.sh debs
        # path didn't actually yield a working chromium for consumers).
        # Re-run verify.sh in a CLEAN container (image + cache mounted
        # read-only = a consumer's exact world); only that result is
        # authoritative. A non-zero clean verify downgrades success so
        # on_bootstrap_complete(False) clears readme_sha and retries —
        # a broken cache is never marked ready.
        _boot_err: str | None = None
        if not ok:
            _boot_err = (
                status_result.get("error")
                or status_result.get("message")
                or status_result.get("reason")
                or "bootstrap performer reported a terminal failure"
            )
        if ok:
            ok, _boot_err = await self._verify_bootstrap_in_clean_context(
                symphony_name, svc, job_id,
            )
        env_cache_svc.on_bootstrap_complete(symphony_name, ok, self._state, error=_boot_err)
        if ok:
            from coordinare.services.http_performer_service import HTTPPerformerService

            _performer_svcs = self._state.get("performer_services") or {}
            for _pid, _psvc in _performer_svcs.items():
                if isinstance(_psvc, HTTPPerformerService) and _psvc.mode == "persistent":
                    logger.warning(
                        "env_cache.persistent_mount_skipped",
                        symphony=symphony_name,
                        performer_id=_pid,
                        detail=(
                            "Env cache became ready after persistent performer started; "
                            "restart the performer container to pick up the new mount."
                        ),
                    )

    async def _reap_bootstrap_container(self, container_id: str, symphony_name: str) -> None:
        """076 (live QA #150): force-stop a wedged env_bootstrap container.

        Shared by the wall-clock-budget and idle-reap paths.  Best-effort: a
        failure to stop must not wedge the symphony (the caller still marks the
        bootstrap failed so dispatch unblocks and the next cycle re-dispatches).
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker",
                "stop",
                "--time",
                "5",
                container_id,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=20.0)
            logger.warning(
                "env_cache.bootstrap_container_reaped",
                symphony=symphony_name,
                container_id=container_id[:12],
            )
        except Exception as exc:
            logger.warning(
                "env_cache.bootstrap_reap_failed",
                symphony=symphony_name,
                container_id=container_id[:12],
                error=str(exc),
            )

    async def _dispatch_documenting_side_runs(
        self, sym_gh_svcs: dict[str, Any] | None, *,
        sessions: dict[str, Any] | None = None, dispatchable_card_ids: set[str] | None = None,
    ) -> None:
        """165 (FR-014/FR-015): run the documenter beside the lifecycle.

        One dispatch per blueprint hash, only for sessions past architecting
        whose blueprint carries documentation topics; polled to a recorded
        outcome on the session (persisted as ``documenting_side``). Never
        blocks or advances the main lifecycle.

        Existing runs are always eligible for polling. New dispatches require
        the card allowlist derived from this board's fresh preflight read.
        """
        from coordinare.services import documenting_side as _ds

        svc = (self._state.get("performer_services") or {}).get("documenting")
        def current_sessions() -> dict[str, Any]:
            current = dict(self._state.get("active_sessions") or {})
            for runtime in (self._state.get("symphony_states") or {}).values():
                current.update(getattr(runtime, "active_sessions", None) or {})
            return current

        sessions = current_sessions() if sessions is None else sessions
        if svc is None or not sessions:
            return

        async def _resolve(card_id: str, session: dict[str, Any]) -> tuple[dict[str, Any], Any] | None:
            if card_id not in (dispatchable_card_ids or set()):
                return None
            return await self._resolve_documenting_side(card_id, session, sym_gh_svcs or {})

        async def _recover(card_id: str, session: dict[str, Any]) -> bool:
            return await recover_documenting_side_session(card_id, session, svc)

        def _spawn(coro: Any) -> None:
            task = asyncio.create_task(coro)
            self._documenting_side_tasks.add(task)
            task.add_done_callback(self._documenting_side_tasks.discard)

        try:
            if not hasattr(self, "_documenting_side_polling"):
                self._documenting_side_polling: set[str] = set()
            await _ds.run_cycle(sessions, svc=svc, resolve=_resolve, spawn=_spawn,
                                get_session=lambda cid: current_sessions().get(cid),
                                polling=self._documenting_side_polling, recover=_recover)
        except Exception as exc:
            logger.warning("documenting_side.cycle_failed", error=str(exc)[:200])

    async def _resolve_documenting_side(
        self, card_id: str, session: dict[str, Any], sym_gh_svcs: dict[str, Any],
    ) -> tuple[dict[str, Any], Any] | None:
        """Turn a session into the documenter's dispatch payload, or None when
        the repo, token or branch cannot be resolved (retried next cycle)."""
        from coordinare.services import documenting_side as _ds
        from coordinare.services.persona_service import get_effective_instructions
        from coordinare.workspace import WorkspaceInfo

        card = session.get("current_card") or {}
        symphony = str(session.get("current_symphony") or card.get("symphony") or self._state.get("current_symphony") or "")
        github = sym_gh_svcs.get(symphony) if symphony else None
        if github is None:
            github = self._state.get("github_service")
        org = getattr(github, "org", None) or getattr(github, "_org", "") or ""
        repo = getattr(github, "_project_name", "") or ""
        branch = str(session.get("workspace_branch") or card.get("branch") or "")
        if not (org and repo and branch):
            logger.info("documenting_side.unresolved", card_id=card_id, org=bool(org), repo=bool(repo), branch=bool(branch))
            return None
        token = ""
        sym_wm = (self._state.get("symphony_workspace_managers") or {}).get(symphony)
        if sym_wm is not None and hasattr(sym_wm, "get_fresh_github_token"):
            try:
                token = await sym_wm.get_fresh_github_token() or ""
            except Exception as exc:
                logger.warning("documenting_side.token_fetch_failed", card_id=card_id, error=str(exc)[:200])
        if not token:
            import os

            token = os.environ.get("GITHUB_TOKEN", "")
        if not token:
            return None
        cfg = self._state.get("config")
        sym_cfg = (self._state.get("symphony_configs") or {}).get(symphony)
        coordinare_cfg = self._state.get("coordinare_config")
        global_cfg = getattr(coordinare_cfg, "global_config", None) or cfg
        if sym_cfg is not None and hasattr(sym_cfg, "effective_config") and global_cfg is not None:
            cfg = sym_cfg.effective_config(global_cfg)
        persona, backend, model_block = "", "codex", {}
        workflow_fields: dict[str, Any] = {}
        if cfg is not None:
            try:
                persona = get_effective_instructions("tech_writer", cfg.personas)
            except Exception:
                persona = ""
            rc = cfg.performers.resolved_role("tech_writer") if hasattr(cfg, "performers") else None
            if rc is not None and getattr(rc, "backend", None):
                backend = rc.backend
            if rc is not None:
                from coordinare.services.performer_tuning import translate_tuning
                workflow_fields.update(translate_tuning(rc))
                if getattr(rc, "workflow", None):
                    workflow_fields["workflow"] = rc.workflow
                if getattr(rc, "workflow_env", None):
                    workflow_fields["workflow_env"] = dict(rc.workflow_env)
            if hasattr(cfg, "resolve_performer_orchestration"):
                orchestration = cfg.resolve_performer_orchestration("tech_writer")
                if orchestration is not None:
                    workflow_fields["orchestration"] = orchestration
            for key in ("github_api_url", "github_graphql_url"):
                value = getattr(cfg, key, None)
                if value:
                    workflow_fields[key] = value
            try:
                model_block = cfg.resolve_performer_dispatch_model("tech_writer")
            except Exception:
                model_block = {}
        git_base = getattr(sym_wm, "_performer_git_base_url", None) or getattr(cfg, "performer_git_base_url", None) or getattr(cfg, "git_base_url", "https://github.com")
        repo_url = f"{str(git_base).rstrip('/')}/{org}/{repo}.git"
        ctx = _ds.build_card_context(
            card, session, persona=persona, backend=backend, model_block=model_block,
            repo_url=repo_url, base_branch=str(card.get("base_branch") or getattr(cfg, "base_branch", None) or "main"),
        )
        ctx.update(workflow_fields)
        workspace_info = WorkspaceInfo(path=None, branch=branch, repo_url=repo_url, github_token=token)
        return ctx, workspace_info

    async def _resolve_cardless_github_token(
        self, symphony_name: str, event_prefix: str,
    ) -> str:
        """GITHUB_TOKEN provisioning for cardless dispatches: they never flow
        through WorkspaceManager.prepare(), so fetch a fresh credential from
        the symphony's workspace manager (App installation token or configured
        PAT) — the same source the env_bootstrap dispatch uses — then fall back
        to the process env.  Returns "" when neither source has a token."""
        sym_wm = (self._state.get("symphony_workspace_managers") or {}).get(symphony_name)
        token = ""
        if sym_wm is not None and hasattr(sym_wm, "get_fresh_github_token"):
            try:
                token = await sym_wm.get_fresh_github_token() or ""
            except Exception as exc:
                logger.warning(
                    f"{event_prefix}.token_fetch_failed", symphony=symphony_name, error=str(exc),
                )
        if not token:
            import os

            token = os.environ.get("GITHUB_TOKEN", "")
        return token

    def _build_intake_payload(
        self,
        role: str,
        symphony_name: str,
        cfg: Any,
        role_cfg: Any,
        github: Any,
        org: str,
        repo: str,
        token: str,
    ) -> tuple[dict[str, Any], Any]:
        """Build the cardless intake card_context + WorkspaceInfo (spec 173)."""
        from coordinare.services.intake_dispatch import (
            build_card_context,
            build_workflow_env,
        )
        from coordinare.services.persona_service import get_effective_instructions
        from coordinare.workspace import WorkspaceInfo

        try:
            persona = get_effective_instructions(role, cfg.personas)
        except Exception:
            persona = ""
        backend = str(getattr(role_cfg, "backend", "") or "hermes")
        try:
            model_block = cfg.resolve_performer_dispatch_model(role)
        except Exception:
            model_block = {}

        card_context = build_card_context(
            role,  # type: ignore[arg-type]
            symphony_name=symphony_name,
            org=org,
            repo=repo,
            persona=persona,
            backend=backend,
            model_block=model_block,
            project_id=str(getattr(github, "project_id", "") or ""),
            workflow_env=build_workflow_env(role, role_cfg),  # type: ignore[arg-type]
        )
        workspace_info = WorkspaceInfo(
            path=None,
            branch=card_context["branch"],
            repo_url=card_context["repo_url"],
            github_token=token,
        )
        return card_context, workspace_info

    async def _maybe_dispatch_intake(self, role: IntakeRole, symphony_name: str, github: Any) -> None:
        """Start one card-less intake run when the gate allows it (spec 173).

        Coordinare's whole job for these two roles: decide, dispatch, record. It
        classifies nothing and posts nothing itself.
        """
        from coordinare.services.intake_dispatch import (
            register_failure,
            should_run,
        )

        cfg = self._state.get("config")
        if cfg is None:
            return
        role_cfg = getattr(cfg, role, None)
        if role_cfg is None or not getattr(role_cfg, "enabled", False):
            return
        ec = (self._state.get("env_cache") or {}).get(symphony_name)
        if ec is None:
            return
        if not should_run(
            ec, role,
            enabled=True,
            interval_seconds=int(getattr(role_cfg, "scan_interval_seconds", 900)),
        ):
            return

        svc = (self._state.get("performer_services") or {}).get("assessing")
        if svc is None:
            logger.warning("intake.no_performer_service", role=role, symphony=symphony_name)
            return

        org = str(getattr(cfg, "github_org", "") or "")
        repo = str(getattr(role_cfg, "github_repo", "") or "")
        if not (org and repo):
            logger.warning("intake.repo_unknown", role=role, symphony=symphony_name)
            return

        token = await self._resolve_cardless_github_token(symphony_name, "intake")
        if not token:
            logger.warning("intake.no_github_token", role=role, symphony=symphony_name)
            return

        card_context, workspace_info = self._build_intake_payload(
            role, symphony_name, cfg, role_cfg, github, org, repo, token,
        )

        # The marker goes up BEFORE the dispatch and comes down on a synchronous
        # failure. Setting it afterwards clobbers the reset that failure performs
        # and wedges the role until the process restarts.
        setattr(ec, f"{role}_in_flight", True)
        try:
            result = await svc.dispatch_card(card_context, workspace_info=workspace_info)
        except Exception as exc:
            setattr(ec, f"{role}_in_flight", False)
            register_failure(ec, role, f"dispatch failed: {exc}", max_attempts=3)
            logger.warning("intake.dispatch_failed", role=role, error=str(exc))
            return

        session_id = (result or {}).get("session_id") or (result or {}).get("job_id")
        if not session_id:
            setattr(ec, f"{role}_in_flight", False)
            register_failure(ec, role, "dispatch returned no session id", max_attempts=3)
            return

        logger.info("intake.dispatched", role=role, symphony=symphony_name, session_id=session_id)
        task = asyncio.create_task(
            self._poll_intake_completion(role, symphony_name, svc, session_id),
        )
        self._intake_poll_tasks.add(task)
        task.add_done_callback(self._intake_poll_tasks.discard)

    async def _poll_intake_completion(
        self, role: str, symphony_name: str, svc: Any, session_id: str,
    ) -> None:
        """Poll one intake run to terminal and record what it did (spec 173)."""
        from coordinare.services.intake_dispatch import TERMINAL_BY_ROLE, handle_run_result

        ec = (self._state.get("env_cache") or {}).get(symphony_name)
        if ec is None:
            return
        terminal = {TERMINAL_BY_ROLE[role], "env_blocked", "error", "failed", "blocked", "cancelled"}
        status: dict[str, Any] = {}
        for _ in range(60):  # 10 min at 10s; a read-only run has no reason to exceed it
            await asyncio.sleep(10)
            try:
                status = await svc.check_status(session_id)
            except Exception as exc:
                logger.debug("intake.poll_error", role=role, error=str(exc))
                continue
            if ((status or {}).get("status") or (status or {}).get("state")) in terminal:
                break
        handle_run_result(ec, role, status, self._state)  # type: ignore[arg-type]

    def _build_wiki_init_card_context(
        self, symphony_name: str, org: str, repo: str,
    ) -> dict[str, Any]:
        """Build the card_context for a wiki-init seed run (spec 124)."""
        from coordinare.services.env_cache import sanitise_symphony_name
        from coordinare.services.persona_service import get_effective_instructions

        cfg = self._state.get("config")
        persona, backend, model_block = "", "hermes", {}
        role_workflow_env: dict[str, str] = {}
        if cfg is not None:
            try:
                persona = get_effective_instructions("tech_writer", cfg.personas)
            except Exception:
                persona = ""
            rc = cfg.performers.resolved_role("tech_writer") if hasattr(cfg, "performers") else None
            if rc is not None and getattr(rc, "backend", None):
                backend = rc.backend
            role_workflow_env = dict(getattr(rc, "workflow_env", None) or {})
            try:
                model_block = cfg.resolve_performer_dispatch_model("tech_writer")
            except Exception:
                model_block = {}

        return {
            "card_id": f"wiki-init-{symphony_name}",
            "role": "documenting",
            "doc_mode": "init",
            "repo_url": f"https://github.com/{org}/{repo}.git",
            "branch": f"wiki-init/{sanitise_symphony_name(symphony_name)}",
            "base_branch": "main",
            "title": "Initialize the project wiki",
            "description": (
                "Build the initial living docs/wiki for this repository — a "
                "README.md entrypoint plus section pages grounded in the actual "
                "code — and add the Project Wiki pointer to AGENTS.md/CLAUDE.md."
            ),
            "persona_instructions": persona,
            "backend": backend,
            # 415: pointer-file creation is opt-in for documentation updates;
            # the wiki-init card is the opt-in -- its whole job is to seed the
            # pointer section the update runs then refresh. It is not a side
            # run (it builds the wiki on its own branch), and the workflow
            # refreshes pointers only for an explicit False, so say so. The
            # role's workflow_env (notably a configured DOCS_ROOT) rides along;
            # the pointer opt-in is forced on top of it.
            "workflow_env": {**role_workflow_env, "DOCS_CREATE_POINTERS": "1"},
            "documenting_side_run": False,
            **model_block,
        }

    async def _execute_wiki_init_dispatch(self, symphony_name: str, github: Any) -> None:
        """124(US2): dispatch a CARDLESS documenter run in init mode to seed the
        symphony's ``docs/wiki``, then poll → auto-merge the seed PR via
        WikiInitService. Triggered manually by the dashboard "Init wiki" button
        (operator-initiated — no auto-gate, so it never holds other dispatch)."""
        from coordinare.workspace import WorkspaceInfo

        svc = (self._state.get("performer_services") or {}).get("documenting")
        if svc is None:
            logger.warning("wiki_init.no_documenting_service", symphony=symphony_name)
            return
        ec = (self._state.get("env_cache") or {}).get(symphony_name)
        if ec is None or getattr(ec, "wiki_in_flight", False):
            return
        try:
            org = getattr(github, "org", None) or getattr(github, "_org", "") or ""
            repo = getattr(github, "_project_name", "") or ""
        except Exception as exc:
            logger.warning("wiki_init.repo_resolve_failed", symphony=symphony_name, error=str(exc))
            return
        if not (org and repo):
            logger.warning("wiki_init.repo_unknown", symphony=symphony_name, org=org, repo=repo)
            return

        # Fail fast if none is available rather than dispatch a doomed job that
        # the performer rejects with "permanent performer config error: GITHUB_TOKEN".
        token = await self._resolve_cardless_github_token(symphony_name, "wiki_init")
        if not token:
            logger.warning("wiki_init.no_github_token", symphony=symphony_name)
            return

        card_context = self._build_wiki_init_card_context(symphony_name, org, repo)
        # The documenting role gets its GITHUB_TOKEN secret from
        # workspace_info.github_token (env_bootstrap is the only role that reads
        # card_context["_github_token"]). A cardless dispatch never flows through
        # WorkspaceManager.prepare(), so synthesize a self-clone WorkspaceInfo
        # (path=None) here — otherwise the performer fails "permanent performer
        # config error: GITHUB_TOKEN" with no token to clone/push the seed PR.
        workspace_info = WorkspaceInfo(
            path=None,
            branch=card_context["branch"],
            repo_url=card_context["repo_url"],
            github_token=token,
        )
        ec.wiki_in_flight = True
        try:
            result = await svc.dispatch_card(card_context, workspace_info=workspace_info)
        except Exception as exc:
            ec.wiki_in_flight = False
            logger.warning("wiki_init.dispatch_failed", symphony=symphony_name, error=str(exc))
            return
        session_id = (result or {}).get("session_id") or (result or {}).get("job_id")
        if not session_id:
            ec.wiki_in_flight = False
            logger.warning(
                "wiki_init.no_session_id", symphony=symphony_name, result=str(result)[:200],
            )
            return
        logger.info(
            "wiki_init.dispatched",
            symphony=symphony_name,
            branch=card_context["branch"],
            session_id=session_id,
        )
        task = asyncio.create_task(
            self._poll_wiki_init_completion(symphony_name, svc, session_id, github),
        )
        self._wiki_init_poll_tasks.add(task)
        task.add_done_callback(self._wiki_init_poll_tasks.discard)

    async def _poll_wiki_init_completion(
        self,
        symphony_name: str,
        svc: Any,
        session_id: str,
        github: Any,
    ) -> None:
        """Poll a wiki-init documenting job to terminal, then hand its seed PR to
        WikiInitService for auto-merge (CI-green + trusted-bot) or record a
        failure. Empty trusted-bot list simply leaves the PR open for human review."""
        ec = (self._state.get("env_cache") or {}).get(symphony_name)
        if ec is None:
            return
        cfg = self._state.get("config")
        trusted = list(getattr(cfg, "trusted_bot_reviewers", []) or []) if cfg else []
        notif = self._state.get("notification_service")
        status: dict[str, Any] = {}
        for _ in range(180):  # 30 min at 10s — a full-wiki build on a slow model
            await asyncio.sleep(10)
            try:
                status = await svc.check_status(session_id)
            except Exception as exc:
                logger.debug("wiki_init.poll_error", symphony=symphony_name, error=str(exc))
                continue
            st = (status or {}).get("status") or (status or {}).get("state")
            if st in ("docs_committed", "error", "failed", "blocked", "cancelled"):
                break
        st = (status or {}).get("status") or ""
        succeeded = st == "docs_committed"
        pr_node_id = (status or {}).get("pr_node_id") or ""
        try:
            await self._wiki_init_svc.handle_init_result(
                symphony_name,
                ec,
                github,
                pr_node_id,
                trusted,
                notif,
                job_succeeded=succeeded,
                error=(
                    None
                    if succeeded
                    else ((status or {}).get("reason") or st or "wiki-init failed")
                ),
            )
        except Exception as exc:
            ec.wiki_in_flight = False
            logger.warning("wiki_init.handle_result_failed", symphony=symphony_name, error=str(exc))

    def _resolve_bootstrap_volumes(
        self, performer_id: str, svc: Any, symphony_name: str,
    ) -> tuple[Any, dict[str, Any]]:
        """Resolve the env-cache volume mount for a bootstrap dispatch and emit
        the volume-debug line.  Returns (ec_result, dispatch_kw)."""
        from coordinare.services.env_cache import DEFAULT_DEVENV_ROOT, get_env_volume_for_symphony
        from coordinare.services.http_performer_service import HTTPPerformerService

        devenv_root = DEFAULT_DEVENV_ROOT
        if isinstance(svc, HTTPPerformerService):
            devenv_root = svc.devenv_root
        # env_cache read live so we see entries that became ready mid-cycle.
        ec_result = get_env_volume_for_symphony(
            symphony_name,
            self._state.get("env_cache") or {},
            is_bootstrap=True,
            container_devenv_root=devenv_root,
        )
        extra = [ec_result[0]] if ec_result is not None else []
        dispatch_kw: dict[str, Any] = {}
        if isinstance(svc, HTTPPerformerService) and extra:
            dispatch_kw["extra_volumes"] = extra
        ec_state = (self._state.get("env_cache") or {}).get(symphony_name)
        logger.debug(
            "env_cache.bootstrap_dispatch_volume",
            symphony=symphony_name,
            performer_id=performer_id,
            svc_type=type(svc).__name__,
            svc_mode=getattr(getattr(svc, "_config", None), "mode", None),
            devenv_root=devenv_root,
            ec_state_type=type(ec_state).__name__ if ec_state is not None else None,
            ec_state_cache_dir=str(getattr(ec_state, "cache_dir", None)) if ec_state else None,
            ec_state_sanitised=getattr(ec_state, "sanitised_name", None) if ec_state else None,
            ec_result_present=ec_result is not None,
            host_path=str(ec_result[0].host_path) if ec_result else None,
            container_path=str(ec_result[0].container_path) if ec_result else None,
            mount_mode=ec_result[0].mode if ec_result else None,
            extra_volumes_attached=bool(dispatch_kw.get("extra_volumes")),
        )
        return ec_result, dispatch_kw

    def _resolve_bootstrap_backend(
        self, symphony_name: str,
    ) -> tuple[_BootstrapBackend, Any]:
        """Resolve backend for env_bootstrap from the symphony's role config so
        the performer matches the same agent backend used for implementing/etc.
        Default to "codex" (matches coordinare-performer:full image) when no
        role config is available.  Also returns the effective config the
        resolution used (the symphony override when present, else the global
        config), for the workflow-override block."""
        from coordinare.config import CoordinareConfiguration, SymphonyConfig

        resolved = _BootstrapBackend()
        cfg = self._state.get("config")
        symphony_cfg = (self._state.get("symphony_configs") or {}).get(symphony_name)
        coordinare_cfg = self._state.get("coordinare_config")
        if (
            symphony_cfg is not None
            and coordinare_cfg is not None
            and isinstance(symphony_cfg, SymphonyConfig)
            and isinstance(coordinare_cfg, CoordinareConfiguration)
        ):
            cfg = symphony_cfg.effective_config(coordinare_cfg.global_config)
        if cfg is not None and hasattr(cfg, "performers"):
            for _probe_role in ("env_bootstrap", "implementer", "architect", "assessor"):
                rc = cfg.performers.resolved_role(_probe_role)
                if rc is not None and getattr(rc, "backend", None):
                    resolved.backend = rc.backend
                    resolved.effort = getattr(rc, "effort", None)
                    resolved.temperature = getattr(rc, "temperature", None)
                    # 080 moved all model selection to the mode → model_endpoint →
                    # endpoint catalogs; inline performer model/base_url/auth fields
                    # are schema-forbidden. The card-dispatch path resolves the model
                    # via resolve_performer_dispatch_model; the bootstrap path must do
                    # the SAME, otherwise the dispatch carries model=None and the
                    # self-hosted routing table (keyed on (backend, model)) can't match
                    # → claude_code silently falls back to the LiteLLM shim.
                    model_resolution = {}
                    if hasattr(cfg, "resolve_performer_dispatch_model"):
                        model_resolution = cfg.resolve_performer_dispatch_model(_probe_role) or {}
                    resolved.model = model_resolution.get("model") or getattr(rc, "model", None)
                    resolved.base_url = model_resolution.get("base_url") or getattr(
                        rc, "base_url", None,
                    )
                    resolved.api_key_env = model_resolution.get("api_key_env") or getattr(
                        rc, "api_key_env", None,
                    )
                    resolved.auth_token_env = model_resolution.get("auth_token_env") or getattr(
                        rc, "auth_token_env", None,
                    )
                    if hasattr(cfg, "resolve_performer_orchestration"):
                        orchestration = cfg.resolve_performer_orchestration(_probe_role)
                        if isinstance(orchestration, dict):
                            resolved.orchestration = orchestration
                    break
        return resolved, cfg

    async def _finish_bootstrap_dispatch(
        self,
        svc: Any,
        performer_id: str,
        symphony_name: str,
        dispatch_dict: dict[str, Any],
        dispatch_kw: dict[str, Any],
        env_cache_svc: Any,
    ) -> None:
        """Dispatch the bootstrap payload; start the completion poller on
        success, else surface the rich dispatch status/reason and mark
        bootstrap complete-with-error."""
        result = await svc.dispatch_card(dispatch_dict, **dispatch_kw)
        job_id = (result or {}).get("session_id") or (result or {}).get("job_id")
        bootstrap_container_id = (result or {}).get("container_id")
        if job_id and hasattr(svc, "check_status"):
            bootstrap_task = asyncio.create_task(
                self._poll_bootstrap_completion(
                    svc,
                    job_id,
                    symphony_name,
                    env_cache_svc,
                    container_id=bootstrap_container_id,
                ),
                name=f"bootstrap_poll_{symphony_name}",
            )
            self._bootstrap_poll_tasks.add(bootstrap_task)
            bootstrap_task.add_done_callback(self._bootstrap_poll_tasks.discard)
        else:
            # dispatch_card returns a rich {"status","reason"} on every failure
            # mode (container start failed / readiness timeout / payload error /
            # transport-auth / 409 busy).  Surface that reason instead of the
            # generic "no job id" so the dashboard + feedback-injection know
            # WHICH dispatch layer failed.
            _status = (result or {}).get("status")
            _reason = (result or {}).get("reason")
            _detail = (
                f"bootstrap dispatch failed ({_status}): {_reason}"
                if _reason
                else "bootstrap dispatch produced no job id"
            )
            logger.warning(
                "env_cache.bootstrap_no_job_id",
                symphony=symphony_name,
                performer_id=performer_id,
                status=_status,
                reason=_reason,
            )
            env_cache_svc.on_bootstrap_complete(
                symphony_name,
                False,
                self._state,
                error=_detail,
            )

    async def _execute_bootstrap_dispatch(
        self,
        performer_id: str,
        payload: BootstrapJobPayload,
        symphony_name: str,
        performer_svcs: dict[str, Any],
        env_cache_svc: Any,
    ) -> None:
        """Dispatch an env_bootstrap job to a performer and start polling for completion."""
        svc = performer_svcs.get(performer_id)
        if svc is None:
            logger.warning(
                "env_cache.bootstrap_svc_not_found",
                performer_id=performer_id,
                symphony=symphony_name,
            )
            return
        _ec_result, dispatch_kw = self._resolve_bootstrap_volumes(
            performer_id, svc, symphony_name,
        )
        # dispatch_card takes dict[str, Any]; model_dump() is an intentional demotion
        # because the performer HTTP API is untyped at the wire level.
        dispatch_dict = payload.model_dump()
        bootstrap, cfg = self._resolve_bootstrap_backend(symphony_name)
        bootstrap_backend = bootstrap.backend
        bootstrap_model = bootstrap.model
        bootstrap_effort = bootstrap.effort
        bootstrap_temperature = bootstrap.temperature
        bootstrap_base_url = bootstrap.base_url
        bootstrap_api_key_env = bootstrap.api_key_env
        bootstrap_auth_token_env = bootstrap.auth_token_env
        # Bootstrap owns its workflow choice even when its backend falls back.
        bootstrap_role = getattr(getattr(cfg, "performers", None), "env_bootstrap", None)
        workflow = getattr(bootstrap_role, "workflow", None)
        if bootstrap_role is not None and isinstance(workflow, str) and workflow:
            dispatch_dict["workflow"] = workflow
            workflow_env = dict(bootstrap_role.workflow_env)
            budget = int(getattr(self._state.get("coordinare_config"), "bootstrap_max_seconds", 0) or 0)
            if workflow == "env_bootstrap" and budget > 0:
                try:
                    configured = int(workflow_env.get("ENV_BOOTSTRAP_TIMEOUT_SECONDS", "1800"))
                except ValueError:
                    pass  # Preserve invalid input for the workflow's terminal error report.
                else:
                    workflow_env["ENV_BOOTSTRAP_TIMEOUT_SECONDS"] = str(min(configured, budget))
            dispatch_dict["workflow_env"] = workflow_env
        dispatch_dict["backend"] = bootstrap_backend
        if bootstrap_model:
            dispatch_dict["model"] = bootstrap_model
        if bootstrap_effort:
            dispatch_dict["effort"] = bootstrap_effort
        if bootstrap_temperature is not None:
            dispatch_dict["temperature"] = bootstrap_temperature
        if bootstrap_base_url:
            dispatch_dict["base_url"] = bootstrap_base_url
        if bootstrap_api_key_env:
            dispatch_dict["api_key_env"] = bootstrap_api_key_env
        if bootstrap_auth_token_env:
            dispatch_dict["auth_token_env"] = bootstrap_auth_token_env
        if bootstrap.orchestration:
            dispatch_dict["orchestration"] = bootstrap.orchestration
        logger.info(
            "env_cache.bootstrap_backend_resolved",
            symphony=symphony_name,
            backend=bootstrap_backend,
            model=bootstrap_model,
            effort=bootstrap_effort,
        )
        # 060: Bootstrap dispatches don't flow through WorkspaceManager.prepare(),
        # so fetch a fresh GitHub token here from the symphony's workspace manager
        # (App installation token or static PAT) and inject it for the performer
        # to use when cloning the symphony repo. Falls back to env if unavailable.
        sym_wms = self._state.get("symphony_workspace_managers") or {}
        sym_wm = sym_wms.get(symphony_name)
        gh_token: str | None = None
        if sym_wm is not None and hasattr(sym_wm, "get_fresh_github_token"):
            try:
                gh_token = await sym_wm.get_fresh_github_token()
            except Exception as exc:
                logger.warning(
                    "env_cache.bootstrap_token_fetch_failed",
                    symphony=symphony_name,
                    error=str(exc),
                )
        if gh_token:
            dispatch_dict["_github_token"] = gh_token
        await self._finish_bootstrap_dispatch(
            svc, performer_id, symphony_name, dispatch_dict, dispatch_kw, env_cache_svc,
        )

    def _announce_paused_symphonies(self) -> None:
        """Emit a one-time startup line for each symphony paused via ``enabled: false``.

        Called once during startup (not per poll cycle) so a disabled symphony is
        obvious at boot without recurring per-cycle log noise (spec 132 / issue #180).
        """
        symphony_configs = self._state.get("symphony_configs") or {}
        for name, cfg in symphony_configs.items():
            if not getattr(cfg, "enabled", True):
                logger.info(
                    "symphony.paused",
                    symphony=name,
                    enabled=False,
                    detail=f"symphony '{name}' is paused (enabled: false)",
                )

    async def _startup_load_snapshot(self) -> None:
        """T018: Startup recovery — load persisted state before poll loop."""
        if self._state_store is not None:
            try:
                snapshot = await self._state_store.load()
                if snapshot is not None:
                    self._restore_from_snapshot(snapshot)
                    self._emit(
                        **build_runtime_event(
                            category="startup",
                            message="prior state loaded",
                            phase=snapshot.phase,
                            active_card_id=snapshot.active_card_id,
                        ),
                    )
                    # T020: Board reconciliation after restore
                    if snapshot.active_card_id or snapshot.active_sessions:
                        await self._reconcile_with_board(snapshot)
                else:
                    self._emit(
                        **build_runtime_event(
                            category="startup",
                            message="no prior state found",
                        ),
                    )
            except StateLoadError as exc:
                self._emit(
                    **build_runtime_event(
                        category="warning",
                        message="state load failed",
                        reason=exc.reason,
                        detail=exc.detail,
                    ),
                )
                # Fresh start — self._state already initialised by initial_state()

    async def _startup_reconciliation_pass(self) -> None:
        """076 (T056, FR-002): startup reconciliation pass.  Runs AFTER
        snapshot load and board reconciliation, BEFORE the first poll
        cycle.  Walks every in-flight session in the snapshot,
        enumerates Docker containers, and decides adopt / reap+replace
        / fresh-dispatch / orphan-sweep per card.  On Docker-down, the
        report sets docker_unreachable=True and the daemon refuses to
        dispatch any ephemeral performer for this process's lifetime
        (per FR-012)."""
        self._reconciliation_blocked_by_docker = False
        try:
            from coordinare.services.docker_executor import DockerExecutor
            from coordinare.services.reconciliation import (
                run_kubernetes_orphan_sweep,
                run_startup_reconciliation,
            )

            cfg = self._state.get("coordinare_config")
            recon_budget = 30.0
            if cfg is not None:
                _dd = getattr(cfg, "dispatcher_dedup", None)
                if _dd is not None:
                    recon_budget = float(getattr(_dd, "reconciliation_budget_seconds", 30.0))
            report = await run_startup_reconciliation(
                self._state,
                DockerExecutor(),
                budget_seconds=recon_budget,
            )
            if report.docker_unreachable:
                self._reconciliation_blocked_by_docker = True
            # Issue #489: kubernetes counterpart of the FR-005 orphan sweep.
            # Resolves the runtime duck-typed through performer_services, so
            # docker/subprocess deployments no-op. An empty keep-set mirrors
            # the Docker SC-002 fast path. Crashing must not prevent boot.
            try:
                await run_kubernetes_orphan_sweep(self._state)
            except Exception:  # pragma: no cover — defensive crash-blocker
                logger.warning(
                    "daemon.kubernetes_orphan_sweep_crashed",
                    exc_info=True,
                )
        except Exception as exc:  # pragma: no cover — defensive crash-blocker
            # Reconciliation MUST NOT prevent the daemon from booting on a
            # bug or unexpected failure — fall through to the normal cycle
            # with a warning.  Real Docker-down is signalled via
            # docker_unreachable on the report, not via an exception.
            logger.warning(
                "daemon.reconciliation_pass_crashed",
                error=str(exc),
                exc_info=True,
            )

    async def _notify_daemon_restart(self) -> None:
        """T018: Dispatch daemon_restart notification."""
        notification_service = self._state.get("notification_service")
        if notification_service is not None:
            from coordinare.models.notification import (
                EventType,
                NotificationEvent,
                NotificationSeverity,
            )

            try:
                await notification_service.dispatch(
                    NotificationEvent(
                        event_type=EventType.daemon_restart,
                        severity=NotificationSeverity.info,
                        source="daemon",
                        payload={
                            "event_type": "daemon_restart",
                            "severity": "info",
                            "source": "daemon",
                            "run_mode": self._run_mode,
                            "summary": f"🔄 Coordinare restarted (mode: {self._run_mode})",
                        },
                    ),
                )
            except Exception as exc:
                logger.warning("daemon_restart_notification_failed", error=str(exc))

    async def _run_env_cache_and_cardless_cycle(self, symphony_configs: dict[str, Any]) -> None:
        """Per-cycle env-cache bootstrap checks plus the card-less dispatches
        (173 intake, 124 wiki-init) for every configured symphony."""
        # 060: Env-cache SHA check — run once per cycle before orchestration.
        _env_cache_svc = self._state.get("env_cache_service")
        if _env_cache_svc is not None:
            _sym_gh_svcs = self._state.get("symphony_github_services") or {}
            # Snapshot performer services once before the loop so the
            # closure captures a stable mapping even if the state dict
            # is mutated mid-cycle by a performer reconnect.
            # env_cache is read live inside the closure because it only
            # exists after initialise() runs and its entries grow as
            # cache dirs are created — snapshotting it here would miss
            # caches that became ready during this cycle.
            # Bootstrap dispatch looks up by performer *id* (e.g. "codex-ephemeral"),
            # not by lifecycle stage — so use the id-keyed map populated at startup.
            _ec_performer_svcs = dict(self._state.get("performer_services_by_id") or {})
            for _ec_sym_name, _ec_sym_cfg in symphony_configs.items():
                _ec_gh_svc = _sym_gh_svcs.get(_ec_sym_name)
                if _ec_gh_svc is None:
                    continue

                async def _bootstrap_dispatch_fn(
                    performer_id: str,
                    payload: BootstrapJobPayload,
                    _sym: str = _ec_sym_name,
                    _svc_map: dict[str, Any] = _ec_performer_svcs,
                    _ec_svc: Any = _env_cache_svc,
                ) -> None:
                    await self._execute_bootstrap_dispatch(
                        performer_id, payload, _sym, _svc_map, _ec_svc,
                    )

                from coordinare.services.env_cache import DEFAULT_DEVENV_ROOT

                _bootstrap_devenv_root = DEFAULT_DEVENV_ROOT
                _bootstrap_svc = _ec_performer_svcs.get(
                    _ec_sym_cfg.env_bootstrap_performer_id or "",
                )
                if _bootstrap_svc is not None:
                    from coordinare.services.http_performer_service import (
                        HTTPPerformerService,
                    )

                    if isinstance(_bootstrap_svc, HTTPPerformerService):
                        _bootstrap_devenv_root = _bootstrap_svc.devenv_root

                # 088 (US5): restart honor path — hand the service a
                # clean-room verifier so a persisted success is
                # re-verified (not re-bootstrapped) on fresh boot.
                async def _clean_verify_fn(
                    _sym_name: str,
                    _svc: Any = _bootstrap_svc,
                ) -> tuple[bool | None, str]:
                    return await self._verify_env_cache_clean(_sym_name, _svc)

                await _env_cache_svc.check_and_trigger(
                    symphony_name=_ec_sym_name,
                    symphony_config=_ec_sym_cfg,
                    github_service=_ec_gh_svc,
                    state=self._state,
                    dispatch_fn=_bootstrap_dispatch_fn,
                    container_devenv_root=_bootstrap_devenv_root,
                    llm_chat=self._get_manifest_llm_chat(),
                    clean_verify_fn=_clean_verify_fn,
                )

            # 173: the two card-less intake runs (advocate, curator).
            # Gated, rate limited and dispatched straight from here:
            # neither owns a card, so neither can go through the
            # graph node, which refuses a dispatch without one.
            for _irole in ("advocate", "curator"):
                _igh = _sym_gh_svcs.get(_ec_sym_name)
                if _igh is not None:
                    await self._maybe_dispatch_intake(_irole, _ec_sym_name, _igh)

            # 124(US2): drain manual wiki-init requests (dashboard
            # "Init wiki" button). Operator-initiated, so it dispatches
            # regardless of the default-off auto-gate and holds nothing.
            # 165: documenter side runs for cards whose blueprint has a
            # documentation brief and that have moved past architecting.
            if self._wiki_init_requests:
                for _wsym in list(self._wiki_init_requests):
                    self._wiki_init_requests.discard(_wsym)
                    _wgh = _sym_gh_svcs.get(_wsym)
                    if _wgh is not None:
                        await self._execute_wiki_init_dispatch(_wsym, _wgh)
                    else:
                        # The symphony was removed/disabled between the
                        # button click (202) and this drain. Surface the
                        # drop rather than discarding it silently.
                        logger.warning(
                            "wiki_init.request_dropped_no_github_service",
                            symphony=_wsym,
                        )

    async def _run_orchestration(self, symphony_configs: dict[str, Any]) -> bool:
        """057: Multi-symphony orchestration cycle.  Returns True when the
        multi-symphony path ran (False = legacy single-symphony mode)."""
        _multi_symphony = bool(symphony_configs)
        if symphony_configs:
            await self._run_env_cache_and_cardless_cycle(symphony_configs)

            logger.info(
                "symphony.loop_entry",
                symphony_count=len(symphony_configs),
                symphony_names=list(symphony_configs.keys()),
                sym_gh_keys=list(
                    (self._state.get("symphony_github_services") or {}).keys(),
                ),
                global_gh_present=self._state.get("github_service") is not None,
            )
            for sym_name, sym_cfg in symphony_configs.items():
                if not self._running or self._stop_event.is_set():
                    break
                if not getattr(sym_cfg, "enabled", True):
                    # Paused symphonies are announced once at startup by
                    # _announce_paused_symphonies(); skip silently here to
                    # avoid per-cycle log noise (spec 132 / issue #180).
                    continue
                await self._conduct_single_symphony(sym_name, sym_cfg)
            # Rebuild aggregate active_sessions from all symphony states so
            # downstream metrics, slot sync, and dashboard see the full picture.
            _agg_sessions: dict[str, Any] = {}
            for _ss in (self._state.get("symphony_states") or {}).values():
                _agg_sessions.update(getattr(_ss, "active_sessions", None) or {})
            self._state["active_sessions"] = _agg_sessions
            self._state["phase"] = _derive_global_phase(_agg_sessions)
        else:
            # Legacy single-symphony mode (backward compat).
            # 066 T019/FR-004: unified entry path — empty-sessions case
            # short-circuits to a single graph cycle inside the method.
            await self._invoke_multi_session()
        return _multi_symphony

    def _record_cycle_success_metrics(self, _cycle_t0: float) -> float:
        """US1: record cycle metrics + US3: mark external subsystems healthy."""
        _cycle_elapsed = perf_counter() - _cycle_t0
        METRICS.cycles_completed_total.inc()
        METRICS.cycle_duration_seconds.observe(_cycle_elapsed)
        # 035: Update active session gauge
        _active = self._state.get("active_sessions") or {}
        METRICS.active_sessions.set(len(_active))
        # US3: mark external service subsystems healthy after a successful poll cycle
        HEALTH.update("github", HealthStatus.healthy)
        HEALTH.update("agent", HealthStatus.healthy)
        # config and notifications don't change mid-run; refresh timestamps
        # so the stale-detection window doesn't expire between cycles.
        HEALTH.update("config", HealthStatus.healthy)
        if self._state.get("notification_service") is not None:
            HEALTH.update("notifications", HealthStatus.healthy)
        return _cycle_elapsed

    async def _reconcile_board_pauses(self, *, board_is_fresh: bool = True) -> None:
        """Retry owned stops during outages; change pause intent on fresh reads."""
        board = (self._state.get("board_snapshot") or {}) if board_is_fresh else {}
        sessions = self._state.get("active_sessions") or {}
        for card_id, sess in sessions.items():
            card = sess.get("current_card") or {}
            item_id = str(card.get("content_id") or card.get("id") or card_id)
            column = next((col for col, ids in board.items() if item_id in ids or card_id in ids), "")
            _observe_handoff_board_column(sess, column)
            dispatch = sess.get("agent_dispatch") or {}
            side = sess.get("documenting_side") or {}
            writers = [(sess.get("performer_stage"), dispatch, False)]
            if side.get("status") == "running" or side.get("writer_active"):
                writers.append(("documenting", side, True))
            newly_paused = _is_manual_board_block(sess, column) or (column in {"TODO", "BACKLOG"} and (
                any(identity.get("session_id") for _, identity, _ in writers)
                or _handoff_needs_board_pause(sess, column)
            ))
            if not sess.get("board_paused") and not newly_paused:
                continue
            was_paused = bool(sess.get("board_paused"))
            _capture_board_pause_resume_phase(sess)
            sess["board_paused"] = True
            if not was_paused or column == "BACKLOG":
                sess["board_pause_column"] = column
            pause_column = str(sess.get("board_pause_column") or column)
            stopped = await stop_owned_writers(self._state, card_id, sess, reason="board paused")
            if not stopped:
                # Keep the live phase: it continues to reserve worker capacity.
                sess["phase"] = "monitoring_performer"
                _record_unconfirmed_board_pause(sess)
                activity = self._state.get("activity_log")
                if activity is not None:
                    activity.record(
                        activity_type="blocked", card_id=card_id,
                        card_title=str(card.get("title") or ""),
                        stage=str(sess.get("performer_stage") or ""),
                        text="Board paused; worker may still be running. Ownership retained; resume waits for confirmed stop.",
                    )
                continue
            sess["phase"] = "blocked"
            if was_paused and column != pause_column and column in {"TODO", "IN_PROGRESS", "IN_REVIEW"}:
                sess["board_paused"] = False
                sess["board_pause_column"] = ""
                if sess.get("pending_pr_handoff"):
                    sess["pending_pr_handoff"]["resumed_board_column"] = column
                _resume_board_paused_session(sess)
            logger.info("daemon.board_pause_reconciled", card_id=card_id, board_status=column, stopped=stopped)

    async def _reconcile_board_state_with_release(self) -> None:
        """Issue #516: board ↔ local-state reconciliation, then best-effort
        release of a retired session's performer resources.

        Runs reconcile_board_state against the current board snapshot and,
        when a session was retired for a DONE card, hands it to the
        existing release-time teardown so a still-running performer does
        not outlive the session that dropped it (the monitor never polls
        a card that left the active columns).  Best-effort: a release
        failure is logged and swallowed by _release_session_resources.
        """
        try:
            _board = self._state.get("board_snapshot")
            if isinstance(_board, dict) and (_board or _has_fresh_board_snapshot(self._state)):
                # Graph board moves can make this pre-cycle snapshot stale.
                # Cancel paused writers only after the fresh pre-fanout poll.
                await _retire_nonfocused_paused_terminal(self._state, _board)
                result = reconcile_board_state(
                    self._state, _board, allow_paused_missing=_has_fresh_board_snapshot(self._state),
                )
                retired = result.get("retired_session")
                if isinstance(retired, dict):
                    await _release_done_session_resources(self._state, retired)
        except Exception as _exc:
            logger.warning(
                "daemon.board_reconcile_crashed",
                error=str(_exc),
                exc_info=True,
            )

    async def _post_cycle_invariants(self) -> None:
        """076 end-of-cycle invariants: wedge detection, then board ↔ local
        reconciliation (after the wedge invariant so a wedge-released pin
        doesn't re-trigger there)."""
        # 076 (T064): reconciliation_decisions_last_startup is
        # cleared per-card by notify.py on consumption (see
        # contracts/notification-dedup.md and the
        # ``recon_decisions.pop(...)`` site in notify.py).  No
        # cycle-level clear needed.

        # 076 (T092, FR-020): wedge invariant.  Runs at the end
        # of every successful cycle.  Detects the forbidden
        # "active_card pinned + no session + idle phase"
        # combination that produced today's incident.  Default:
        # release the pin; ≥3 wedges in 24h → promote to BLOCKED.
        try:
            from coordinare.services.reconciliation import detect_wedged_state

            _dd_cfg = getattr(self._state.get("coordinare_config"), "dispatcher_dedup", None)
            _threshold = int(getattr(_dd_cfg, "wedge_block_threshold", 3))
            _window_hours = int(getattr(_dd_cfg, "wedge_block_window_hours", 24))
            detect_wedged_state(
                self._state,
                wedge_block_threshold=_threshold,
                wedge_block_window_hours=_window_hours,
            )
        except Exception as _exc:  # pragma: no cover — defensive
            logger.warning(
                "daemon.wedge_invariant_crashed",
                error=str(_exc),
                exc_info=True,
            )

        # 076 (T121, FR-025): board ↔ local-state reconciliation.
        # Runs after the wedge invariant so a wedge-released pin
        # doesn't re-trigger here.  Compares state.active_card
        # .status with the board's column for the same card;
        # divergence → release the pin so eligibility re-picks.
        # Issue #516: the shared helper also best-effort releases the
        # retired session's performer resources.
        await self._reconcile_board_state_with_release()

    def _post_cycle_dashboard(self, cycle_count: int, _cycle_elapsed: float) -> None:
        """Dashboard: record cycle and broadcast updated snapshot to all open tabs."""
        if self._dashboard_store is not None:
            _current_phase = str(self._state.get("phase", "idle"))
            self._dashboard_store.record_cycle(
                duration_seconds=_cycle_elapsed,
                phase=_current_phase,
                outcome="success",
            )
            _snapshot = self._dashboard_store.build_snapshot(self, METRICS, HEALTH)
            self._dashboard_store.broadcaster.broadcast(_snapshot)
        self._emit(
            **build_runtime_event(
                category="activity",
                message="processing cycle completed",
                cycle=cycle_count,
                phase=self._state.get("phase", "unknown"),
            ),
        )
    def _emit_phase_transition(
        self, previous_phase: Any, current_phase: Any, _multi_symphony: bool,
    ) -> None:
        """Emit the legacy-mode phase-transition metric (runtime event already
        emitted by the caller)."""
        # In legacy mode, emit the phase-transition metric here.
        # In multi-symphony mode it is emitted per-symphony inside
        # _conduct_single_symphony() with the actual symphony label.
        if not _multi_symphony:
            _transition_label = _PHASE_TRANSITION_METRIC.get(
                (str(previous_phase), str(current_phase)),
            )
            if _transition_label is not None:
                METRICS.card_state_transitions_total.labels(
                    symphony="__default__",
                    transition_type=_transition_label,
                ).inc()

    async def _check_prolonged_idle(
        self, last_activity_at: float, notification_service: Any,
    ) -> float:
        """T019: Prolonged idle detection.  Returns the (possibly new)
        last-activity timestamp."""
        current_phase = self._state.get("phase")
        if current_phase != "idle":
            return monotonic()
        if notification_service is not None:
            idle_seconds = monotonic() - last_activity_at
            if idle_seconds >= self._idle_threshold_seconds:
                from coordinare.models.notification import (
                    EventType,
                    NotificationEvent,
                    NotificationSeverity,
                )

                try:
                    await notification_service.dispatch(
                        NotificationEvent(
                            event_type=EventType.prolonged_idle,
                            severity=NotificationSeverity.warning,
                            source="daemon",
                            payload={
                                "event_type": "prolonged_idle",
                                "severity": "warning",
                                "source": "daemon",
                                "idle_seconds": str(int(idle_seconds)),
                                "summary": f"💤 Coordinare has been idle for {int(idle_seconds // 60)} minutes — no cards to process",
                            },
                            # 530: last_activity_at is the idle episode's start
                            # while the coordinare stays idle, and it resets the
                            # moment the phase leaves idle.
                            dedup_key=f"prolonged_idle@{last_activity_at:.6f}",
                            episode_scoped=True,
                        ),
                    )
                except Exception as exc:
                    logger.warning("prolonged_idle_notification_failed", error=str(exc))
        return last_activity_at

    async def _detect_stuck_card(self, notification_service: Any) -> None:
        """028: Stuck card detection (with cooldown to avoid alert spam)."""
        sessions = self._state.get("active_sessions") or {}
        if sessions:
            await self._detect_stuck_performers(sessions, notification_service)
            # Retained history must not turn an inactive aggregate phase into
            # a worker alert. Preserve legacy non-monitoring transition alerts.
            if self._state.get("phase") in {
                "idle", "blocked", "system_error", "monitoring_pr",
                "monitoring_performer", "monitoring_agent",
            }:
                return
        else:
            self._session_stuck_alerts.clear()
            self._stuck_observed_starts.clear()
        _stuck_phase = self._state.get("phase")
        _stuck_excluded = {"idle", "system_error"}
        # 138 T038: the `notification_service is not None` gate that used
        # to sit here is gone — detection must run so the activity feed
        # gets its entry with zero channels configured. It was dead in
        # production anyway (build_notification_service always returns a
        # service); the removal only matters for tests and non-dashboard
        # embeddings, which is why the dispatch below now guards itself.
        if _stuck_phase and _stuck_phase not in _stuck_excluded:
            _config = self._state.get("config")
            _phase_entered = self._state.get("phase_entered_at")
            if (
                _config is not None
                and _phase_entered is not None
                and hasattr(_config, "stuck_alerts")
            ):
                _stuck_cfg = _config.stuck_alerts
                _threshold = _stuck_cfg.per_phase_thresholds.get(
                    _stuck_phase, _stuck_cfg.threshold_seconds,
                )
                _raw_cooldown = getattr(_stuck_cfg, "cooldown_seconds", None)
                _cooldown = _raw_cooldown if _raw_cooldown is not None else _threshold
                # 138: this block used to be shielded by the (dead)
                # notification_service gate. Now that detection always
                # runs, a non-numeric threshold — a stubbed config in a
                # test, a hand-edited YAML — must disable it rather than
                # raise into the cycle. Real configs are pydantic ints.
                if not isinstance(_threshold, int):
                    _threshold = 0
                if not isinstance(_cooldown, int):
                    _cooldown = _threshold
                if _threshold > 0:
                    _elapsed = (datetime.now(UTC) - _phase_entered).total_seconds()
                    _last_stuck = getattr(self, "_last_stuck_alert_at", None)
                    _cooldown_ok = (
                        _last_stuck is None or (monotonic() - _last_stuck) >= _cooldown
                    )
                    if _elapsed > _threshold and _cooldown_ok:
                        await self._emit_stuck_alert(
                            _stuck_phase,
                            _threshold,
                            _elapsed,
                            _phase_entered,
                            notification_service,
                        )

    def _stuck_performer_start(
        self, key: tuple[str, str], session: dict[str, Any], now: datetime,
    ) -> datetime | None:
        """Resolve a durable start or conservative older-snapshot fallback."""
        dispatched_at = session.get("agent_dispatch_at")
        if dispatched_at is None:
            # Older snapshots omitted dispatch time. Persisted progress gives
            # a minimum age; otherwise observe once rather than skip forever.
            progress_at = session.get("last_progress_at")
            start = (
                progress_at if isinstance(progress_at, datetime)
                and progress_at.tzinfo is not None else now
            )
            dispatched_at = self._stuck_observed_starts.setdefault(key, start)
        elif not isinstance(dispatched_at, datetime) or dispatched_at.tzinfo is None:
            return None
        entered_at = session.get("phase_entered_at")
        if isinstance(entered_at, datetime) and entered_at.tzinfo is not None:
            dispatched_at = max(dispatched_at, entered_at)
        return dispatched_at

    async def _detect_stuck_performers(
        self, sessions: dict[str, Any], notification_service: Any,
    ) -> None:
        """Use each live worker's clock and identity, independent of siblings."""
        config = self._state.get("config")
        stuck_config = getattr(config, "stuck_alerts", None)
        if stuck_config is None:
            return
        now = datetime.now(UTC)
        live_keys: set[tuple[str, str]] = set()
        for card_id, session in sessions.items():
            if not isinstance(session, dict) or session.get("board_paused"):
                continue
            phase = session.get("phase")
            if phase not in {"monitoring_performer", "monitoring_agent"}:
                continue
            dispatch = session.get("agent_dispatch") or {}
            if not isinstance(dispatch, dict):
                continue
            session_id = dispatch.get("session_id")
            if not isinstance(session_id, str) or not session_id:
                continue
            card = session.get("current_card")
            if not isinstance(card, dict) or not card.get("id"):
                continue
            key = (card_id, session_id)
            live_keys.add(key)
            dispatched_at = self._stuck_performer_start(key, session, now)
            if dispatched_at is None:
                continue
            threshold = stuck_config.per_phase_thresholds.get(
                phase, stuck_config.threshold_seconds,
            )
            if not isinstance(threshold, int) or threshold <= 0:
                continue
            cooldown = getattr(stuck_config, "cooldown_seconds", threshold)
            if not isinstance(cooldown, int):
                cooldown = threshold
            elapsed = (now - dispatched_at).total_seconds()
            last_alert = self._session_stuck_alerts.get(key)
            if elapsed <= threshold or (
                last_alert is not None and monotonic() - last_alert < cooldown
            ):
                continue
            await self._emit_stuck_alert(
                phase, threshold, elapsed, dispatched_at, notification_service,
                card=card, session_id=session_id,
            )
            self._session_stuck_alerts[key] = monotonic()
        # Ended/replaced workers cannot retain cooldowns indefinitely.
        self._session_stuck_alerts = {
            key: stamp for key, stamp in self._session_stuck_alerts.items()
            if key in live_keys
        }
        self._stuck_observed_starts = {
            key: stamp for key, stamp in self._stuck_observed_starts.items()
            if key in live_keys
        }

    async def _emit_stuck_alert(
        self,
        _stuck_phase: str,
        _threshold: int,
        _elapsed: float,
        _phase_entered: Any,
        notification_service: Any,
        *,
        card: dict[str, Any] | None = None,
        session_id: str = "",
    ) -> None:
        """Emit one stuck-card alert: card_stuck log, activity-feed entry,
        optional channel dispatch, then advance the cooldown stamp."""
        from coordinare.models.notification import (
            EventType,
            NotificationEvent,
            NotificationSeverity,
        )

        _card = card if card is not None else resolve_stuck_card(self._state)
        _card_title = str(_card.get("title", ""))[:50]
        _card_num = _card.get("issue_number", "")
        _card_id = str(_card.get("id", ""))
        _card_ref = f"#{_card_num} " if _card_num else ""
        _summary = f"⏰ {_card_ref}{_card_title} — stuck in {_stuck_phase} for {round(_elapsed // 60)} min"
        # 138: the destination that always exists. The bug
        # this closes was a *delivery* failure — detection
        # ran, then the decision was handed to a service
        # with no channel to route it to and dropped.
        # 139: the surface that survives everything being
        # switched off. The activity feed below needs the
        # dashboard to read it, and channels are optional,
        # so with both off a stall was previously recorded
        # nowhere an operator could see. The only log line
        # near here was about the *notification* failing —
        # and with no channels there is nothing to fail.
        _stuck_key = stuck_dedup_key(
            _card_id, _stuck_phase, _phase_entered,
        )
        if session_id:
            _stuck_key = f"{_stuck_key}:{session_id}"
        if should_log_stall(
            self._logged_stalls, _stuck_key, time.monotonic(),
        ):
            logger.warning(
                "card_stuck",
                card_id=_card_id,
                card_number=_card_num,
                card_title=_card_title,
                stage=_stuck_phase,
                stuck_minutes=round(_elapsed // 60),
                detail=(
                    "no further action is being taken on this card; "
                    "this line appears regardless of notification "
                    "channels or the dashboard"
                ),
            )

        _alog = self._state.get("activity_log")
        if _alog is not None:
            with contextlib.suppress(Exception):
                _alog.record(
                    activity_type="stuck",
                    card_id=str(_card.get("id", "")),
                    card_number=_card.get("issue_number"),
                    card_title=str(_card.get("title", "")),
                    stage=_stuck_phase,
                    text=f"stuck in {_stuck_phase} for {round(_elapsed // 60)} min",
                    session_id=session_id,
                )
        try:
            if notification_service is not None:
                await notification_service.dispatch(
                    NotificationEvent(
                        event_type=EventType.card_stuck,
                        severity=NotificationSeverity.warning,
                        payload={
                            "phase": _stuck_phase,
                            "elapsed_seconds": str(round(_elapsed)),
                            "threshold_seconds": str(_threshold),
                            "card_title": str(_card.get("title", "")),
                            "card_id": str(_card.get("id", "")),
                            "summary": _summary,
                        },
                        source="daemon",
                        dedup_key=_stuck_key,
                    ),
                )
        except Exception as _exc:
            logger.warning(
                "stuck_card_notification_failed", error=str(_exc),
            )
        # Outside the try AND outside the dispatch guard:
        # the cooldown must advance whether or not a
        # channel exists, or the feed takes a stuck entry
        # every cycle (FR-013, SC-006).
        # Live workers advance their own episode cooldown in the caller;
        # they must not consume a separate legacy transition's cooldown.
        if not session_id:
            self._last_stuck_alert_at = monotonic()

    async def _handle_circuit_open(self, exc: CircuitOpenError) -> bool:
        """Handle an open circuit mid-cycle.  Returns True when the daemon
        must stop (external cancellation during the backoff wait)."""
        self._cycle_active = False
        current_sym = self._state.get("current_symphony") or "__default__"
        self._state["current_symphony"] = None
        METRICS.service_calls_total.labels(
            symphony=current_sym,
            service=exc.service_name,
            action="call_blocked",
            outcome="circuit_open",
        ).inc()
        logger.warning(
            "circuit_open.call_skipped",
            service=exc.service_name,
        )
        # Mark the isolated service degraded so /ready reflects the circuit state.
        # Use the mapping to translate circuit service names to health subsystem names.
        _health_subsystem = _CIRCUIT_TO_HEALTH_SUBSYSTEM.get(exc.service_name)
        if _health_subsystem is not None:
            HEALTH.update(
                _health_subsystem,
                HealthStatus.degraded,
                details=f"circuit open: {exc.service_name}",
            )
        # Do NOT set self._running = False — continue the poll loop
        clear_cycle_id()
        # Use a dedicated backoff rather than _wait_for_next_cycle():
        # in poll=0 (webhook-only) mode, _wait_for_next_cycle blocks
        # until a webhook fires — but if GitHub is down the circuit is
        # open AND no webhooks arrive, causing an indefinite hang.
        # This backoff always makes forward progress and respects stop().
        _backoff = (
            self._poll_interval_seconds
            if self._poll_interval_seconds > 0
            else _CIRCUIT_OPEN_BACKOFF_SECONDS
        )
        _sleep_t = asyncio.ensure_future(self._sleep(_backoff))
        _stop_t = asyncio.ensure_future(self._stop_event.wait())
        try:
            _cb_done, _cb_pending = await asyncio.wait(
                {_sleep_t, _stop_t}, return_when=asyncio.FIRST_COMPLETED,
            )
            for _t in _cb_pending:
                _t.cancel()
        except asyncio.CancelledError:
            _sleep_t.cancel()
            _stop_t.cancel()
            return True  # treat external cancellation as stop signal
        return False

    async def _handle_cycle_failure(
        self, exc: Exception, _cycle_t0: float,
    ) -> tuple[RuntimeExecutionError, Any]:
        """Generic cycle-failure path: record, transition to recovery, and
        return the error to raise after shutdown along with the pre-recovery
        phase (the caller's previous_phase tracking must see it)."""
        self._cycle_active = False
        self._state["error_count"] = self._state.get("error_count", 0) + 1
        self._emit(
            **build_runtime_event(
                category="failure",
                message="runtime processing cycle failed",
                error=str(exc),
                failing_step="cycle_execution",
                error_count=self._state["error_count"],
            ),
        )
        previous_phase = self._state.get("phase", "unknown")
        self._state["phase"] = "recovery"
        self._emit(
            **build_runtime_event(
                category="state_change",
                message="state transition detected",
                previous_phase=previous_phase,
                current_phase="recovery",
            ),
        )
        failure = RuntimeExecutionError(phase="runtime", step="cycle_execution", cause=exc)
        # Dashboard: record error cycle and broadcast
        if self._dashboard_store is not None:
            _err_phase = str(self._state.get("phase", "recovery"))
            self._dashboard_store.record_cycle(
                duration_seconds=perf_counter() - _cycle_t0,
                phase=_err_phase,
                outcome="error",
            )
            _err_snapshot = self._dashboard_store.build_snapshot(self, METRICS, HEALTH)
            self._dashboard_store.broadcaster.broadcast(_err_snapshot)
        self._running = False
        clear_cycle_id()
        return failure, previous_phase

    async def _run_startup_sequence(self) -> tuple[Any, Any]:
        """Startup: snapshot load, 076 reconciliation pass, startup-complete
        announcement, paused-symphony line, and the daemon_restart
        notification.  Returns (previous_phase, previous_lifecycle_sig)."""
        # T018: Startup recovery — load persisted state before poll loop
        await self._startup_load_snapshot()
        previous_lifecycle_sig = self._lifecycle_signature()

        # 076 (T056, FR-002) comment lives on the helper; runs after snapshot
        # load and board reconciliation, before the first poll cycle.
        await self._startup_reconciliation_pass()

        previous_phase = self._state.get("phase")
        try:
            previous_lifecycle_sig = await self._save_snapshot_if_changed(previous_lifecycle_sig)
        except Exception as exc:
            logger.warning("daemon.startup_snapshot_save_failed", error=str(exc))
        self._emit(
            **build_runtime_event(
                category="startup",
                message="daemon startup complete",
                run_mode=self._run_mode,
                poll_interval_seconds=self._poll_interval_seconds,
            ),
        )

        # 132 (issue #180): announce paused symphonies once at startup so a
        # disabled symphony is obvious, without the per-cycle log noise the
        # poll loop used to emit.
        self._announce_paused_symphonies()

        # T018: Dispatch daemon_restart notification
        await self._notify_daemon_restart()
        return previous_phase, previous_lifecycle_sig

    async def _begin_cycle(self) -> float:
        """Cycle preamble: config-reload check, cycle_id correlation, slot
        sync, documenting side runs.  Returns the cycle start timestamp."""
        self._cycle_active = True
        # 057: Check for config reload at cycle start
        if self._config_reload_trigger.is_set():
            self._config_reload_trigger.clear()
            await self._handle_config_reload()

        # US2: bind a unique cycle_id for log correlation
        cycle_id = str(uuid4())
        bind_cycle_id(cycle_id)
        _cycle_t0 = perf_counter()

        # 035: Multi-card parallelism — when concurrency > 1,
        # iterate over active sessions independently.
        self._state["session_skip_reasons"] = {}
        # Free stale slots on every cycle (not just in multi-card mode).
        _slot_mgr = self._state.get("slot_manager")
        if _slot_mgr is not None and hasattr(_slot_mgr, "sync_from_sessions"):
            _active_sessions = self._state.get("active_sessions") or {}
            _slot_mgr.sync_from_sessions(_active_sessions)

        # Resume existing side polls here. New writers require the scoped
        # preflight board read and pause reconciliation in _invoke_multi_session.
        await self._dispatch_documenting_side_runs(self._state.get("symphony_github_services") or {})
        return _cycle_t0

    def _check_phase_transition(self, previous_phase: Any, _multi_symphony: bool) -> Any:
        """Emit a state-transition event when the phase moved; returns the
        phase that should become the new previous_phase."""
        current_phase = self._state.get("phase")
        if current_phase != previous_phase:
            self._emit(
                **build_runtime_event(
                    category="state_change",
                    message="state transition detected",
                    previous_phase=previous_phase,
                    current_phase=current_phase,
                ),
            )
            self._emit_phase_transition(previous_phase, current_phase, _multi_symphony)
            # 028: Track when the phase was entered
            self._state["phase_entered_at"] = datetime.now(UTC)
            return current_phase
        return previous_phase

    def _maybe_emit_heartbeat(self, last_heartbeat: float, cycle_count: int) -> float:
        """Emit the periodic heartbeat runtime event.  Returns the (possibly
        new) last-heartbeat timestamp."""
        now = monotonic()
        if now - last_heartbeat >= self._heartbeat_interval_seconds:
            self._emit(
                **build_runtime_event(
                    category="heartbeat",
                    message="daemon heartbeat",
                    cycle=cycle_count,
                    phase=self._state.get("phase", "unknown"),
                ),
            )
            return now
        return last_heartbeat

    async def _save_snapshot_if_changed(self, previous_lifecycle_sig: Any) -> Any:
        """Persist the snapshot when the lifecycle signature changed.

        T021 + 077: Persist the snapshot whenever lifecycle-relevant
        state changes — NOT only on daemon `phase` transitions. `phase`
        stays 'monitoring_performer' across an ENTIRE card lifecycle
        (assess→architect→…→qa), so the old phase-only trigger captured
        just the first stage and every restart rewound the card to
        'assessing'. The signature includes `phase`, so this still
        covers the phase-transition case the old code handled.
        Returns the signature that is now current."""
        if self._state_store is None:
            return previous_lifecycle_sig
        _lifecycle_sig = self._lifecycle_signature()
        if _lifecycle_sig != previous_lifecycle_sig:
            snapshot = self._build_snapshot()
            await self._state_store.save(snapshot)
            if isinstance(self._state_store, StateStore) and self._state_store.last_snapshot is not snapshot:
                return previous_lifecycle_sig
            return _lifecycle_sig
        return previous_lifecycle_sig

    async def _shutdown_flush(self, previous_lifecycle_sig: Any) -> None:
        """077: Flush final state on shutdown so a clean restart resumes at the
        current lifecycle stage instead of the last transition snapshot. The
        per-cycle signature save above already keeps the snapshot current to
        within one poll, so this is a best-effort belt-and-suspenders flush.
        Gate it on the lifecycle signature actually having changed since the
        last save: an idle daemon whose state never moved must NOT write a
        snapshot (test_no_snapshot_write_when_phase_unchanged), and a redundant
        rewrite of already-persisted state is pointless."""
        if self._state_store is not None and self._lifecycle_signature() != previous_lifecycle_sig:
            try:
                await self._state_store.save(self._build_snapshot())
            except Exception as exc:  # pragma: no cover — best-effort flush
                logger.warning("daemon.shutdown_snapshot_failed", error=str(exc))

    async def start(self) -> None:
        self._main_task = asyncio.current_task()
        self._running = True
        self._install_signal_handlers()
        last_heartbeat = monotonic()
        cycle_count = 0

        previous_phase, previous_lifecycle_sig = await self._run_startup_sequence()

        if self._poll_interval_seconds == 0:
            logger.info("polling_disabled")

        # T019: Track prolonged idle
        last_activity_at = monotonic()

        notification_service = self._state.get("notification_service")
        failure: RuntimeExecutionError | None = None
        # Bind the cycle timestamp before the loop: _begin_cycle() takes its
        # reading only after fallible preamble work, so a first-iteration raise
        # would otherwise leave the failure handler with an UnboundLocalError
        # masking the real exception.
        _cycle_t0 = perf_counter()
        while self._running and not self._stop_event.is_set():
            try:
                _cycle_t0 = await self._begin_cycle()

                # 057: Multi-symphony orchestration (env-cache checks, card-less
                # dispatches, per-symphony cycles, aggregate rebuild).
                _multi_symphony = await self._run_orchestration(
                    self._state.get("symphony_configs") or {},
                )

                # US1: record cycle metrics
                _cycle_elapsed = self._record_cycle_success_metrics(_cycle_t0)

                self._cycle_active = False
                cycle_count += 1
                self._state["error_count"] = 0

                await self._post_cycle_invariants()

                # Dashboard: record cycle and broadcast updated snapshot to all open tabs
                self._post_cycle_dashboard(cycle_count, _cycle_elapsed)
                previous_phase = self._check_phase_transition(previous_phase, _multi_symphony)
                previous_lifecycle_sig = await self._save_snapshot_if_changed(
                    previous_lifecycle_sig,
                )

                # T019: Prolonged idle detection
                last_activity_at = await self._check_prolonged_idle(
                    last_activity_at, notification_service,
                )

                # 028: Stuck card detection (with cooldown to avoid alert spam)
                await self._detect_stuck_card(notification_service)

                last_heartbeat = self._maybe_emit_heartbeat(last_heartbeat, cycle_count)

                if self._max_cycles is not None and cycle_count >= self._max_cycles:
                    self.stop()
                    clear_cycle_id()
                    break
                await self._wait_for_next_cycle()
            except asyncio.CancelledError:
                self._cycle_active = False
                clear_cycle_id()
                break  # exit loop cleanly so shutdown log can emit
            except CircuitOpenError as exc:
                if await self._handle_circuit_open(exc):
                    break
            except Exception as exc:
                failure, previous_phase = await self._handle_cycle_failure(exc, _cycle_t0)
            else:
                # Happy-path cycle end — clear cycle_id before inter-cycle sleep
                clear_cycle_id()

        await self._shutdown_flush(previous_lifecycle_sig)

        self._emit(
            **build_runtime_event(
                category="shutdown",
                message="daemon stopped",
                graceful=failure is None,
                cycle_interrupted=self._stop_during_cycle,
                run_mode=self._run_mode,
            ),
        )
        if failure is not None:
            raise failure

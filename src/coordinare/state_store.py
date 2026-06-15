from __future__ import annotations

import os
import tempfile
import time
from datetime import datetime  # noqa: TC003 — Pydantic needs this at runtime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field, field_validator

if TYPE_CHECKING:
    from coordinare.metrics import CoordinareMetrics

logger = structlog.get_logger(__name__)

CURRENT_SCHEMA_VERSION: int = 9

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
    open_questions: list[str] = Field(default_factory=list)
    card_clarifications: list[dict] = Field(default_factory=list)
    relay_feedback: list[dict] = Field(default_factory=list)
    system_error_count: int = 0
    system_error_reason: str | None = None
    system_error_notified: bool = False
    requirements_changed: bool = False
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

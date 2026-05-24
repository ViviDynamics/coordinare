from __future__ import annotations

import os
import tempfile
import time
from datetime import datetime  # noqa: TC003 — Pydantic needs this at runtime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from coordinare.metrics import CoordinareMetrics

logger = structlog.get_logger(__name__)

CURRENT_SCHEMA_VERSION: int = 2

# Lowest schema_version we still know how to read.  v1 snapshots are upgraded
# in-memory at load time (065 Fix 7b: active_sessions added in v2; v1 snapshots
# simply restore with an empty active_sessions dict and rely on board re-adopt).
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
                    f"got {snapshot.schema_version}"
                ),
            )

        self.last_snapshot = snapshot
        return snapshot

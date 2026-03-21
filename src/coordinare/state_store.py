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

CURRENT_SCHEMA_VERSION: int = 1

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
    last_blocked_notified_at: datetime | None = None


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

        if snapshot.schema_version != CURRENT_SCHEMA_VERSION:
            raise StateLoadError(
                reason="schema_mismatch",
                detail=f"expected {CURRENT_SCHEMA_VERSION}, got {snapshot.schema_version}",
            )

        self.last_snapshot = snapshot
        return snapshot

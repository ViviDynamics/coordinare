"""AttemptLog — spec 141 attempt telemetry."""
from __future__ import annotations

import json
import tempfile
import uuid
from dataclasses import dataclass
from datetime import (
    UTC,
    datetime,
)
from typing import TYPE_CHECKING, Any, Literal

import structlog

if TYPE_CHECKING:
    from pathlib import Path

logger = structlog.get_logger(__name__)

Verdict = Literal["pass", "fail", "error", "timeout"]
VerdictSource = Literal["qa_role", "human", "grader", "system"]
TerminalState = Literal["merged", "blocked", "abandoned"]

_VERDICT_MAP: dict[str, Verdict] = {
    # Identity entries — makes map_verdict idempotent so double-application at a
    # call site is harmless rather than silently downgrading to "error".
    "pass": "pass",
    "fail": "fail",
    "error": "error",
    "timeout": "timeout",
    # Passing markers (EXPECTED_STAGE_MARKER values in monitor_performer.py)
    "approved": "pass",
    "qa_passed": "pass",
    "security_passed": "pass",
    "docs_committed": "pass",
    # 412: advance-with-note verdicts -- the stage completed, so the attempt
    # closes as a pass even though nothing was reviewed/scanned.
    "nothing_to_review": "pass",
    "nothing_to_scan": "pass",
    "not_applicable": "pass",
    # Failure markers — QA/review rejected the work
    "changes_requested": "fail",
    "qa_failed": "fail",
    "security_failed": "fail",
    # Error markers — infra/env failure, not task difficulty
    "blocked": "error",
    "env_blocked": "error",
    "qa_env_blocked": "error",
    # Timeout markers
    "session_expired": "timeout",
    "token_limit": "timeout",
    "idle_timeout": "timeout",
    # partial_progress / working are non-terminal — callers must not call
    # close_attempt for these markers; they fall through to "error" as a
    # safety net if they do.
}


def map_verdict(raw: str) -> Verdict:
    """Map a monitor_performer stage marker to the AttemptRecord taxonomy (spec 141 FR-007).

    Unknown values map to "error", never "fail" — an unrecognised value must not
    masquerade as task difficulty.
    """
    return _VERDICT_MAP.get(raw.strip().lower(), "error")


def _dir_is_writable(path: Path) -> bool:
    """Probe real write access by creating and removing a temporary file.

    os.access is not sufficient here: it only checks permission bits, which a
    root process in a container satisfies even on a read-only filesystem.
    Creating a file surfaces the actual errno — EROFS on a read-only ConfigMap
    mount (issue 493), EACCES on a permission-bounded directory.
    """
    try:
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix=".coordinare-attempt-probe", dir=path):
            return True
    except OSError:
        return False


def resolve_writable_log_dir(candidate: Path, fallback: Path) -> Path:
    """Return candidate when it is writable, else fall back to fallback (issue 493).

    AttemptLog must land on a writable filesystem or every append fails and
    attempt history is lost. The candidate is probed with a real file create;
    any OSError selects the fallback. Callers keep the existing anchor when it
    works, so bare-metal deployments are unaffected.
    """
    if _dir_is_writable(candidate):
        return candidate
    logger.warning(
        "attempt_log.dir_unwritable",
        candidate=str(candidate),
        fallback=str(fallback),
    )
    return fallback


@dataclass(frozen=True)
class _OpenAttempt:
    started_at: str | None
    task_id: str
    log_path: Path


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


class AttemptLog:
    """Append-only JSONL log of performer attempt events.

    Writes to log_dir/YYYY-MM-DD.jsonl (UTC date). Two rows per attempt:
    a start row (open_attempt) and an end row (close_attempt), joined by
    attempt_id. Write failures are swallowed after logging so that telemetry
    can never interrupt card processing.
    """

    def __init__(self, log_dir: Path) -> None:
        self._log_dir = log_dir
        self._open_attempts: dict[str, _OpenAttempt] = {}

    def reopen_attempt(self, attempt_id: str, task_id: str, log_path: Path) -> None:
        """Restore an in-flight attempt from PersistedSession after a daemon restart (A-008).

        started_at is unknown after restart — wall_time_s in the end row will be null.
        No-ops if the attempt_id is already registered (idempotent).
        """
        if attempt_id not in self._open_attempts:
            self._open_attempts[attempt_id] = _OpenAttempt(
                started_at=None,  # unknown after restart; wall_time_s will be null
                task_id=task_id,
                log_path=log_path,
            )

    def log_path_for(self, attempt_id: str) -> str | None:
        """Return the log path for an open attempt as a string (for persistence per A-008)."""
        open_attempt = self._open_attempts.get(attempt_id)
        return str(open_attempt.log_path) if open_attempt is not None else None

    def _log_path(self) -> Path:
        date = datetime.now(UTC).strftime("%Y-%m-%d")
        return self._log_dir / f"{date}.jsonl"

    def _append(self, row: dict[str, Any], path: Path) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")
        except Exception as exc:
            logger.warning("attempt_log.write_failed", path=str(path), error=str(exc))

    def open_attempt(
        self,
        task_id: str,
        routing_reason: str,
        parent_attempt_id: str | None = None,
        model_tier: str | None = None,
        model_name: str | None = None,
        spec_word_count: int | None = None,
        spec_has_acceptance_tests: bool | None = None,
        repo_area: str | None = None,
        source: str = "live",
    ) -> str:
        attempt_id = str(uuid.uuid4())
        started_at = _utcnow()
        log_path = self._log_path()
        self._open_attempts[attempt_id] = _OpenAttempt(
            started_at=started_at,
            task_id=task_id,
            log_path=log_path,
        )
        row = {
            "schema_version": 1,
            "attempt_id": attempt_id,
            "task_id": task_id,
            "parent_attempt_id": parent_attempt_id,
            "model_tier": model_tier,
            "model_name": model_name,
            "routing_reason": routing_reason,
            "started_at": started_at,
            "ended_at": None,
            "wall_time_s": None,
            "tokens_in": None,
            "tokens_out": None,
            "verdict": None,
            "verdict_source": None,
            "terminal_state": None,
            "spec_word_count": spec_word_count,
            "spec_has_acceptance_tests": spec_has_acceptance_tests,
            "repo_area": repo_area,
            "source": source,
        }
        self._append(row, log_path)
        return attempt_id

    def close_attempt(
        self,
        attempt_id: str,
        verdict: Verdict,
        verdict_source: VerdictSource,
        terminal_state: TerminalState | None = None,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        model_name: str | None = None,
        source: str = "live",
    ) -> None:
        verdict = map_verdict(verdict)
        ended_at = _utcnow()
        open_attempt = self._open_attempts.pop(attempt_id, None)
        if open_attempt is None:
            logger.warning("attempt_log.close_unknown_attempt", attempt_id=attempt_id)
            return
        started_at = open_attempt.started_at if open_attempt else None
        task_id = open_attempt.task_id if open_attempt else None
        log_path = open_attempt.log_path if open_attempt else self._log_path()
        wall_time_s: float | None = None
        if started_at is not None:
            try:
                start_dt = datetime.fromisoformat(started_at)
                end_dt = datetime.fromisoformat(ended_at)
                wall_time_s = (end_dt - start_dt).total_seconds()
            except Exception:
                logger.debug(
                    "attempt_log.wall_time_unparseable",
                    attempt_id=attempt_id,
                    started_at=started_at,
                )
        row = {
            "schema_version": 1,
            "attempt_id": attempt_id,
            "task_id": task_id,
            "started_at": started_at,
            "ended_at": ended_at,
            "wall_time_s": wall_time_s,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "verdict": verdict,
            "verdict_source": verdict_source,
            "terminal_state": terminal_state,
            "model_name": model_name,
            "source": source,
        }
        self._append(row, log_path)

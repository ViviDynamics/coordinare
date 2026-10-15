"""266: the env bootstrap's bounded history, failure text and feed entries.

The history is per process and bounded: each symphony keeps its last ``HISTORY_LIMIT`` attempts, and
run snapshots are persisted in the event store. The daemon opens an attempt when a bootstrap is dispatched and the
env cache closes it when the bootstrap completes. The status derivation in ``bootstrap_status`` reads the
history; the recorders here never decide anything, so a failure to record cannot change a bootstrap's outcome.

Every string that can carry verify output passes through ``redact_state_value`` before it is kept.
"""

from __future__ import annotations

import contextlib
from collections import deque
from dataclasses import dataclass, field, fields
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import structlog

from coordinare.lib.redaction import clip_session_error, redact_state_value

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

logger = structlog.get_logger(__name__)

# The attempts kept per symphony. A long-running daemon keeps this many, never more.
HISTORY_LIMIT = 10
# The verify output lines kept with a failure, counted from the end.
TAIL_LINES = 6
# One output line, after redaction, is clipped to this many characters.
MAX_LINE_CHARS = 240
# The first characters of a commit, as shown in words.
SHORT_SHA_CHARS = 7
# The stage the feed entries carry, so a feed row reads as a bootstrap, not a card.
ACTIVITY_STAGE = "env_bootstrap"
RUN_SNAPSHOT_KIND = "bootstrap_run"

OUTCOME_RUNNING = "running"
OUTCOME_SUCCEEDED = "succeeded"
OUTCOME_FAILED = "failed"

# Words that mark the line a verify run failed on. The first line with one is the failing line.
_ERROR_MARKERS = (
    "error", "not found", "could not", "no such", "fatal", "failed", "non-zero",
    "cannot", "unable", "missing", "exit",
)

# The feed's activity types for the bootstrap. The browser has a label, a summary and an icon for each.
ACT_DISPATCHED = "env_bootstrap_dispatched"
ACT_FAILED = "env_bootstrap_failed"
ACT_RETRY = "env_bootstrap_retry"
ACT_BUDGET_RESET = "env_bootstrap_budget_reset"
ACT_READY = "env_bootstrap_ready"
ACT_EXHAUSTED = "env_bootstrap_exhausted"
ACTIVITY_TYPES = (
    ACT_DISPATCHED, ACT_FAILED, ACT_RETRY, ACT_BUDGET_RESET, ACT_READY, ACT_EXHAUSTED,
)


@dataclass(slots=True)
class BootstrapAttempt:
    """One bootstrap run, from its dispatch to its completion."""

    number: int
    attempt: int
    commit: str
    started_at: datetime
    backend: str
    model: str
    performer_id: str
    ended_at: datetime | None = None
    outcome: str = OUTCOME_RUNNING
    reason: str | None = None
    infra: bool = False
    job_id: str | None = None
    pod: str | None = None
    verifying: bool = False
    run_id: str = field(default_factory=lambda: uuid4().hex)
    manifest: list[dict[str, Any]] = field(default_factory=list)
    verify_verdict: str | None = None
    reason_tail: list[str] = field(default_factory=list)
    last_activity_at: datetime | None = None
    restored: bool = False
    seen_events: set[str] = field(default_factory=set, repr=False)


@dataclass(frozen=True, slots=True)
class AttemptStart:
    """What a dispatched bootstrap is: the commit it is for, its budget number and where it runs."""

    commit: str
    started_at: datetime
    attempt: int
    backend: str
    model: str
    performer_id: str


@dataclass(frozen=True, slots=True)
class AttemptEnd:
    """How a bootstrap ended: when, whether it passed, and the verify output if it did not."""

    ended_at: datetime
    succeeded: bool
    reason: str | None
    commit: str
    attempt: int
    infra: bool = False


@dataclass(frozen=True, slots=True)
class BudgetNow:
    """The budget when an attempt ended: its size, whether this failure used it up, and when a retry may start."""

    max_attempts: int
    exhausted_now: bool
    retry_at: datetime | None


def failure_detail(text: object) -> tuple[str | None, list[str]]:
    """The failing line of a verify output, and the last few lines of it, both redacted and clipped.

    The failing line is the first line that carries an error word, else the last non-empty line.
    Returns (None, []) when there is no text.
    """
    redacted = redact_state_value(str(text)) if text else ""
    lines = [line.strip() for line in str(redacted).splitlines() if line.strip()]
    if not lines:
        return None, []
    failing = next(
        (line for line in lines if any(marker in line.lower() for marker in _ERROR_MARKERS)),
        lines[-1],
    )
    tail = [clip_session_error(line[:MAX_LINE_CHARS]) for line in lines[-TAIL_LINES:]]
    return clip_session_error(failing[:MAX_LINE_CHARS]), tail


class BootstrapHistory:
    """The recent attempts of every symphony, newest last, each symphony bounded by ``limit``."""

    def __init__(self, limit: int = HISTORY_LIMIT) -> None:
        self._limit = limit
        self._by_symphony: dict[str, deque[BootstrapAttempt]] = {}
        self._counter = 0

    def _attempts(self, symphony: str) -> deque[BootstrapAttempt]:
        attempts = self._by_symphony.get(symphony)
        if attempts is None:
            attempts = deque(maxlen=self._limit)
            self._by_symphony[symphony] = attempts
        return attempts

    def begin(self, symphony: str, start: AttemptStart) -> BootstrapAttempt:
        """Open an attempt for a dispatched bootstrap."""
        self._counter += 1
        record = BootstrapAttempt(
            number=self._counter,
            attempt=start.attempt,
            commit=start.commit,
            started_at=start.started_at,
            backend=start.backend,
            model=start.model,
            performer_id=start.performer_id,
        )
        self._attempts(symphony).append(record)
        return record

    def open_attempt(self, symphony: str) -> BootstrapAttempt | None:
        """The symphony's attempt still running, or None."""
        attempts = self._by_symphony.get(symphony)
        if not attempts:
            return None
        newest = attempts[-1]
        return newest if newest.ended_at is None and not newest.restored else None

    def restore(self, symphony: str, records: list[BootstrapAttempt]) -> None:
        """Restore newest first snapshots, without treating an old process's job as live."""
        attempts = self._attempts(symphony)
        attempts.clear()
        for record in reversed(records[:self._limit]):
            record.restored = True
            attempts.append(record)
            self._counter = max(self._counter, record.number)

    def attach(self, symphony: str, *, job_id: str | None, pod: str | None) -> None:
        """Record the job and the pod (or container) the running attempt was dispatched to."""
        record = self.open_attempt(symphony)
        if record is None:
            return
        record.job_id = job_id or None
        record.pod = pod or None

    def mark_verifying(self, symphony: str) -> None:
        """The running attempt's workload has finished, and its verify is running now."""
        record = self.open_attempt(symphony)
        if record is not None:
            record.verifying = True

    def finish(self, symphony: str, end: AttemptEnd) -> BootstrapAttempt:
        """Close the running attempt. With none running, record a closed one that began and ended at once."""
        record = self.open_attempt(symphony)
        if record is None:
            record = self.begin(
                symphony,
                AttemptStart(
                    commit=end.commit, started_at=end.ended_at, attempt=end.attempt,
                    backend="", model="", performer_id="",
                ),
            )
        record.ended_at = end.ended_at
        record.outcome = OUTCOME_SUCCEEDED if end.succeeded else OUTCOME_FAILED
        record.reason = None if end.succeeded else failure_detail(end.reason)[0]
        if end.reason:
            record.reason_tail = failure_detail(end.reason)[1]
        record.infra = end.infra and not end.succeeded
        record.verifying = False
        return record

    def recent(self, symphony: str, limit: int) -> list[BootstrapAttempt]:
        """The symphony's last ``limit`` attempts, newest first."""
        attempts = self._by_symphony.get(symphony)
        if not attempts or limit <= 0:
            return []
        return list(reversed(list(attempts)[-limit:]))


def history_of(state: Mapping[str, Any]) -> BootstrapHistory | None:
    """The daemon's history, or None when the daemon has not set one up (a unit test state, for one)."""
    history = state.get("bootstrap_history")
    return history if isinstance(history, BootstrapHistory) else None


def begin_attempt(state: Mapping[str, Any], symphony: str, start: AttemptStart) -> None:
    """Open an attempt in the daemon's history, if there is one. Never raises."""
    history = history_of(state)
    if history is None:
        return
    with contextlib.suppress(Exception):
        record = history.begin(symphony, start)
        cache = (state.get("env_cache") or {}).get(symphony)
        record.manifest = redact_state_value(getattr(cache, "manifest_items", []))
        persist_attempt(state, symphony, record)


def attach_job(state: Mapping[str, Any], symphony: str, *, job_id: str | None, pod: str | None) -> None:
    """Record the job and pod of the running attempt, if the daemon keeps a history. Never raises."""
    history = history_of(state)
    if history is None:
        return
    with contextlib.suppress(Exception):
        history.attach(symphony, job_id=job_id, pod=pod)
        persist_attempt(state, symphony, history.open_attempt(symphony))


def mark_verifying(state: Mapping[str, Any], symphony: str) -> None:
    """Say the running attempt is verifying, if the daemon keeps a history. Never raises."""
    history = history_of(state)
    if history is None:
        return
    with contextlib.suppress(Exception):
        history.mark_verifying(symphony)
        persist_attempt(state, symphony, history.open_attempt(symphony))


def persist_attempt(state: Mapping[str, Any], symphony: str, record: BootstrapAttempt | None) -> None:
    """Persist one immutable run snapshot through the event store's redaction boundary."""
    store = state.get("event_store")
    if store is None or record is None:
        return
    with contextlib.suppress(Exception):
        data = {item.name: getattr(record, item.name) for item in fields(record) if item.name != "seen_events"}
        for key in ("started_at", "ended_at", "last_activity_at"):
            value = data[key]
            data[key] = value.isoformat() if value is not None else None
        store.emit({"symphony": symphony, "run_id": record.run_id, "stage": ACTIVITY_STAGE,
                    "source": "bootstrap", "kind": RUN_SNAPSHOT_KIND, "data": data})


def record_activity(state: Mapping[str, Any], symphony: str, activity_type: str, text: str) -> None:
    """Put one bootstrap event in the activity feed. Best effort: a feed that cannot be written never fails a bootstrap."""
    log = state.get("activity_log")
    if log is None:
        return
    try:
        history = history_of(state)
        recent = history.recent(symphony, 1) if history is not None else []
        record = recent[0] if recent else None
        log.record(
            activity_type=activity_type,
            card_id="",
            card_title=symphony,
            stage=ACTIVITY_STAGE,
            text=redact_state_value(text),
            symphony=symphony,
            session_id=record.run_id if record is not None else "",
            performer_id=record.performer_id if record is not None else "",
        )
    except Exception as exc:
        logger.warning("env_bootstrap.activity_not_recorded", symphony=symphony, error_type=type(exc).__name__)


def report_completion(state: Mapping[str, Any], symphony: str, end: AttemptEnd, budget: BudgetNow) -> None:
    """Close the attempt and put its outcome in the feed: ready, failed, a retry, or exhausted.

    ``end.attempt`` is the budget number the attempt was dispatched under, used when no attempt was
    open. ``budget.retry_at`` is when the next attempt may start, when one is scheduled; it is None on
    success and once the budget is exhausted.
    """
    history = history_of(state)
    record = None
    if history is not None:
        with contextlib.suppress(Exception):
            record = history.finish(symphony, end)
            persist_attempt(state, symphony, record)
    number = record.attempt if record is not None else end.attempt
    failing, _ = failure_detail(end.reason)
    if end.succeeded:
        record_activity(state, symphony, ACT_READY, f"Ready for commit {short_commit(end.commit) or 'unknown'}")
        return
    if budget.exhausted_now:
        record_activity(
            state, symphony, ACT_EXHAUSTED,
            f"Budget exhausted after {number} failed runs; a new commit to the env spec files resets it."
            + (f" Last failure: {failing}" if failing else ""),
        )
    else:
        verb = "Verify failed" if failing and "verif" in str(end.reason).lower() else "Bootstrap failed"
        record_activity(
            state, symphony, ACT_FAILED,
            f"{verb} on run {number} of {budget.max_attempts}" + (f": {failing}" if failing else "."),
        )
    if budget.retry_at is not None and not budget.exhausted_now:
        record_activity(
            state, symphony, ACT_RETRY,
            f"Retry scheduled for run {number + 1} of {budget.max_attempts} at {budget.retry_at.strftime('%H:%M UTC')}.",
        )


def short_commit(commit: object) -> str:
    """The first characters of a commit or a combined spec SHA, for a label. Empty for no value."""
    text = str(commit or "")
    return text.split(":", 1)[0][:SHORT_SHA_CHARS]

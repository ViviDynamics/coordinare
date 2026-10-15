"""266: one symphony's env bootstrap in words, derived from the daemon's own state.

The derivation reads the env-cache state (``EnvCacheState``: the in-flight flag, the run count, the
budget, the last outcome and its error), the retry backoff (``env_cache.BOOTSTRAP_RETRY_COOLDOWN_S``
doubled per failure), the running run and the bounded history. It writes nothing and changes no
decision: the dispatch gate and the retry logic keep their own rules, and this only says them out loud.

User-facing text says "run", never "attempt" (the #141 wording rule). The field names keep ``attempt``.

The result is one of these states, and the dashboard, the card's waiting reason and the health row all
read the same words from it:

- ``not_configured``: the symphony has no env bootstrap performer.
- ``not_started``: no bootstrap has run for it yet.
- ``bootstrapping``: a bootstrap is running (run N of M).
- ``verifying``: the bootstrap's workload has finished and its verify is running.
- ``ready``: the cache passed for a commit, since a time.
- ``failed``: the last run failed, and its retry is due now.
- ``waiting_to_retry``: the last run failed, and the retry is scheduled for a later time.
- ``budget_exhausted``: the runs are used up. A new commit to the env spec files resets it.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from coordinare.models.env_cache import EnvCacheState
from coordinare.services.bootstrap_history import (
    BOOTSTRAP_CANCEL_COOLDOWN_S,
    OUTCOME_CANCELLED,
    OUTCOME_RUNNING,
    TAIL_LINES,
    BootstrapAttempt,
    failure_detail,
    history_of,
    short_commit,
)
from coordinare.services.env_cache import BOOTSTRAP_RETRY_COOLDOWN_S

if TYPE_CHECKING:
    from collections.abc import Mapping

STATE_NOT_CONFIGURED = "not_configured"
STATE_NOT_STARTED = "not_started"
STATE_BOOTSTRAPPING = "bootstrapping"
STATE_VERIFYING = "verifying"
STATE_READY = "ready"
STATE_FAILED = "failed"
STATE_WAITING = "waiting_to_retry"
STATE_EXHAUSTED = "budget_exhausted"
STATE_PASSED_NOT_READY = "passed_not_ready"
STATE_STOP_UNCONFIRMED = "stop_unconfirmed"
STATE_CANCELLED = "cancelled"

#: The states a card waits on. Ready and not configured hold nothing.
HOLD_STATES = frozenset({
    STATE_NOT_STARTED, STATE_BOOTSTRAPPING, STATE_VERIFYING, STATE_FAILED, STATE_WAITING, STATE_EXHAUSTED,
    STATE_PASSED_NOT_READY, STATE_STOP_UNCONFIRMED, STATE_CANCELLED,
})

#: The reason a card held by the env bootstrap carries in session_skip_reasons.
ENV_BOOTSTRAP_HOLD = "env_bootstrap"
#: The runs the snapshot carries per symphony, newest first.
RECENT_LIMIT = 5
#: The budget the daemon uses when its config does not name one (env_cache._spend_breaker_attempt).
DEFAULT_MAX_ATTEMPTS = 3

RESET_HINT_VERDICT = "A new commit to the env spec files resets the budget."
RESET_HINT_INFRA = (
    "The docker daemon or the performer image must be fixed, or a new commit to the env spec files, "
    "to reset the budget."
)
HEALTH_FAILED_REASON = "The performer environment failed a health check and is being rebuilt."
NOT_STARTED_LABEL = "Not started"
#: The waiting reason of a card held while the gate is closed but the bootstrap view has no words for it.
GATE_HOLD_TEXT = "Waiting for the environment bootstrap to finish for the current commit"

# The fields the derivation reads, with the value a missing one reads as. A stand-in record, or one
# from an older snapshot, is read through these defaults, so a missing field never breaks a view.
_CACHE_DEFAULTS: dict[str, Any] = {
    "readme_sha": None,
    "bootstrap_in_flight": False,
    "bootstrap_attempts": 0,
    "bootstrap_exhausted": False,
    "runtime_health_failed": False,
    "last_bootstrap_succeeded": None,
    "cache_dir_ready": False,
    "last_bootstrap_at": None,
    "last_bootstrap_error": None,
    "infra_failures": 0,
}


def _as_state(cache: object) -> EnvCacheState | None:
    """The cache record as an ``EnvCacheState``, reading each missing field as its default. None stays None."""
    if cache is None or isinstance(cache, EnvCacheState):
        return cache
    fields = {name: getattr(cache, name, default) for name, default in _CACHE_DEFAULTS.items()}
    return EnvCacheState.model_construct(**fields)


def max_attempts_of(state: Mapping[str, Any]) -> int:
    """The bootstrap budget the daemon runs with (``env_bootstrap_max_attempts``), or the default."""
    coordinare = state.get("coordinare_config")
    global_config = getattr(coordinare, "global_config", None)
    value = getattr(global_config, "env_bootstrap_max_attempts", None)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else DEFAULT_MAX_ATTEMPTS


def _count_words(count: int, noun: str) -> str:
    """"1 failed run", "3 failed runs": the count with its noun in the right form."""
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _clock(value: datetime) -> str:
    return value.strftime("%H:%M UTC")


def _elapsed(started: datetime | None, now: datetime) -> int | None:
    if started is None:
        return None
    return max(0, int((now - started).total_seconds()))


def _blank(state: str, label: str, max_attempts: int) -> dict[str, Any]:
    """Every key the view carries, empty, so a reader never has to check for a missing one."""
    return {
        "state": state,
        "label": label,
        "attempt": None,
        "max_attempts": max_attempts,
        "commit": None,
        "started_at": None,
        "elapsed_seconds": None,
        "next_attempt_at": None,
        "ready_since": None,
        "reason": None,
        "reason_tail": [],
        "reset_hint": None,
        "backend": None,
        "model": None,
        "performer_id": None,
        "pod": None,
        "job_id": None,
        "infra_failures": 0,
        "history": [],
    }


def _reason_fields(view: dict[str, Any], text: object) -> None:
    failing, tail = failure_detail(text)
    view["reason"] = failing
    view["reason_tail"] = tail[-TAIL_LINES:]


def _running_fields(view: dict[str, Any], running: BootstrapAttempt | None, now: datetime) -> None:
    if running is None:
        return
    view["started_at"] = _iso(running.started_at)
    view["elapsed_seconds"] = _elapsed(running.started_at, now)
    view["backend"] = running.backend or None
    view["model"] = running.model or None
    view["performer_id"] = running.performer_id or None
    view["pod"] = running.pod
    view["job_id"] = running.job_id
    view["run_id"] = running.run_id


def derive_status(
    cache: EnvCacheState | None,
    *,
    configured: bool,
    running: BootstrapAttempt | None,
    max_attempts: int,
    now: datetime,
) -> dict[str, Any]:
    """The one state a symphony's env bootstrap is in, with the words and figures that describe it."""
    cache = _as_state(cache)
    if not configured:
        return _blank(STATE_NOT_CONFIGURED, "Not configured", max_attempts)
    if cache is None:
        return _blank(STATE_NOT_STARTED, NOT_STARTED_LABEL, max_attempts)
    view = _blank(STATE_NOT_STARTED, NOT_STARTED_LABEL, max_attempts)
    view["commit"] = short_commit(cache.readme_sha) or None
    view["infra_failures"] = int(cache.infra_failures or 0)
    _apply_cache(view, cache, running, max_attempts, now)
    if view["infra_failures"] and view["state"] not in (STATE_EXHAUSTED, STATE_READY):
        # Verify runs spend the budget and are numbered; infrastructure failures do not, so they are shown beside the run.
        infra = _count_words(view["infra_failures"], "infrastructure failure")
        view["label"] = f"{view['label']} · {infra}"
    return view


def _apply_cache(
    view: dict[str, Any],
    cache: EnvCacheState,
    running: BootstrapAttempt | None,
    max_attempts: int,
    now: datetime,
) -> None:
    """Set the state of a configured symphony that has a cache record, in the order the daemon decides it."""
    failures = int(cache.bootstrap_attempts or 0)
    if cache.bootstrap_in_flight:
        run = min(failures + 1, max_attempts)
        verifying = running is not None and running.verifying
        view["state"] = STATE_VERIFYING if verifying else STATE_BOOTSTRAPPING
        view["attempt"] = run
        word = "Verifying" if verifying else "Bootstrapping"
        view["label"] = f"{word}, run {run} of {max_attempts}"
        _running_fields(view, running, now)
        if running is not None and running.stop_unconfirmed:
            view["state"] = STATE_STOP_UNCONFIRMED
            view["label"] = "Stop unconfirmed"
            _reason_fields(view, running.reason)
        return
    if cache.bootstrap_exhausted:
        _set_exhausted(view, cache, failures)
        return
    if cache.runtime_health_failed:
        view["state"] = STATE_FAILED
        view["label"] = "Failed a health check"
        view["reason"] = HEALTH_FAILED_REASON
        return
    if cache.last_bootstrap_succeeded is True and cache.cache_dir_ready:
        view["state"] = STATE_READY
        view["label"] = f"Ready for commit {view['commit']}" if view["commit"] else "Ready"
        view["ready_since"] = _iso(cache.last_bootstrap_at)
        return
    if cache.last_bootstrap_succeeded is True:
        # The run passed, but the cache is not marked ready yet (the verify has not confirmed it).
        view["state"] = STATE_PASSED_NOT_READY
        view["label"] = "Passed, not ready"
        view["ready_since"] = _iso(cache.last_bootstrap_at)
        return
    if cache.last_bootstrap_succeeded is False:
        _set_failed(view, cache, failures, max_attempts, now)
        return
    if cache.readme_sha is not None:
        # A new commit cleared the previous commit's outcome: the view names that wait, not the old failure.
        view["state"] = STATE_NOT_STARTED
        view["label"] = "Waiting to bootstrap the new commit"
        view["commit"] = None


def _set_exhausted(view: dict[str, Any], cache: EnvCacheState, failures: int) -> None:
    """The budget is used up. The label says whether the verdict or the infrastructure budget ran out."""
    view["state"] = STATE_EXHAUSTED
    view["attempt"] = failures
    infra_only = bool(cache.infra_failures) and not failures
    if infra_only:
        view["label"] = f"Budget exhausted after {_count_words(cache.infra_failures, 'infrastructure failure')}"
    else:
        view["label"] = f"Budget exhausted after {_count_words(failures, 'failed run')}"
    view["reset_hint"] = RESET_HINT_INFRA if infra_only else RESET_HINT_VERDICT
    _reason_fields(view, cache.last_bootstrap_error)


def _set_failed(
    view: dict[str, Any],
    cache: EnvCacheState,
    failures: int,
    max_attempts: int,
    now: datetime,
) -> None:
    """The last run failed. Its retry is waiting for the backoff, or is due now."""
    cooldown = timedelta(seconds=BOOTSTRAP_RETRY_COOLDOWN_S * 2 ** (failures + int(cache.infra_failures or 0)))
    due = cache.last_bootstrap_at + cooldown if cache.last_bootstrap_at is not None else None
    view["next_attempt_at"] = _iso(due)
    _reason_fields(view, cache.last_bootstrap_error)
    if due is not None and due > now:
        view["state"] = STATE_WAITING
        view["attempt"] = min(failures + 1, max_attempts)
        view["label"] = f"Waiting to retry, run {view['attempt']} of {max_attempts}"
        return
    view["state"] = STATE_FAILED
    view["attempt"] = min(max(failures, 1), max_attempts)
    view["label"] = f"Failed on run {view['attempt']} of {max_attempts}"


def attempt_view(record: BootstrapAttempt, now: datetime) -> dict[str, Any]:
    """One history row, in the words and figures the Symphonies page shows."""
    ended = record.ended_at
    duration = _elapsed(record.started_at, ended if ended is not None else now)
    interrupted = record.restored and ended is None
    failing, tail = failure_detail(record.reason)
    return {
        "number": record.number,
        "run_id": record.run_id,
        "attempt": record.attempt,
        "commit": short_commit(record.commit) or None,
        "started_at": _iso(record.started_at),
        "ended_at": _iso(ended),
        "duration_seconds": None if interrupted else duration,
        "outcome": "interrupted" if interrupted else record.outcome,
        "running": record.outcome == OUTCOME_RUNNING and not record.restored,
        "reason": failing,
        "infra": record.infra,
        "cancelled_by": record.cancelled_by,
        "stop_unconfirmed": record.stop_unconfirmed,
        "backend": record.backend or None,
        "model": record.model or None,
        "job_id": record.job_id,
        "pod": record.pod,
        "reason_tail": record.reason_tail or tail,
    }


def symphony_view(
    state: Mapping[str, Any],
    symphony: str,
    *,
    configured: bool,
    now: datetime,
) -> dict[str, Any]:
    """One symphony's bootstrap view, read from the daemon state: the state, the figures and the recent runs."""
    cache = (state.get("env_cache") or {}).get(symphony)
    history = history_of(state)
    running = history.open_attempt(symphony) if history is not None else None
    view = derive_status(
        cache, configured=configured, running=running,
        max_attempts=max_attempts_of(state), now=now,
    )
    recent = history.recent(symphony, RECENT_LIMIT) if history is not None else []
    if view["state"] in (STATE_WAITING, STATE_FAILED) and recent and recent[0].outcome == OUTCOME_CANCELLED:
        record = recent[0]
        view["state"] = STATE_CANCELLED
        view["label"] = "Cancelled"
        _reason_fields(view, record.reason)
        view["next_attempt_at"] = _iso(
            record.ended_at + timedelta(seconds=BOOTSTRAP_CANCEL_COOLDOWN_S) if record.ended_at else None,
        )
    view["history"] = [attempt_view(record, now) for record in recent]
    return view


_HOLD_WORDS = {
    STATE_NOT_STARTED: "Waiting for the environment bootstrap to start",
    STATE_STOP_UNCONFIRMED: "Environment bootstrap stop unconfirmed",
    STATE_CANCELLED: "Environment bootstrap cancelled",
    STATE_PASSED_NOT_READY: "Waiting for the environment bootstrap to finish for the current commit",
}


def hold_text(view: Mapping[str, Any]) -> str | None:
    """The waiting reason a card held by this bootstrap shows, or None when the bootstrap holds nothing."""
    state = view.get("state")
    if state not in HOLD_STATES:
        return None
    if state in _HOLD_WORDS:
        words = _HOLD_WORDS[state]
        if state in (STATE_CANCELLED, STATE_STOP_UNCONFIRMED) and view.get("reason"):
            words += f": {view['reason']}"
        return words
    run = view.get("attempt")
    budget = view.get("max_attempts")
    reason = str(view.get("reason") or "")
    if state in (STATE_BOOTSTRAPPING, STATE_VERIFYING):
        return f"Waiting for the environment bootstrap (run {run} of {budget})"
    if state == STATE_WAITING:
        due = view.get("next_attempt_at")
        when = _clock(datetime.fromisoformat(str(due))) if due else "the next check"
        return f"Waiting for the environment bootstrap to retry (run {run} of {budget}, next at {when})" + (
            f": {reason}" if reason else ""
        )
    if state == STATE_FAILED:
        return f"Environment bootstrap failed (run {run} of {budget})" + (f": {reason}" if reason else "")
    return (
        f"Environment bootstrap budget exhausted after {run} failed runs"
        + (f": {reason}" if reason else "")
        + f". {view.get('reset_hint') or RESET_HINT_VERDICT}"
    )

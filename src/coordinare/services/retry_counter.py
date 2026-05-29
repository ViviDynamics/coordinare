"""Per-(card_id, performer_stage) idle-timeout retry counter (spec 076 FR-019).

Backed by ``PersistedSession.idle_timeout_retries`` so the counter
survives daemon restart (clarification Q3 / FR-019 — 2 retries per
rolling 24h window, then BLOCKED).

State shape (per data-model §4):

    state["active_sessions"][card_id]["idle_timeout_retries"][f"{card_id}:{stage}"]
        = IdleTimeoutRetryRecord(
            card_id=...,
            performer_stage=...,
            window_start_at=datetime,
            attempt_count=int,
            last_at=datetime|None,
          )

Stored as a plain dict in the session for JSON portability; the typed
``IdleTimeoutRetryRecord`` is the canonical model in
``dispatcher_dedup_models``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

import structlog

if TYPE_CHECKING:
    from coordinare.graph.state import CoordinareState

logger = structlog.get_logger(__name__)

RetryDecision = Literal["retry", "block"]


def _key(card_id: str, performer_stage: str, suffix: str = "") -> str:
    """Build the per-(card, stage[, error-class]) retry-counter key.

    ``suffix`` namespaces distinct terminal-error classes within the
    same persisted dict.  Idle-timeout uses the bare
    ``f"{card_id}:{stage}"`` key (suffix="") for backward compatibility;
    empty-output uses ``f"{card_id}:{stage}:empty_output"``.  Different
    keys → independent counters, no schema change.
    """
    return f"{card_id}:{performer_stage}{suffix}"


def _get_retries_dict(state: CoordinareState, card_id: str) -> dict[str, dict[str, Any]]:
    """Return the (mutable) per-session ``idle_timeout_retries`` dict.

    Creates it if missing so callers can write through unconditionally.
    """
    sessions = state.get("active_sessions") or {}
    if not isinstance(sessions, dict):
        # Fall back to state root for legacy single-card shape
        return state.setdefault("idle_timeout_retries", {})  # type: ignore[no-any-return]
    sess = sessions.get(card_id)
    if not isinstance(sess, dict):
        return state.setdefault("idle_timeout_retries", {})  # type: ignore[no-any-return]
    retries = sess.get("idle_timeout_retries")
    if not isinstance(retries, dict):
        retries = {}
        sess["idle_timeout_retries"] = retries
    return retries


def _record_retry(
    state: CoordinareState,
    card_id: str,
    performer_stage: str,
    *,
    key_suffix: str,
    event_name: str,
    budget: int,
    window_hours: int,
) -> RetryDecision:
    """Shared rolling-window retry-counter core.

    Increments a per-(card, stage, error-class) counter inside a rolling
    ``window_hours`` window and returns ``"retry"`` while the count is
    ``<= budget``, else ``"block"``.  Used by both ``record_idle_timeout``
    (FR-019) and ``record_empty_output`` (empty-output churn cap).
    """
    key = _key(card_id, performer_stage, key_suffix)
    retries = _get_retries_dict(state, card_id)
    record = retries.get(key)
    now = datetime.now(UTC)

    if not isinstance(record, dict):
        record = _new_record(card_id, performer_stage, now)
    else:
        window_start = _parse_dt(record.get("window_start_at")) or now
        elapsed = now - window_start
        if elapsed > timedelta(hours=window_hours):
            # Window expired — start a fresh window
            record = _new_record(card_id, performer_stage, now)
        else:
            record["attempt_count"] = int(record.get("attempt_count", 0)) + 1
            record["last_at"] = now.isoformat()

    retries[key] = record
    attempt = int(record["attempt_count"])
    decision: RetryDecision = "retry" if attempt <= budget else "block"
    logger.info(
        event_name,
        card_id=card_id,
        performer_stage=performer_stage,
        attempt=attempt,
        budget=budget,
        decision=decision,
    )
    if decision == "block":
        logger.warning(
            f"{event_name}_exhausted",
            card_id=card_id,
            performer_stage=performer_stage,
            attempts=attempt,
        )
    return decision


def record_idle_timeout(
    state: CoordinareState,
    card_id: str,
    performer_stage: str,
    *,
    budget: int = 2,
    window_hours: int = 24,
) -> RetryDecision:
    """076 (T082, FR-019): per-(card, stage) idle-timeout retry counter.

    Returns ``"retry"`` while the count is ``<= budget``, else ``"block"``.
    Default ``budget=2`` matches clarification Q3 (2 retries → BLOCKED on
    the 3rd stall).
    """
    return _record_retry(
        state, card_id, performer_stage,
        key_suffix="",  # bare key — backward compatible with v7 idle records
        event_name="monitor_performer.idle_timeout",
        budget=budget, window_hours=window_hours,
    )


def record_empty_output(
    state: CoordinareState,
    card_id: str,
    performer_stage: str,
    *,
    budget: int = 1,
    window_hours: int = 24,
) -> RetryDecision:
    """076 (T171): per-(card, stage) empty-output retry counter.

    A performer that returns a terminal error with empty/unusable output
    (e.g. "Backend produced an empty architecture plan") is, on a card
    that reliably triggers it, a *deterministic* capability failure —
    retrying wastes a full stage run.  So the default budget is just
    ``1`` (one retry to absorb the rare genuine transient, then BLOCK
    for a human) — converting the previous infinite churn loop
    (block → requeue → re-dispatch → empty → …) into fail-fast-then-
    escalate.  Namespaced ``:empty_output`` key so it's independent of
    the idle-timeout counter.
    """
    return _record_retry(
        state, card_id, performer_stage,
        key_suffix=":empty_output",
        event_name="monitor_performer.empty_output",
        budget=budget, window_hours=window_hours,
    )


def attempts_in_window(
    state: CoordinareState,
    card_id: str,
    performer_stage: str,
    *,
    window_hours: int = 24,
) -> int:
    """Return the current attempt count within the rolling window."""
    key = _key(card_id, performer_stage)
    retries = _get_retries_dict(state, card_id)
    record = retries.get(key)
    if not isinstance(record, dict):
        return 0
    window_start = _parse_dt(record.get("window_start_at"))
    if window_start is None:
        return 0
    if datetime.now(UTC) - window_start > timedelta(hours=window_hours):
        return 0  # window expired — counter is effectively reset
    return int(record.get("attempt_count", 0))


def should_block(
    state: CoordinareState,
    card_id: str,
    performer_stage: str,
    *,
    budget: int = 2,
    window_hours: int = 24,
) -> bool:
    """True iff a subsequent idle-timeout would exceed the budget."""
    return attempts_in_window(
        state, card_id, performer_stage, window_hours=window_hours
    ) >= budget


def reset_if_window_expired(
    state: CoordinareState,
    card_id: str,
    performer_stage: str,
    *,
    window_hours: int = 24,
) -> None:
    """Reset the counter if its window has elapsed (called on each
    poll cycle as a maintenance step)."""
    key = _key(card_id, performer_stage)
    retries = _get_retries_dict(state, card_id)
    record = retries.get(key)
    if not isinstance(record, dict):
        return
    window_start = _parse_dt(record.get("window_start_at"))
    if window_start is None:
        retries.pop(key, None)
        return
    if datetime.now(UTC) - window_start > timedelta(hours=window_hours):
        retries.pop(key, None)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _new_record(card_id: str, performer_stage: str, now: datetime) -> dict[str, Any]:
    return {
        "card_id": card_id,
        "performer_stage": performer_stage,
        "window_start_at": now.isoformat(),
        "attempt_count": 1,
        "last_at": now.isoformat(),
    }


def _parse_dt(raw: Any) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        if raw.tzinfo is None:
            return raw.replace(tzinfo=UTC)
        return raw
    if isinstance(raw, str):
        try:
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt
        except ValueError:
            return None
    return None


__all__ = [
    "RetryDecision",
    "attempts_in_window",
    "record_empty_output",
    "record_idle_timeout",
    "reset_if_window_expired",
    "should_block",
]

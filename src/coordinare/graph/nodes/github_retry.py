from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from coordinare.resilience import CircuitOpenError
from coordinare.services.github import RateLimitedGitHubError, TransientGitHubError

if TYPE_CHECKING:
    from coordinare.graph.state import CoordinareState


_TRANSIENT_TEXT_MARKERS = (
    "temporary failure in name resolution",
    "name or service not known",
    "nodename nor servname provided",
    "failed to resolve",
    "dns",
    "connection reset",
    "connection refused",
    "service unavailable",
    "timed out",
    "timeout",
)


def _queue(state: CoordinareState) -> list[dict[str, Any]]:
    raw = state.get("github_retry_queue")
    if isinstance(raw, list):
        return raw
    q: list[dict[str, Any]] = []
    state["github_retry_queue"] = q
    return q


def _recompute_global_retry_after(state: CoordinareState) -> None:
    retry_ats: list[datetime] = []
    for entry in _queue(state):
        if not isinstance(entry, dict):
            continue
        retry_at = entry.get("retry_at")
        if isinstance(retry_at, datetime):
            retry_ats.append(retry_at)
    state["github_retry_after"] = min(retry_ats) if retry_ats else None


def is_transient_github_outage_error(exc: BaseException) -> bool:
    """Return True when *exc* indicates a retryable GitHub availability outage."""
    if isinstance(exc, (TransientGitHubError, RateLimitedGitHubError, CircuitOpenError, TimeoutError, OSError)):
        return True
    text = str(exc).strip().lower()
    return any(marker in text for marker in _TRANSIENT_TEXT_MARKERS)


def defer_github_operation(
    state: CoordinareState,
    *,
    operation: str,
    error: BaseException,
    base_delay_seconds: float = 30.0,
    max_delay_seconds: float = 600.0,
) -> dict[str, Any]:
    """Schedule a deferred retry for a GitHub operation with exponential backoff."""
    now = datetime.now(UTC)
    queue = _queue(state)
    existing = next(
        (entry for entry in queue if isinstance(entry, dict) and entry.get("operation") == operation),
        None,
    )
    prev_attempt = int(existing.get("attempt", 0)) if isinstance(existing, dict) else 0
    attempt = prev_attempt + 1
    if isinstance(error, RateLimitedGitHubError):
        delay = max(base_delay_seconds, float(error.retry_after))
    else:
        delay = min(max_delay_seconds, base_delay_seconds * (2 ** max(0, attempt - 1)))
    retry_at = now + timedelta(seconds=delay)
    new_entry = {
        "operation": operation,
        "attempt": attempt,
        "retry_at": retry_at,
        "error": str(error)[:500],
        "deferred_at": now,
    }
    if existing is None:
        queue.append(new_entry)
    else:
        queue[queue.index(existing)] = new_entry
    state["github_retry_queue"] = queue
    _recompute_global_retry_after(state)
    return new_entry


def clear_deferred_github_operation(state: CoordinareState, operation: str) -> None:
    """Clear deferred retry metadata for *operation* after a successful call."""
    queue = _queue(state)
    queue = [
        entry for entry in queue
        if not (isinstance(entry, dict) and entry.get("operation") == operation)
    ]
    state["github_retry_queue"] = queue
    _recompute_global_retry_after(state)


def get_deferred_github_operation(state: CoordinareState, operation: str) -> dict[str, Any] | None:
    """Return queued entry for *operation* if present."""
    for entry in _queue(state):
        if isinstance(entry, dict) and entry.get("operation") == operation:
            return entry
    return None


def github_operation_ready(state: CoordinareState, operation: str) -> tuple[bool, float]:
    """Return (ready, seconds_remaining) for queued operation backoff."""
    entry = get_deferred_github_operation(state, operation)
    if not isinstance(entry, dict):
        return True, 0.0
    retry_at = entry.get("retry_at")
    if not isinstance(retry_at, datetime):
        return True, 0.0
    now = datetime.now(UTC)
    remaining = (retry_at - now).total_seconds()
    return remaining <= 0, max(0.0, remaining)

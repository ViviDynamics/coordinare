from __future__ import annotations

from datetime import UTC, datetime, timedelta

from coordinare.graph.nodes.github_retry import (
    clear_deferred_github_operation,
    defer_github_operation,
    get_deferred_github_operation,
    github_operation_ready,
    is_transient_github_outage_error,
)
from coordinare.graph.state import initial_state
from coordinare.resilience import CircuitOpenError
from coordinare.services.github import TransientGitHubError


def test_is_transient_github_outage_error_matches_known_types_and_text() -> None:
    assert is_transient_github_outage_error(TransientGitHubError("transport down"))
    assert is_transient_github_outage_error(CircuitOpenError("github"))
    assert is_transient_github_outage_error(RuntimeError("Temporary failure in name resolution"))
    assert not is_transient_github_outage_error(RuntimeError("validation failed: bad field id"))


def test_defer_github_operation_sets_queue_and_retry_after() -> None:
    state = initial_state()

    entry = defer_github_operation(
        state,
        operation="poll_board",
        error=TransientGitHubError("dns lookup failed"),
        base_delay_seconds=10,
        max_delay_seconds=300,
    )

    assert entry["operation"] == "poll_board"
    assert entry["attempt"] == 1
    assert isinstance(entry["retry_at"], datetime)
    assert state.get("github_retry_after") == entry["retry_at"]


def test_defer_github_operation_increments_attempt_for_same_operation() -> None:
    state = initial_state()
    defer_github_operation(
        state,
        operation="poll_board",
        error=TransientGitHubError("dns lookup failed"),
        base_delay_seconds=10,
        max_delay_seconds=300,
    )
    second = defer_github_operation(
        state,
        operation="poll_board",
        error=TransientGitHubError("dns lookup failed"),
        base_delay_seconds=10,
        max_delay_seconds=300,
    )

    assert second["attempt"] == 2
    queue = state.get("github_retry_queue") or []
    assert len(queue) == 1


def test_github_operation_ready_respects_retry_timestamp_and_clear() -> None:
    state = initial_state()
    state["github_retry_queue"] = [{
        "operation": "monitor_pr",
        "attempt": 1,
        "retry_at": datetime.now(UTC) + timedelta(seconds=60),
        "error": "dns",
        "deferred_at": datetime.now(UTC),
    }]

    ready, remaining = github_operation_ready(state, "monitor_pr")
    assert not ready
    assert remaining > 0

    clear_deferred_github_operation(state, "monitor_pr")
    assert get_deferred_github_operation(state, "monitor_pr") is None
    ready_after, remaining_after = github_operation_ready(state, "monitor_pr")
    assert ready_after
    assert remaining_after == 0.0


def test_defer_github_operation_rate_limit_error_uses_retry_after() -> None:
    """RateLimitedGitHubError.retry_after takes precedence over exponential backoff."""
    from coordinare.services.github import RateLimitedGitHubError

    state = initial_state()
    entry = defer_github_operation(
        state,
        operation="poll_board",
        error=RateLimitedGitHubError(120, "rate limited"),
        base_delay_seconds=10,
        max_delay_seconds=300,
    )
    # Delay should be at least retry_after (120s), not the base 10s
    now = datetime.now(UTC)
    remaining = (entry["retry_at"] - now).total_seconds()
    assert remaining >= 100  # allow a few seconds for test execution


def test_github_operation_ready_when_retry_at_not_datetime() -> None:
    """Entry with non-datetime retry_at should be treated as immediately ready."""
    state = initial_state()
    state["github_retry_queue"] = [{
        "operation": "poll_board",
        "attempt": 1,
        "retry_at": "not-a-datetime",
        "error": "dns",
        "deferred_at": datetime.now(UTC),
    }]

    ready, remaining = github_operation_ready(state, "poll_board")
    assert ready
    assert remaining == 0.0

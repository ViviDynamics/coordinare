"""Comprehensive unit tests for the CircuitBreaker FSM (T024).

Tests cover all state transitions:
  (a) CLOSED -> OPEN on failure_threshold within observation_window
  (b) OPEN -> HALF_OPEN on recovery_window elapsed (via allow_request)
  (c) HALF_OPEN -> CLOSED on probe success
  (d) HALF_OPEN -> OPEN on probe failure
  (e) Second concurrent request in HALF_OPEN is blocked
  (f) Failures outside observation_window are pruned and do not count
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from coordinare.resilience import CircuitBreaker, CircuitOpenError, CircuitState

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_cb(
    threshold: int = 3,
    recovery: float = 120.0,
    observation: float = 300.0,
    service_name: str = "test-service",
) -> CircuitBreaker:
    return CircuitBreaker(
        service_name=service_name,
        failure_threshold=threshold,
        recovery_window=recovery,
        observation_window=observation,
    )


@pytest.fixture(autouse=True)
def _mock_metrics():
    """Prevent real Prometheus metric writes during every test.

    The import inside _transition() is a deferred local import:
        from coordinare.metrics import METRICS
    So we patch METRICS on the *coordinare.metrics* module itself.
    """
    mock_gauge = MagicMock()
    mock_gauge.labels.return_value = mock_gauge
    mock_metrics = MagicMock()
    mock_metrics.circuit_breaker_state = mock_gauge
    with patch("coordinare.metrics.METRICS", mock_metrics):
        yield mock_metrics


# ---------------------------------------------------------------------------
# (a) CLOSED -> OPEN: failure_threshold failures within observation_window
# ---------------------------------------------------------------------------


def test_closed_to_open_on_threshold_failures():
    """Three failures in a row within the observation window trip the breaker."""
    cb = _make_cb(threshold=3)

    base = 1000.0
    with patch.object(time, "monotonic", return_value=base):
        cb.record_failure()
    assert cb.state == CircuitState.CLOSED

    with patch.object(time, "monotonic", return_value=base + 1):
        cb.record_failure()
    assert cb.state == CircuitState.CLOSED

    with patch.object(time, "monotonic", return_value=base + 2):
        cb.record_failure()
    assert cb.state == CircuitState.OPEN


# ---------------------------------------------------------------------------
# (b) OPEN -> HALF_OPEN: recovery_window elapsed
# ---------------------------------------------------------------------------


def test_open_to_half_open_after_recovery_window():
    """After recovery_window seconds the next allow_request() transitions to HALF_OPEN."""
    cb = _make_cb(threshold=2, recovery=60.0)

    # Trip the breaker
    base = 1000.0
    with patch.object(time, "monotonic", return_value=base):
        cb.record_failure()
        cb.record_failure()
    assert cb.state == CircuitState.OPEN

    # Before recovery window: request blocked, still OPEN
    with patch.object(time, "monotonic", return_value=base + 59.9):
        assert cb.allow_request() is False
    assert cb.state == CircuitState.OPEN

    # At recovery window boundary: transitions to HALF_OPEN
    with patch.object(time, "monotonic", return_value=base + 60.0):
        assert cb.allow_request() is True
    assert cb.state == CircuitState.HALF_OPEN


# ---------------------------------------------------------------------------
# (c) HALF_OPEN -> CLOSED: probe success
# ---------------------------------------------------------------------------


def test_half_open_to_closed_on_probe_success():
    """A successful probe in HALF_OPEN resets the breaker to CLOSED."""
    cb = _make_cb(threshold=2, recovery=10.0)

    base = 1000.0
    # Trip to OPEN
    with patch.object(time, "monotonic", return_value=base):
        cb.record_failure()
        cb.record_failure()
    assert cb.state == CircuitState.OPEN

    # Advance past recovery → HALF_OPEN
    with patch.object(time, "monotonic", return_value=base + 10.0):
        assert cb.allow_request() is True
    assert cb.state == CircuitState.HALF_OPEN

    # Probe succeeds → CLOSED
    cb.record_success()
    assert cb.state == CircuitState.CLOSED
    assert cb.opened_at is None


# ---------------------------------------------------------------------------
# (d) HALF_OPEN -> OPEN: probe failure
# ---------------------------------------------------------------------------


def test_half_open_to_open_on_probe_failure():
    """A failed probe in HALF_OPEN re-opens the breaker."""
    cb = _make_cb(threshold=2, recovery=10.0)

    base = 1000.0
    with patch.object(time, "monotonic", return_value=base):
        cb.record_failure()
        cb.record_failure()
    assert cb.state == CircuitState.OPEN

    with patch.object(time, "monotonic", return_value=base + 10.0):
        assert cb.allow_request() is True
    assert cb.state == CircuitState.HALF_OPEN

    # Probe fails → back to OPEN
    with patch.object(time, "monotonic", return_value=base + 11.0):
        cb.record_failure()
    assert cb.state == CircuitState.OPEN
    assert cb.opened_at is not None


# ---------------------------------------------------------------------------
# (e) HALF_OPEN blocks second concurrent request
# ---------------------------------------------------------------------------


def test_half_open_blocks_concurrent_probe():
    """Only one probe request is allowed in HALF_OPEN; a second is blocked.

    The OPEN->HALF_OPEN transition (via allow_request) returns True without
    setting _half_open_probe_in_flight.  The *next* allow_request in HALF_OPEN
    sets the flag and grants the probe slot.  A *third* call must be blocked.
    """
    cb = _make_cb(threshold=2, recovery=10.0)

    base = 1000.0
    with patch.object(time, "monotonic", return_value=base):
        cb.record_failure()
        cb.record_failure()
    assert cb.state == CircuitState.OPEN

    with patch.object(time, "monotonic", return_value=base + 10.0):
        # This call transitions OPEN->HALF_OPEN and returns True
        assert cb.allow_request() is True
    assert cb.state == CircuitState.HALF_OPEN
    # The probe-in-flight flag is NOT set by the transition itself
    assert cb._half_open_probe_in_flight is False

    # First HALF_OPEN probe — allowed, flag now set
    assert cb.allow_request() is True
    assert cb._half_open_probe_in_flight is True

    # Second request while probe is in-flight — blocked
    assert cb.allow_request() is False


# ---------------------------------------------------------------------------
# (f) Failures outside observation_window are pruned
# ---------------------------------------------------------------------------


def test_failures_outside_observation_window_are_pruned():
    """Old failures beyond the observation window do not count toward the threshold."""
    cb = _make_cb(threshold=3, observation=60.0)

    base = 1000.0
    # Two failures at t=base
    with patch.object(time, "monotonic", return_value=base):
        cb.record_failure()
        cb.record_failure()
    assert cb.state == CircuitState.CLOSED

    # Third failure at t=base+61 — the first two are outside the window
    with patch.object(time, "monotonic", return_value=base + 61.0):
        cb.record_failure()
    # Only one failure remains in the window; breaker stays CLOSED
    assert cb.state == CircuitState.CLOSED


# ---------------------------------------------------------------------------
# guard() async context manager tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_guard_allows_request_when_closed():
    """guard() yields without error when the circuit is CLOSED."""
    cb = _make_cb()
    async with cb.guard():
        pass  # should not raise
    assert cb.state == CircuitState.CLOSED


@pytest.mark.asyncio
async def test_guard_raises_circuit_open_error_when_open():
    """guard() raises CircuitOpenError when the circuit is OPEN."""
    cb = _make_cb(threshold=1)

    base = 1000.0
    with patch.object(time, "monotonic", return_value=base):
        cb.record_failure()
    assert cb.state == CircuitState.OPEN

    with patch.object(time, "monotonic", return_value=base + 1.0), pytest.raises(
        CircuitOpenError, match="test-service",
    ):
        async with cb.guard():
            pass  # pragma: no cover — should not reach here


@pytest.mark.asyncio
async def test_guard_records_failure_on_exception():
    """guard() records a failure and re-raises when the body raises."""
    cb = _make_cb(threshold=2)

    base = 1000.0
    with patch.object(time, "monotonic", return_value=base), pytest.raises(
        ValueError, match="boom",
    ):
        async with cb.guard():
            raise ValueError("boom")

    # One failure recorded — still CLOSED
    assert cb.state == CircuitState.CLOSED

    with patch.object(time, "monotonic", return_value=base + 1.0), pytest.raises(
        ValueError, match="boom",
    ):
        async with cb.guard():
            raise ValueError("boom")

    # Second failure trips the breaker
    assert cb.state == CircuitState.OPEN


@pytest.mark.asyncio
async def test_guard_records_success_on_clean_exit():
    """guard() records a success when the body completes without error."""
    cb = _make_cb(threshold=2, recovery=10.0)

    base = 1000.0
    # Trip to OPEN
    with patch.object(time, "monotonic", return_value=base):
        cb.record_failure()
        cb.record_failure()
    assert cb.state == CircuitState.OPEN

    # Advance past recovery → allow_request transitions to HALF_OPEN
    with patch.object(time, "monotonic", return_value=base + 10.0):
        async with cb.guard():
            assert cb.state == CircuitState.HALF_OPEN

    # guard exited cleanly — record_success() should have moved to CLOSED
    assert cb.state == CircuitState.CLOSED


# ---------------------------------------------------------------------------
# Additional edge-case tests
# ---------------------------------------------------------------------------


def test_allow_request_always_true_when_closed():
    """allow_request() returns True repeatedly in CLOSED state."""
    cb = _make_cb()
    for _ in range(10):
        assert cb.allow_request() is True


def test_record_success_is_noop_when_closed():
    """record_success() in CLOSED state does nothing harmful."""
    cb = _make_cb()
    cb.record_success()  # should not raise
    assert cb.state == CircuitState.CLOSED


def test_circuit_open_error_attributes():
    """CircuitOpenError exposes the service_name attribute."""
    err = CircuitOpenError("my-service")
    assert err.service_name == "my-service"
    assert "my-service" in str(err)


def test_opened_at_set_on_trip_and_cleared_on_close():
    """opened_at datetime is set when tripped and cleared when closed."""
    cb = _make_cb(threshold=1, recovery=5.0)

    assert cb.opened_at is None

    base = 1000.0
    with patch.object(time, "monotonic", return_value=base):
        cb.record_failure()
    assert cb.state == CircuitState.OPEN
    assert cb.opened_at is not None

    # Transition to HALF_OPEN
    with patch.object(time, "monotonic", return_value=base + 5.0):
        cb.allow_request()
    assert cb.state == CircuitState.HALF_OPEN

    # Succeed the probe → CLOSED, opened_at cleared
    cb.record_success()
    assert cb.state == CircuitState.CLOSED
    assert cb.opened_at is None


def test_half_open_probe_flag_reset_on_failure():
    """After a probe failure in HALF_OPEN the probe-in-flight flag is reset."""
    cb = _make_cb(threshold=1, recovery=5.0)

    base = 1000.0
    with patch.object(time, "monotonic", return_value=base):
        cb.record_failure()
    assert cb.state == CircuitState.OPEN

    # Move to HALF_OPEN
    with patch.object(time, "monotonic", return_value=base + 5.0):
        cb.allow_request()
    assert cb.state == CircuitState.HALF_OPEN

    # Probe fails → back to OPEN, flag should be reset
    with patch.object(time, "monotonic", return_value=base + 6.0):
        cb.record_failure()
    assert cb.state == CircuitState.OPEN
    # The internal flag is cleared so the next HALF_OPEN probe can proceed
    assert cb._half_open_probe_in_flight is False


# ---------------------------------------------------------------------------
# 042 — guard(ignore=...) skips failure accounting for application-layer errors
# ---------------------------------------------------------------------------


class _AppError(Exception):
    """Stand-in for PermanentGitHubError in tests — represents an error
    that's logically the application's fault, not the service being down."""


@pytest.mark.asyncio
async def test_guard_ignore_does_not_record_failure():
    """guard(ignore=AppError) must not count toward the failure threshold —
    otherwise a misconfigured downstream (e.g., branch ruleset rejection)
    trips the breaker and starves all other healthy traffic."""
    cb = _make_cb(threshold=2, recovery=10.0)

    # Fire 5 ignored exceptions — should NOT trip the breaker
    for _ in range(5):
        with pytest.raises(_AppError):
            async with cb.guard(ignore=_AppError):
                raise _AppError("rejected by app layer")

    assert cb.state == CircuitState.CLOSED


@pytest.mark.asyncio
async def test_guard_ignore_still_propagates_exception():
    """Ignored exceptions must still propagate to the caller — only the
    failure accounting is skipped, not the error itself."""
    cb = _make_cb(threshold=2, recovery=10.0)

    with pytest.raises(_AppError, match="boom"):
        async with cb.guard(ignore=_AppError):
            raise _AppError("boom")


@pytest.mark.asyncio
async def test_guard_ignore_does_not_record_success():
    """Ignored exceptions are neither failures nor successes — they leave
    the breaker state unchanged.  This matters in HALF_OPEN: an app-layer
    error during a probe must not flip the breaker to CLOSED prematurely."""
    cb = _make_cb(threshold=1, recovery=5.0)

    base = 1000.0
    with patch.object(time, "monotonic", return_value=base):
        cb.record_failure()
    assert cb.state == CircuitState.OPEN

    # Move to HALF_OPEN
    with patch.object(time, "monotonic", return_value=base + 5.0):
        cb.allow_request()
    assert cb.state == CircuitState.HALF_OPEN

    # Probe call hits an app-layer error — must NOT close the breaker
    # (the service is still considered untrusted; we haven't proven recovery).
    with patch.object(time, "monotonic", return_value=base + 6.0), pytest.raises(_AppError):
        async with cb.guard(ignore=_AppError):
            raise _AppError("app-level rejection")

    # Still HALF_OPEN — the probe was inconclusive, neither success nor failure
    assert cb.state == CircuitState.HALF_OPEN


@pytest.mark.asyncio
async def test_guard_non_ignored_exception_still_records_failure():
    """Defensive: ignore= only matches the listed types.  Other exceptions
    must still count as failures so genuine outages still trip the breaker."""
    cb = _make_cb(threshold=2, recovery=10.0)

    for _ in range(2):
        with pytest.raises(RuntimeError):
            async with cb.guard(ignore=_AppError):
                raise RuntimeError("real network error")

    assert cb.state == CircuitState.OPEN


@pytest.mark.asyncio
async def test_guard_ignore_accepts_tuple_of_types():
    """ignore= takes either a single class or a tuple — same semantics as
    `except`.  Verifies both shapes work."""
    cb = _make_cb(threshold=2, recovery=10.0)

    class _OtherAppError(Exception): ...

    for exc in (_AppError("a"), _OtherAppError("b")):
        with pytest.raises(type(exc)):
            async with cb.guard(ignore=(_AppError, _OtherAppError)):
                raise exc

    assert cb.state == CircuitState.CLOSED


@pytest.mark.asyncio
async def test_guard_ignore_default_is_empty_tuple():
    """Without ignore=, behaviour is identical to before — every exception
    counts as a failure (backward compatibility)."""
    cb = _make_cb(threshold=2, recovery=10.0)

    for _ in range(2):
        with pytest.raises(_AppError):
            async with cb.guard():  # no ignore=
                raise _AppError("counts as failure")

    assert cb.state == CircuitState.OPEN


@pytest.mark.asyncio
async def test_guard_ignore_in_half_open_clears_probe_flag():
    """Copilot review: when an ignored exception fires during a HALF_OPEN
    probe, the probe-in-flight flag MUST be cleared — otherwise later
    callers see the flag still set and get blocked indefinitely.
    The ignore path doesn't flip state (inconclusive probe), but it
    must release the reservation allow_request() acquired."""
    cb = _make_cb(threshold=1, recovery=5.0)

    base = 1000.0
    # Trip to OPEN
    with patch.object(time, "monotonic", return_value=base):
        cb.record_failure()
    assert cb.state == CircuitState.OPEN

    # Probe #1 — transitions to HALF_OPEN, raises ignored exception
    with patch.object(time, "monotonic", return_value=base + 6.0), pytest.raises(_AppError):
        async with cb.guard(ignore=_AppError):
            raise _AppError("ignored during probe")

    # Still HALF_OPEN — the probe was inconclusive
    assert cb.state == CircuitState.HALF_OPEN
    # But the probe-in-flight flag must be cleared so a later call can probe
    assert cb._half_open_probe_in_flight is False

    # Probe #2 — must be allowed (not blocked by stale flag)
    with patch.object(time, "monotonic", return_value=base + 7.0):
        async with cb.guard(ignore=_AppError):
            pass  # success
    # Success closed the breaker
    assert cb.state == CircuitState.CLOSED

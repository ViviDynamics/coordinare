"""Unit tests for ResilientAgentService (T022).

Validates retry semantics, circuit-breaker guard propagation,
and the check_health bypass behaviour.

NOTE: The project-wide conftest disables stamina retries
(``stamina.set_active(False)``).  With stamina inactive the ``@retry``
decorator is a passthrough — the inner callable executes exactly once
regardless of the ``on=`` filter.  Tests that need real retry behaviour
re-enable stamina for the duration of the test.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import stamina

from coordinare.resilience import (
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
    ResilientAgentService,
    RetryConfig,
)
from coordinare.transport.base import TransportError, TransportTimeoutError
from coordinare.workspace import WorkspaceInfo

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _InnerAgent:
    """Minimal stub satisfying ``AgentServiceProtocol``."""

    def __init__(self) -> None:
        self.dispatch_card = AsyncMock(return_value={"ok": True})
        self.check_health = AsyncMock(return_value={"healthy": True})
        self.check_status = AsyncMock(return_value={"status": "running"})
        self.relay_feedback = AsyncMock(return_value={"relayed": True})


def _make_cb(
    *,
    failure_threshold: int = 3,
    recovery_window: float = 60.0,
    observation_window: float = 120.0,
) -> CircuitBreaker:
    return CircuitBreaker(
        service_name="test-agent",
        failure_threshold=failure_threshold,
        recovery_window=recovery_window,
        observation_window=observation_window,
    )


def _make_retry(*, attempts: int = 2) -> RetryConfig:
    return RetryConfig(
        attempts=attempts,
        wait_initial=0.0,
        wait_max=0.0,
        wait_jitter=0.0,
    )


# ---------------------------------------------------------------------------
# (a) TransportTimeoutError triggers stamina retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_card_retries_on_transport_timeout() -> None:
    """With stamina active and attempts=2, a transient TransportTimeoutError
    on the first call is retried and the second (successful) call wins."""

    inner = _InnerAgent()
    inner.dispatch_card.side_effect = [
        TransportTimeoutError(timeout=30),
        {"ok": True},
    ]

    cb = _make_cb()
    retry_cfg = _make_retry(attempts=2)
    svc = ResilientAgentService(inner, retry_cfg, cb)

    stamina.set_active(True)
    try:
        result = await svc.dispatch_card({"card": "data"})
    finally:
        stamina.set_active(False)

    assert result == {"ok": True}
    assert inner.dispatch_card.call_count == 2


# ---------------------------------------------------------------------------
# (b) Permanent TransportError propagates immediately (no retry)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_card_propagates_transport_error_immediately() -> None:
    """A generic ``TransportError`` (non-timeout) is NOT in the retry ``on=``
    filter, so it propagates on the first occurrence even with stamina active."""

    inner = _InnerAgent()
    inner.dispatch_card.side_effect = TransportError("connection refused")

    cb = _make_cb()
    retry_cfg = _make_retry(attempts=3)
    svc = ResilientAgentService(inner, retry_cfg, cb)

    stamina.set_active(True)
    try:
        with pytest.raises(TransportError, match="connection refused"):
            await svc.dispatch_card({"card": "data"})
    finally:
        stamina.set_active(False)

    # Called exactly once — no retry for non-timeout errors.
    assert inner.dispatch_card.call_count == 1


# ---------------------------------------------------------------------------
# (c) CircuitOpenError propagates when circuit is open
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_card_raises_circuit_open_error() -> None:
    """When the circuit breaker is already open, ``dispatch_card`` raises
    ``CircuitOpenError`` without calling the inner service at all."""

    inner = _InnerAgent()
    cb = _make_cb(failure_threshold=1)
    retry_cfg = _make_retry()
    svc = ResilientAgentService(inner, retry_cfg, cb)

    # Force the CB into OPEN state by recording enough failures.
    cb.record_failure()
    assert cb.state == CircuitState.OPEN

    with pytest.raises(CircuitOpenError):
        await svc.dispatch_card({"card": "data"})

    inner.dispatch_card.assert_not_called()


# ---------------------------------------------------------------------------
# (d) check_health bypasses circuit breaker
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_health_bypasses_circuit_breaker() -> None:
    """``check_health()`` delegates directly to the inner service, ignoring
    the circuit breaker state entirely."""

    inner = _InnerAgent()
    cb = _make_cb(failure_threshold=1)
    retry_cfg = _make_retry()
    svc = ResilientAgentService(inner, retry_cfg, cb)

    # Force the CB open.
    cb.record_failure()
    assert cb.state == CircuitState.OPEN

    result = await svc.check_health()

    assert result == {"healthy": True}
    inner.check_health.assert_awaited_once()


# ---------------------------------------------------------------------------
# (e) Successful dispatch_card records success on the CB
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_card_records_success_on_circuit_breaker() -> None:
    """A successful ``dispatch_card`` call invokes ``record_success()``
    on the circuit breaker (via the ``guard()`` context manager)."""

    inner = _InnerAgent()
    cb = _make_cb()
    retry_cfg = _make_retry()
    svc = ResilientAgentService(inner, retry_cfg, cb)

    with patch.object(cb, "record_success", wraps=cb.record_success) as spy:
        await svc.dispatch_card({"card": "data"})
        spy.assert_called_once()


# ---------------------------------------------------------------------------
# (f) Successful response is NOT retried
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_card_does_not_retry_on_success() -> None:
    """When ``dispatch_card`` returns a normal response the inner service
    is called exactly once — no spurious retries."""

    inner = _InnerAgent()
    inner.dispatch_card.return_value = {"ok": True}

    cb = _make_cb()
    retry_cfg = _make_retry(attempts=3)
    svc = ResilientAgentService(inner, retry_cfg, cb)

    stamina.set_active(True)
    try:
        result = await svc.dispatch_card({"card": "data"})
    finally:
        stamina.set_active(False)

    assert result == {"ok": True}
    assert inner.dispatch_card.call_count == 1


# ---------------------------------------------------------------------------
# Additional: check_status and relay_feedback exercise same retry + CB path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_status_retries_on_transport_timeout() -> None:
    """``check_status`` retries on TransportTimeoutError just like
    ``dispatch_card``."""

    inner = _InnerAgent()
    inner.check_status.side_effect = [
        TransportTimeoutError(timeout=10),
        {"status": "running"},
    ]

    cb = _make_cb()
    retry_cfg = _make_retry(attempts=2)
    svc = ResilientAgentService(inner, retry_cfg, cb)

    stamina.set_active(True)
    try:
        result = await svc.check_status("sess-1")
    finally:
        stamina.set_active(False)

    assert result == {"status": "running"}
    assert inner.check_status.call_count == 2


@pytest.mark.asyncio
async def test_relay_feedback_raises_circuit_open_error() -> None:
    """``relay_feedback`` is also guarded by the circuit breaker."""

    inner = _InnerAgent()
    cb = _make_cb(failure_threshold=1)
    retry_cfg = _make_retry()
    svc = ResilientAgentService(inner, retry_cfg, cb)

    cb.record_failure()
    assert cb.state == CircuitState.OPEN

    with pytest.raises(CircuitOpenError):
        await svc.relay_feedback({"review": "lgtm"})

    inner.relay_feedback.assert_not_called()


@pytest.mark.asyncio
async def test_dispatch_card_records_failure_on_circuit_breaker() -> None:
    """When the inner call raises, the circuit breaker records a failure."""

    inner = _InnerAgent()
    inner.dispatch_card.side_effect = TransportError("boom")

    cb = _make_cb()
    retry_cfg = _make_retry()
    svc = ResilientAgentService(inner, retry_cfg, cb)

    with patch.object(cb, "record_failure", wraps=cb.record_failure) as spy:
        with pytest.raises(TransportError):
            await svc.dispatch_card({"card": "data"})
        spy.assert_called_once()


# ---------------------------------------------------------------------------
# workspace_info forwarding (T025)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_card_forwards_workspace_info_to_inner() -> None:
    """ResilientAgentService forwards workspace_info kwarg to the inner service."""

    inner = _InnerAgent()
    cb = _make_cb()
    retry_cfg = _make_retry()
    svc = ResilientAgentService(inner, retry_cfg, cb)

    ws_info = WorkspaceInfo(
        path=Path("/tmp/ws/repo"),
        branch="coordinare/CARD_1/feature",
        repo_url="https://github.com/acme/repo.git",
    )

    await svc.dispatch_card({"card": "data"}, workspace_info=ws_info)

    inner.dispatch_card.assert_awaited_once_with(
        {"card": "data"}, workspace_info=ws_info
    )


@pytest.mark.asyncio
async def test_dispatch_card_forwards_none_workspace_info() -> None:
    """workspace_info=None (default) is forwarded correctly to the inner service."""

    inner = _InnerAgent()
    cb = _make_cb()
    retry_cfg = _make_retry()
    svc = ResilientAgentService(inner, retry_cfg, cb)

    await svc.dispatch_card({"card": "data"})

    inner.dispatch_card.assert_awaited_once_with({"card": "data"}, workspace_info=None)


# ---------------------------------------------------------------------------
# RetryConfig.to_stamina_kwargs()
# ---------------------------------------------------------------------------


def test_retry_config_to_stamina_kwargs_returns_expected_keys() -> None:
    """RetryConfig.to_stamina_kwargs() returns all stamina.retry() parameters."""
    cfg = RetryConfig(
        attempts=5,
        wait_initial=0.1,
        wait_max=2.0,
        wait_jitter=0.5,
        wait_exp_base=3.0,
    )
    kwargs = cfg.to_stamina_kwargs()
    assert kwargs == {
        "attempts": 5,
        "wait_initial": 0.1,
        "wait_max": 2.0,
        "wait_jitter": 0.5,
        "wait_exp_base": 3.0,
    }


def test_retry_config_to_stamina_kwargs_default_exp_base() -> None:
    """to_stamina_kwargs() includes wait_exp_base=2.0 when not explicitly set."""
    cfg = RetryConfig(attempts=2, wait_initial=0.0, wait_max=0.0, wait_jitter=0.0)
    kwargs = cfg.to_stamina_kwargs()
    assert kwargs["wait_exp_base"] == 2.0


# ---------------------------------------------------------------------------
# relay_feedback() failure path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_relay_feedback_reraises_and_increments_failure_metric() -> None:
    """When the inner relay_feedback raises, the exception propagates and the
    failure metric is incremented."""
    from coordinare.metrics import METRICS

    inner = _InnerAgent()
    inner.relay_feedback.side_effect = RuntimeError("relay exploded")

    cb = _make_cb()
    retry_cfg = _make_retry()
    svc = ResilientAgentService(inner, retry_cfg, cb)

    before = METRICS.service_calls_total.labels(
        symphony="__default__", service="agent", action="relay_feedback", outcome="failure"
    )._value.get()

    with pytest.raises(RuntimeError, match="relay exploded"):
        await svc.relay_feedback({"review": "data"})

    after = METRICS.service_calls_total.labels(
        symphony="__default__", service="agent", action="relay_feedback", outcome="failure"
    )._value.get()
    assert after == before + 1.0


# ---------------------------------------------------------------------------
# check_status() failure path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_status_reraises_and_increments_failure_metric() -> None:
    """When the inner check_status raises, the exception propagates and the
    failure metric is incremented."""
    from coordinare.metrics import METRICS

    inner = _InnerAgent()
    inner.check_status.side_effect = RuntimeError("status exploded")

    cb = _make_cb()
    retry_cfg = _make_retry()
    svc = ResilientAgentService(inner, retry_cfg, cb)

    before = METRICS.service_calls_total.labels(
        symphony="__default__", service="agent", action="check_status", outcome="failure"
    )._value.get()

    with pytest.raises(RuntimeError, match="status exploded"):
        await svc.check_status("sess-x")

    after = METRICS.service_calls_total.labels(
        symphony="__default__", service="agent", action="check_status", outcome="failure"
    )._value.get()
    assert after == before + 1.0


# ---------------------------------------------------------------------------
# relay_feedback() happy path — line 298: return result
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_relay_feedback_returns_result_on_success() -> None:
    """relay_feedback() happy path — line 298 (return result) is executed."""
    inner = _InnerAgent()
    inner.relay_feedback = AsyncMock(return_value={"relayed": True})
    cb = _make_cb()
    svc = ResilientAgentService(inner, _make_retry(), cb)

    result = await svc.relay_feedback({"review": "data"})
    assert result == {"relayed": True}


# ---------------------------------------------------------------------------
# get_agent_logs() — inner lacks the method → returns [] (lines 247-248)
# ---------------------------------------------------------------------------


def test_get_agent_logs_falls_back_to_empty_list_when_inner_lacks_method() -> None:
    """get_agent_logs() returns [] when inner has no get_agent_logs attr (else branch)."""

    class _NoLogs:
        """Inner service without get_agent_logs."""
        dispatch_card = AsyncMock()
        check_health = AsyncMock()
        check_status = AsyncMock()
        relay_feedback = AsyncMock()

    svc = ResilientAgentService(_NoLogs(), _make_retry(), _make_cb())
    result = svc.get_agent_logs()
    assert result == []


# ---------------------------------------------------------------------------
# CircuitBreaker on_open_callback — called when circuit opens (lines 110-111)
# ---------------------------------------------------------------------------


def test_on_open_callback_called_when_circuit_opens() -> None:
    """on_open_callback is invoked (inside contextlib.suppress) when state → OPEN."""
    calls: list[tuple[str, str]] = []

    def _callback(service: str, reason: str) -> None:
        calls.append((service, reason))

    cb = CircuitBreaker(
        service_name="svc",
        failure_threshold=1,
        recovery_window=60.0,
        observation_window=120.0,
        on_open_callback=_callback,
    )
    # Single failure trips the breaker (threshold=1)
    cb.record_failure()

    assert len(calls) == 1
    assert calls[0][0] == "svc"


def test_record_success_on_closed_circuit_is_noop() -> None:
    """record_success() on a CLOSED circuit executes the 'elif CLOSED: pass' branch."""
    cb = _make_cb()
    assert cb._state == CircuitState.CLOSED
    cb.record_success()  # Must not raise and must leave state CLOSED
    assert cb._state == CircuitState.CLOSED


def test_record_success_on_open_circuit_is_noop() -> None:
    """record_success() on an OPEN circuit hits the 'elif CLOSED' False branch (136->exit)."""
    cb = CircuitBreaker(
        service_name="svc",
        failure_threshold=1,
        recovery_window=60.0,
        observation_window=120.0,
    )
    cb.record_failure()  # trips the breaker → OPEN
    assert cb._state == CircuitState.OPEN
    # Calling record_success() while OPEN: neither elif branch matches → silent exit
    cb.record_success()
    assert cb._state == CircuitState.OPEN  # unchanged

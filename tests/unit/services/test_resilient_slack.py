"""Unit tests for Slack service resilience (T017).

Tests cover:
(a) Transient 5xx -> TransientSlackError raised
(b) Permanent 4xx -> PermanentSlackError raised (not retried)
(c) CircuitOpenError propagated when circuit is open
(d) httpx.TimeoutException -> TransientSlackError
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from coordinare.models.card import CardStatus
from coordinare.models.notification import Notification
from coordinare.resilience import CircuitBreaker, CircuitOpenError, CircuitState
from coordinare.services.slack import (
    PermanentSlackError,
    SlackService,
    TransientSlackError,
)

# ---------------------------------------------------------------------------
# Module-scoped fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _mock_metrics():
    """Prevent real Prometheus metric writes when CB state transitions fire.

    ``CircuitBreaker._transition()`` does a deferred local import of
    ``coordinare.metrics.METRICS``, so we patch at the module level.
    """
    mock_gauge = MagicMock()
    mock_gauge.labels.return_value = mock_gauge
    mock_metrics = MagicMock()
    mock_metrics.circuit_breaker_state = mock_gauge
    with patch("coordinare.metrics.METRICS", mock_metrics):
        yield mock_metrics


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_notification() -> Notification:
    return Notification(
        card_title="Test Card",
        previous_status=CardStatus.TODO,
        card_status=CardStatus.IN_PROGRESS,
        task_description="Run unit tests",
    )


def _make_http_response(status_code: int) -> httpx.Response:
    """Build a minimal httpx.Response with the given status code."""
    return httpx.Response(
        status_code=status_code,
        request=httpx.Request("POST", "https://hooks.slack.com/services/x/y/z"),
    )


def _patch_httpx_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    response: httpx.Response | None = None,
    side_effect: Exception | None = None,
) -> AsyncMock:
    """Replace ``httpx.AsyncClient`` so that ``post()`` behaves as instructed.

    If *side_effect* is given the mock's ``post()`` raises it.
    Otherwise ``post()`` returns *response* (which must be provided).
    """
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.__aexit__.return_value = None

    if side_effect is not None:
        client.post.side_effect = side_effect
    else:
        assert response is not None
        # Make raise_for_status() behave like the real httpx: raise on 4xx/5xx
        def _raise_for_status() -> None:
            if response.status_code >= 400:
                raise httpx.HTTPStatusError(
                    f"HTTP {response.status_code}",
                    request=response.request,
                    response=response,
                )

        response.raise_for_status = _raise_for_status  # type: ignore[assignment]
        client.post.return_value = response

    monkeypatch.setattr(
        "coordinare.services.slack.httpx.AsyncClient", lambda timeout=10: client
    )
    return client


def _make_circuit_breaker(
    *,
    failure_threshold: int = 3,
    recovery_window: float = 30.0,
    observation_window: float = 60.0,
) -> CircuitBreaker:
    """Return a ``CircuitBreaker`` for ``SlackService`` tests."""
    return CircuitBreaker(
        service_name="slack-test",
        failure_threshold=failure_threshold,
        recovery_window=recovery_window,
        observation_window=observation_window,
    )


# ---------------------------------------------------------------------------
# (a) Transient 5xx -> TransientSlackError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transient_5xx_raises_transient_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 500 response from Slack should be surfaced as TransientSlackError.

    With stamina globally disabled the retry decorator is a no-op, so the
    exception propagates immediately.
    """
    response = _make_http_response(500)
    _patch_httpx_client(monkeypatch, response=response)

    service = SlackService(
        "https://hooks.slack.com/services/x/y/z",
        "#eng",
    )

    with pytest.raises(TransientSlackError):
        await service.send_notification(_make_notification())


@pytest.mark.asyncio
async def test_transient_502_raises_transient_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """502 Bad Gateway is also transient."""
    response = _make_http_response(502)
    _patch_httpx_client(monkeypatch, response=response)

    service = SlackService(
        "https://hooks.slack.com/services/x/y/z",
        "#eng",
    )

    with pytest.raises(TransientSlackError):
        await service.send_notification(_make_notification())


# ---------------------------------------------------------------------------
# (b) Permanent 4xx -> PermanentSlackError (not retried)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_permanent_4xx_raises_permanent_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 400 response should be surfaced as PermanentSlackError."""
    response = _make_http_response(400)
    _patch_httpx_client(monkeypatch, response=response)

    service = SlackService(
        "https://hooks.slack.com/services/x/y/z",
        "#eng",
    )

    with pytest.raises(PermanentSlackError):
        await service.send_notification(_make_notification())


@pytest.mark.asyncio
async def test_permanent_403_raises_permanent_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """403 Forbidden is also permanent."""
    response = _make_http_response(403)
    _patch_httpx_client(monkeypatch, response=response)

    service = SlackService(
        "https://hooks.slack.com/services/x/y/z",
        "#eng",
    )

    with pytest.raises(PermanentSlackError):
        await service.send_notification(_make_notification())


@pytest.mark.asyncio
async def test_permanent_404_raises_permanent_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """404 Not Found is also permanent."""
    response = _make_http_response(404)
    _patch_httpx_client(monkeypatch, response=response)

    service = SlackService(
        "https://hooks.slack.com/services/x/y/z",
        "#eng",
    )

    with pytest.raises(PermanentSlackError):
        await service.send_notification(_make_notification())


# ---------------------------------------------------------------------------
# (c) CircuitOpenError propagated on open circuit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_circuit_open_error_propagated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the circuit breaker is open, CircuitOpenError must propagate
    without even attempting the HTTP call."""
    # Build a CB and force it open by recording enough failures
    cb_adapter = _make_circuit_breaker(failure_threshold=2)
    inner_cb = cb_adapter

    # We need the CB to be CLOSED and trip it open by recording failures.
    # record_failure() needs to be called within the observation window.
    inner_cb.record_failure()
    inner_cb.record_failure()
    assert inner_cb.state == CircuitState.OPEN

    # Set up a client mock — it should never be called
    response = _make_http_response(200)
    client_mock = _patch_httpx_client(monkeypatch, response=response)

    service = SlackService(
        "https://hooks.slack.com/services/x/y/z",
        "#eng",
        circuit_breaker=cb_adapter,
    )

    with pytest.raises(CircuitOpenError):
        await service.send_notification(_make_notification())

    # Confirm the HTTP client was never invoked
    client_mock.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_circuit_breaker_records_failure_on_transient_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a transient error, the CB should record the failure.

    Enough failures should trip the CB to OPEN.
    """
    cb_adapter = _make_circuit_breaker(failure_threshold=2)
    inner_cb = cb_adapter

    response = _make_http_response(500)
    _patch_httpx_client(monkeypatch, response=response)

    service = SlackService(
        "https://hooks.slack.com/services/x/y/z",
        "#eng",
        circuit_breaker=cb_adapter,
    )

    # First failure
    with pytest.raises(TransientSlackError):
        await service.send_notification(_make_notification())

    assert inner_cb.state == CircuitState.CLOSED  # not yet tripped

    # Second failure -> should trip
    with pytest.raises(TransientSlackError):
        await service.send_notification(_make_notification())

    assert inner_cb.state == CircuitState.OPEN


@pytest.mark.asyncio
async def test_circuit_breaker_records_success_on_ok_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful call through the CB should record success (CB stays CLOSED)."""
    cb_adapter = _make_circuit_breaker(failure_threshold=3)
    inner_cb = cb_adapter

    response = _make_http_response(200)
    _patch_httpx_client(monkeypatch, response=response)

    service = SlackService(
        "https://hooks.slack.com/services/x/y/z",
        "#eng",
        circuit_breaker=cb_adapter,
    )

    await service.send_notification(_make_notification())

    assert inner_cb.state == CircuitState.CLOSED


# ---------------------------------------------------------------------------
# (d) httpx.TimeoutException -> TransientSlackError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeout_raises_transient_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An httpx timeout should be wrapped as TransientSlackError."""
    _patch_httpx_client(
        monkeypatch,
        side_effect=httpx.ReadTimeout("Connection timed out"),
    )

    service = SlackService(
        "https://hooks.slack.com/services/x/y/z",
        "#eng",
    )

    with pytest.raises(TransientSlackError, match="timed out"):
        await service.send_notification(_make_notification())


@pytest.mark.asyncio
async def test_connect_timeout_raises_transient_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connect timeout is also transient."""
    _patch_httpx_client(
        monkeypatch,
        side_effect=httpx.ConnectTimeout("Connect timed out"),
    )

    service = SlackService(
        "https://hooks.slack.com/services/x/y/z",
        "#eng",
    )

    with pytest.raises(TransientSlackError, match="timed out"):
        await service.send_notification(_make_notification())


@pytest.mark.asyncio
async def test_network_error_raises_transient_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An httpx NetworkError should be wrapped as TransientSlackError."""
    _patch_httpx_client(
        monkeypatch,
        side_effect=httpx.NetworkError("Connection reset"),
    )

    service = SlackService(
        "https://hooks.slack.com/services/x/y/z",
        "#eng",
    )

    with pytest.raises(TransientSlackError, match="Connection reset"):
        await service.send_notification(_make_notification())

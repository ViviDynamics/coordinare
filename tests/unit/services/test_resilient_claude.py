"""Unit tests for Anthropic/Claude service resilience (T025).

Validates the exception taxonomy, SDK retry disabling, circuit breaker
integration, and metrics recording in ClaudeService.assess_card_sufficiency().

Test cases:
  (a) APIConnectionError -> TransientAnthropicError
  (b) AuthenticationError -> PermanentAnthropicError (not retried)
  (c) SDK max_retries=0 verified on _client
  (d) CircuitOpenError propagated on open circuit
  (e) Successful call increments SERVICE_CALLS_TOTAL
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from anthropic import APIConnectionError, AuthenticationError

from coordinare.resilience import CircuitBreaker, CircuitOpenError, CircuitState
from coordinare.services.claude import (
    ClaudeService,
    PermanentAnthropicError,
    TransientAnthropicError,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SAMPLE_CARD: dict[str, object] = {"title": "Test Card", "description": "A test card."}


def _make_cb(
    failure_threshold: int = 3,
    recovery_window: float = 60.0,
    observation_window: float = 120.0,
) -> CircuitBreaker:
    """Create a CircuitBreaker with sensible test defaults."""
    return CircuitBreaker(
        service_name="anthropic-test",
        failure_threshold=failure_threshold,
        recovery_window=recovery_window,
        observation_window=observation_window,
    )


def _make_service(
    circuit_breaker: CircuitBreaker | None = None,
) -> ClaudeService:
    """Create a ClaudeService with a dummy API key and optional CB."""
    return ClaudeService(
        api_key="test-key",
        circuit_breaker=circuit_breaker,
    )


def _mock_request() -> httpx.Request:
    """Create a minimal httpx.Request for SDK exception constructors."""
    return httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def _mock_response(status_code: int = 401) -> httpx.Response:
    """Create a minimal httpx.Response for SDK exception constructors."""
    request = _mock_request()
    return httpx.Response(status_code=status_code, request=request)


def _successful_response(text: str = '{"sufficient": true, "questions": [], "rationale": "ok"}') -> SimpleNamespace:
    """Build a mock Anthropic Messages response with a single text block."""
    block = SimpleNamespace(text=text)
    return SimpleNamespace(content=[block])


# ---------------------------------------------------------------------------
# (a) APIConnectionError -> TransientAnthropicError
# ---------------------------------------------------------------------------


class TestAPIConnectionErrorClassification:
    """APIConnectionError is classified as TransientAnthropicError."""

    @pytest.mark.asyncio
    async def test_api_connection_error_raises_transient(self) -> None:
        service = _make_service()
        exc = APIConnectionError(request=_mock_request())

        mock_create = AsyncMock(side_effect=exc)
        service._client = SimpleNamespace(messages=SimpleNamespace(create=mock_create))

        with pytest.raises(TransientAnthropicError):
            await service.assess_card_sufficiency(SAMPLE_CARD)

    @pytest.mark.asyncio
    async def test_api_connection_error_chains_original_cause(self) -> None:
        service = _make_service()
        original = APIConnectionError(request=_mock_request())

        mock_create = AsyncMock(side_effect=original)
        service._client = SimpleNamespace(messages=SimpleNamespace(create=mock_create))

        with pytest.raises(TransientAnthropicError) as exc_info:
            await service.assess_card_sufficiency(SAMPLE_CARD)

        assert exc_info.value.__cause__ is original


# ---------------------------------------------------------------------------
# (b) AuthenticationError -> PermanentAnthropicError (not retried)
# ---------------------------------------------------------------------------


class TestAuthenticationErrorClassification:
    """AuthenticationError is classified as PermanentAnthropicError."""

    @pytest.mark.asyncio
    async def test_authentication_error_raises_permanent(self) -> None:
        service = _make_service()
        response = _mock_response(status_code=401)
        exc = AuthenticationError(
            message="Invalid API key",
            response=response,
            body={"error": {"message": "Invalid API key"}},
        )

        mock_create = AsyncMock(side_effect=exc)
        service._client = SimpleNamespace(messages=SimpleNamespace(create=mock_create))

        with pytest.raises(PermanentAnthropicError):
            await service.assess_card_sufficiency(SAMPLE_CARD)

    @pytest.mark.asyncio
    async def test_authentication_error_chains_original_cause(self) -> None:
        service = _make_service()
        response = _mock_response(status_code=401)
        original = AuthenticationError(
            message="Invalid API key",
            response=response,
            body={"error": {"message": "Invalid API key"}},
        )

        mock_create = AsyncMock(side_effect=original)
        service._client = SimpleNamespace(messages=SimpleNamespace(create=mock_create))

        with pytest.raises(PermanentAnthropicError) as exc_info:
            await service.assess_card_sufficiency(SAMPLE_CARD)

        assert exc_info.value.__cause__ is original

    @pytest.mark.asyncio
    async def test_authentication_error_not_retried(self) -> None:
        """PermanentAnthropicError should not be retried even with multiple attempts configured."""
        service = ClaudeService(
            api_key="test-key",
            retry_kwargs={
                "attempts": 3,
                "wait_initial": 0.0,
                "wait_max": 0.0,
                "wait_jitter": 0.0,
                "wait_exp_base": 2.0,
            },
        )
        response = _mock_response(status_code=401)
        exc = AuthenticationError(
            message="Invalid API key",
            response=response,
            body={"error": {"message": "Invalid API key"}},
        )

        mock_create = AsyncMock(side_effect=exc)
        service._client = SimpleNamespace(messages=SimpleNamespace(create=mock_create))

        with pytest.raises(PermanentAnthropicError):
            await service.assess_card_sufficiency(SAMPLE_CARD)

        # PermanentAnthropicError is not in the retry "on" set, so create
        # should have been called exactly once regardless of attempts config.
        assert mock_create.call_count == 1


# ---------------------------------------------------------------------------
# (c) SDK max_retries=0 verified
# ---------------------------------------------------------------------------


class TestSDKMaxRetriesDisabled:
    """AsyncAnthropic client is instantiated with max_retries=0."""

    def test_client_max_retries_is_zero(self) -> None:
        service = _make_service()
        # The anthropic SDK stores max_retries on the client instance.
        assert service._client.max_retries == 0


# ---------------------------------------------------------------------------
# (d) CircuitOpenError propagated on open circuit
# ---------------------------------------------------------------------------


class TestCircuitOpenPropagation:
    """CircuitOpenError raised when CB is open blocks the call."""

    @pytest.mark.asyncio
    async def test_circuit_open_error_propagates(self) -> None:
        cb = _make_cb(failure_threshold=2)

        # Force the circuit open by recording enough failures.
        for _ in range(cb.failure_threshold):
            cb.record_failure()

        assert cb.state == CircuitState.OPEN

        service = _make_service(circuit_breaker=cb)
        mock_create = AsyncMock(return_value=_successful_response())
        service._client = SimpleNamespace(messages=SimpleNamespace(create=mock_create))

        with pytest.raises(CircuitOpenError, match="anthropic-test"):
            await service.assess_card_sufficiency(SAMPLE_CARD)

        # The underlying client should never have been called.
        assert mock_create.call_count == 0

    @pytest.mark.asyncio
    async def test_circuit_open_error_has_service_name(self) -> None:
        cb = _make_cb(failure_threshold=1)
        cb.record_failure()

        service = _make_service(circuit_breaker=cb)
        mock_create = AsyncMock(return_value=_successful_response())
        service._client = SimpleNamespace(messages=SimpleNamespace(create=mock_create))

        with pytest.raises(CircuitOpenError) as exc_info:
            await service.assess_card_sufficiency(SAMPLE_CARD)

        assert exc_info.value.service_name == "anthropic-test"

    @pytest.mark.asyncio
    async def test_failure_records_on_circuit_breaker(self) -> None:
        """A transient error flowing through assess_card_sufficiency records a CB failure."""
        cb = _make_cb(failure_threshold=3)
        service = _make_service(circuit_breaker=cb)

        exc = APIConnectionError(request=_mock_request())
        mock_create = AsyncMock(side_effect=exc)
        service._client = SimpleNamespace(messages=SimpleNamespace(create=mock_create))

        with pytest.raises(TransientAnthropicError):
            await service.assess_card_sufficiency(SAMPLE_CARD)

        # One failure recorded; CB still closed.
        assert cb.state == CircuitState.CLOSED
        assert len(cb._failure_times) == 1

    @pytest.mark.asyncio
    async def test_repeated_failures_trip_circuit(self) -> None:
        """Enough failures through assess_card_sufficiency open the CB."""
        cb = _make_cb(failure_threshold=2)

        for _ in range(2):
            service = _make_service(circuit_breaker=cb)
            exc = APIConnectionError(request=_mock_request())
            mock_create = AsyncMock(side_effect=exc)
            service._client = SimpleNamespace(messages=SimpleNamespace(create=mock_create))

            with pytest.raises(TransientAnthropicError):
                await service.assess_card_sufficiency(SAMPLE_CARD)

        assert cb.state == CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_success_transitions_half_open_to_closed(self) -> None:
        """After recovery window the CB goes half-open; a success closes it."""
        cb = _make_cb(failure_threshold=1, recovery_window=0.0)

        # Trip the breaker.
        cb.record_failure()
        assert cb.state == CircuitState.OPEN

        # With recovery_window=0 the next allow_request() will transition
        # to HALF_OPEN immediately.
        service = _make_service(circuit_breaker=cb)
        mock_create = AsyncMock(return_value=_successful_response())
        service._client = SimpleNamespace(messages=SimpleNamespace(create=mock_create))

        result = await service.assess_card_sufficiency(SAMPLE_CARD)

        assert result["sufficient"] is True
        assert cb.state == CircuitState.CLOSED


# ---------------------------------------------------------------------------
# (e) Successful call increments SERVICE_CALLS_TOTAL
# ---------------------------------------------------------------------------


class TestMetricsRecording:
    """Successful and failed calls update service_calls_total metric."""

    @pytest.mark.asyncio
    async def test_successful_call_increments_success_metric(self) -> None:
        service = _make_service()
        mock_create = AsyncMock(return_value=_successful_response())
        service._client = SimpleNamespace(messages=SimpleNamespace(create=mock_create))

        mock_counter = MagicMock()
        mock_counter.labels.return_value = mock_counter

        with patch("coordinare.services.claude.METRICS") as mock_metrics:
            mock_metrics.service_calls_total = mock_counter
            await service.assess_card_sufficiency(SAMPLE_CARD)

        mock_counter.labels.assert_any_call(
            service="anthropic", action="assess_card", outcome="success",
        )
        mock_counter.inc.assert_called()

    @pytest.mark.asyncio
    async def test_failed_call_increments_failure_metric(self) -> None:
        service = _make_service()
        exc = APIConnectionError(request=_mock_request())
        mock_create = AsyncMock(side_effect=exc)
        service._client = SimpleNamespace(messages=SimpleNamespace(create=mock_create))

        mock_counter = MagicMock()
        mock_counter.labels.return_value = mock_counter

        with patch("coordinare.services.claude.METRICS") as mock_metrics:
            mock_metrics.service_calls_total = mock_counter
            with pytest.raises(TransientAnthropicError):
                await service.assess_card_sufficiency(SAMPLE_CARD)

        mock_counter.labels.assert_any_call(
            service="anthropic", action="assess_card", outcome="failure",
        )
        mock_counter.inc.assert_called()

    @pytest.mark.asyncio
    async def test_success_metric_not_incremented_on_failure(self) -> None:
        """On a failed call, only 'failure' outcome is recorded, not 'success'."""
        service = _make_service()
        exc = APIConnectionError(request=_mock_request())
        mock_create = AsyncMock(side_effect=exc)
        service._client = SimpleNamespace(messages=SimpleNamespace(create=mock_create))

        mock_counter = MagicMock()
        mock_counter.labels.return_value = mock_counter

        with patch("coordinare.services.claude.METRICS") as mock_metrics:
            mock_metrics.service_calls_total = mock_counter
            with pytest.raises(TransientAnthropicError):
                await service.assess_card_sufficiency(SAMPLE_CARD)

        # Collect all labels() calls and ensure "success" was never used.
        label_calls = mock_counter.labels.call_args_list
        outcomes = [call.kwargs.get("outcome") for call in label_calls]
        assert "success" not in outcomes

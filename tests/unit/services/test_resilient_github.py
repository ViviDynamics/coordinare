"""Unit tests for GitHub service resilience (T013).

Validates the exception taxonomy, error classification in _execute(),
retry wrapping in _retried_execute(), and circuit breaker integration
in _guarded_execute().
"""

from __future__ import annotations

from typing import Any

import aiohttp
import pytest

from coordinare.resilience import CircuitBreaker, CircuitOpenError, CircuitState
from coordinare.services.github import (
    GitHubService,
    PermanentGitHubError,
    TransientGitHubError,
)

# ---------------------------------------------------------------------------
# Fake client that can be configured to raise or return specific values
# ---------------------------------------------------------------------------


class _FakeClient:
    """Configurable fake GQL client for testing _execute() error paths."""

    def __init__(
        self,
        responses: list[dict[str, Any] | BaseException] | None = None,
        *,
        async_mode: bool = True,
    ) -> None:
        self._responses: list[dict[str, Any] | BaseException] = responses or []
        self._async_mode = async_mode
        self.calls: list[tuple[object, dict[str, Any]]] = []

    async def _execute_async(
        self, query: object, variable_values: dict[str, Any],
    ) -> Any:
        self.calls.append((query, variable_values))
        resp = self._responses.pop(0)
        if isinstance(resp, BaseException):
            raise resp
        return resp

    def execute(self, query: object, variable_values: dict[str, Any]) -> Any:
        if self._async_mode:
            return self._execute_async(query, variable_values)
        self.calls.append((query, variable_values))
        resp = self._responses.pop(0)
        if isinstance(resp, BaseException):
            raise resp
        return resp


# ---------------------------------------------------------------------------
# Subclass that injects the fake client and disables retries by default
# ---------------------------------------------------------------------------


class _TestGitHubService(GitHubService):
    """GitHubService wired to a fake client, with retries disabled."""

    def __init__(
        self,
        client: _FakeClient,
        circuit_breaker: CircuitBreaker | None = None,
        retry_kwargs: dict[str, Any] | None = None,
    ) -> None:
        # Single attempt by default so tests do not loop on transient errors.
        effective_retry = retry_kwargs if retry_kwargs is not None else {
            "attempts": 1,
            "wait_initial": 0.0,
            "wait_max": 0.0,
            "wait_jitter": 0.0,
            "wait_exp_base": 2.0,
        }
        super().__init__(
            token="tok",
            org="acme",
            project_number=1,
            circuit_breaker=circuit_breaker,
            retry_kwargs=effective_retry,
        )
        self._fake_client = client

    def _build_client(self, token: str = "") -> _FakeClient:  # type: ignore[override]
        return self._fake_client


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DUMMY_QUERY = "query Dummy { __typename }"
DUMMY_VARS: dict[str, Any] = {}


def _make_cb(
    failure_threshold: int = 3,
    recovery_window: float = 60.0,
    observation_window: float = 120.0,
) -> CircuitBreaker:
    """Create a CircuitBreaker with sensible test defaults."""
    return CircuitBreaker(
        service_name="github-test",
        failure_threshold=failure_threshold,
        recovery_window=recovery_window,
        observation_window=observation_window,
    )


# ---------------------------------------------------------------------------
# (a) Transient error classification
# ---------------------------------------------------------------------------


class TestTransientErrorClassification:
    """TimeoutError, aiohttp.ClientError, OSError -> TransientGitHubError."""

    @pytest.mark.asyncio
    async def test_timeout_error_classified_as_transient(self) -> None:
        client = _FakeClient([TimeoutError("timed out")])
        service = _TestGitHubService(client)

        with pytest.raises(TransientGitHubError, match="timed out"):
            await service._execute(DUMMY_QUERY, DUMMY_VARS)

    @pytest.mark.asyncio
    async def test_aiohttp_client_error_classified_as_transient(self) -> None:
        # aiohttp.ClientError is a concrete exception class
        err = aiohttp.ClientError("connection reset")
        client = _FakeClient([err])
        service = _TestGitHubService(client)

        with pytest.raises(TransientGitHubError, match="connection reset"):
            await service._execute(DUMMY_QUERY, DUMMY_VARS)

    @pytest.mark.asyncio
    async def test_os_error_classified_as_transient(self) -> None:
        client = _FakeClient([OSError("network unreachable")])
        service = _TestGitHubService(client)

        with pytest.raises(TransientGitHubError, match="network unreachable"):
            await service._execute(DUMMY_QUERY, DUMMY_VARS)

    @pytest.mark.asyncio
    async def test_transient_error_chains_original_cause(self) -> None:
        original = TimeoutError("read timeout")
        client = _FakeClient([original])
        service = _TestGitHubService(client)

        with pytest.raises(TransientGitHubError) as exc_info:
            await service._execute(DUMMY_QUERY, DUMMY_VARS)

        assert exc_info.value.__cause__ is original


# ---------------------------------------------------------------------------
# (b) Permanent error classification
# ---------------------------------------------------------------------------


class TestPermanentErrorClassification:
    """ValueError -> PermanentGitHubError."""

    @pytest.mark.asyncio
    async def test_value_error_classified_as_permanent(self) -> None:
        client = _FakeClient([ValueError("bad query")])
        service = _TestGitHubService(client)

        with pytest.raises(PermanentGitHubError, match="bad query"):
            await service._execute(DUMMY_QUERY, DUMMY_VARS)

    @pytest.mark.asyncio
    async def test_permanent_error_chains_original_cause(self) -> None:
        original = ValueError("malformed")
        client = _FakeClient([original])
        service = _TestGitHubService(client)

        with pytest.raises(PermanentGitHubError) as exc_info:
            await service._execute(DUMMY_QUERY, DUMMY_VARS)

        assert exc_info.value.__cause__ is original


# ---------------------------------------------------------------------------
# (c) CircuitOpenError propagation
# ---------------------------------------------------------------------------


class TestCircuitOpenPropagation:
    """CircuitOpenError raised when CB is open."""

    @pytest.mark.asyncio
    async def test_circuit_open_error_propagates(self) -> None:
        cb = _make_cb(failure_threshold=2)

        # Force the circuit open by recording enough failures.
        for _ in range(cb.failure_threshold):
            cb.record_failure()

        assert cb.state == CircuitState.OPEN

        client = _FakeClient([{"ok": True}])
        service = _TestGitHubService(client, circuit_breaker=cb)

        with pytest.raises(CircuitOpenError, match="github-test"):
            await service._guarded_execute(DUMMY_QUERY, DUMMY_VARS)

        # The underlying client should never have been called.
        assert len(client.calls) == 0

    @pytest.mark.asyncio
    async def test_circuit_open_error_has_service_name(self) -> None:
        cb = _make_cb(failure_threshold=1)
        cb.record_failure()

        client = _FakeClient([])
        service = _TestGitHubService(client, circuit_breaker=cb)

        with pytest.raises(CircuitOpenError) as exc_info:
            await service._guarded_execute(DUMMY_QUERY, DUMMY_VARS)

        assert exc_info.value.service_name == "github-test"


# ---------------------------------------------------------------------------
# (d) Non-object response -> PermanentGitHubError
# ---------------------------------------------------------------------------


class TestNonObjectResponse:
    """A response that is not a dict is a permanent error."""

    @pytest.mark.asyncio
    async def test_string_response_raises_permanent(self) -> None:
        # The fake client returns a string directly; _execute checks isinstance(result, dict).
        client = _FakeClient([], async_mode=True)
        # Manually inject the string response.  We override _responses to bypass
        # the BaseException check in _FakeClient.
        client._responses = ["not-a-dict"]  # type: ignore[list-item]
        service = _TestGitHubService(client)

        with pytest.raises(PermanentGitHubError, match="JSON object"):
            await service._execute(DUMMY_QUERY, DUMMY_VARS)

    @pytest.mark.asyncio
    async def test_list_response_raises_permanent(self) -> None:
        client = _FakeClient([], async_mode=True)
        client._responses = [[1, 2, 3]]  # type: ignore[list-item]
        service = _TestGitHubService(client)

        with pytest.raises(PermanentGitHubError, match="JSON object"):
            await service._execute(DUMMY_QUERY, DUMMY_VARS)

    @pytest.mark.asyncio
    async def test_none_response_raises_permanent(self) -> None:
        client = _FakeClient([], async_mode=True)
        client._responses = [None]  # type: ignore[list-item]
        service = _TestGitHubService(client)

        with pytest.raises(PermanentGitHubError, match="JSON object"):
            await service._execute(DUMMY_QUERY, DUMMY_VARS)


# ---------------------------------------------------------------------------
# (e) Successful call records success on circuit breaker
# ---------------------------------------------------------------------------


class TestGuardedExecuteRecordsSuccess:
    """Successful guarded_execute records success on the CB."""

    @pytest.mark.asyncio
    async def test_success_records_on_circuit_breaker(self) -> None:
        cb = _make_cb()
        client = _FakeClient([{"data": {"ok": True}}])
        service = _TestGitHubService(client, circuit_breaker=cb)

        result = await service._guarded_execute(DUMMY_QUERY, DUMMY_VARS)

        assert result == {"data": {"ok": True}}
        # CB should remain closed after a success.
        assert cb.state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_success_transitions_half_open_to_closed(self) -> None:
        """After recovery window the CB goes half-open; a success closes it."""
        cb = _make_cb(failure_threshold=1, recovery_window=0.0)

        # Trip the breaker.
        cb.record_failure()
        assert cb.state == CircuitState.OPEN

        # With recovery_window=0 the next allow_request() will transition
        # to HALF_OPEN immediately.
        client = _FakeClient([{"data": {"recovered": True}}])
        service = _TestGitHubService(client, circuit_breaker=cb)

        result = await service._guarded_execute(DUMMY_QUERY, DUMMY_VARS)

        assert result == {"data": {"recovered": True}}
        assert cb.state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_failure_through_guarded_execute_records_failure(self) -> None:
        """A transient error flowing through guarded_execute records a CB failure."""
        cb = _make_cb(failure_threshold=3)
        client = _FakeClient([TimeoutError("boom")])
        service = _TestGitHubService(client, circuit_breaker=cb)

        with pytest.raises(TransientGitHubError):
            await service._guarded_execute(DUMMY_QUERY, DUMMY_VARS)

        # One failure recorded; CB still closed.
        assert cb.state == CircuitState.CLOSED
        assert len(cb._failure_times) == 1

    @pytest.mark.asyncio
    async def test_repeated_failures_trip_circuit(self) -> None:
        """Enough failures through guarded_execute open the CB."""
        cb = _make_cb(failure_threshold=2)

        for i in range(2):
            client = _FakeClient([TimeoutError(f"fail-{i}")])
            service = _TestGitHubService(client, circuit_breaker=cb)
            with pytest.raises(TransientGitHubError):
                await service._guarded_execute(DUMMY_QUERY, DUMMY_VARS)

        assert cb.state == CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_guarded_execute_without_circuit_breaker(self) -> None:
        """When no CB is configured, guarded_execute still works."""
        client = _FakeClient([{"data": {"ok": True}}])
        service = _TestGitHubService(client, circuit_breaker=None)

        result = await service._guarded_execute(DUMMY_QUERY, DUMMY_VARS)

        assert result == {"data": {"ok": True}}

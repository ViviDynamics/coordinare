"""Unit tests for GitHubService token rotation (015 T022)."""
from __future__ import annotations

import pytest

from coordinare.services.github import GitHubService


class _FakeAuth:
    """Controllable auth stub that returns whatever token is set."""

    def __init__(self, token: str) -> None:
        self._token = token

    async def get_token(self) -> str:
        return self._token

    async def invalidate(self) -> None:
        return None


# ---------------------------------------------------------------------------
# Exception taxonomy (085 — Contract B / data-model)
# ---------------------------------------------------------------------------


def test_auth_github_error_is_permanent_subclass() -> None:
    """AuthGitHubError must subclass PermanentGitHubError (and GitHubError)."""
    from coordinare.services.github import (
        AuthGitHubError,
        GitHubError,
        PermanentGitHubError,
    )

    assert issubclass(AuthGitHubError, PermanentGitHubError)
    assert issubclass(AuthGitHubError, GitHubError)
    assert issubclass(AuthGitHubError, RuntimeError)


def _make_service(token: str = "tok") -> GitHubService:
    return GitHubService(
        org="acme",
        project_number=1,
        auth=_FakeAuth(token),
    )


@pytest.mark.asyncio
async def test_client_built_on_first_execute() -> None:
    """After the first token resolution, _last_token and _client are set."""
    svc = _make_service("tok-a")
    assert svc._client is None
    assert svc._last_token is None

    token = await svc._current_token()
    if svc._client is None or token != svc._last_token:
        svc._client = svc._build_client(token)
        svc._last_token = token

    assert svc._last_token == "tok-a"
    assert svc._client is not None


@pytest.mark.asyncio
async def test_client_rebuilt_when_token_changes() -> None:
    auth = _FakeAuth("tok-a")
    svc = GitHubService(org="acme", project_number=1, auth=auth)

    original_build = svc._build_client
    built_tokens: list[str] = []

    def spy_build(token: str):
        built_tokens.append(token)
        return original_build(token)

    svc._build_client = spy_build  # type: ignore[method-assign]

    # Simulate first _execute token resolution
    tok = await svc._current_token()
    if svc._client is None or tok != svc._last_token:
        svc._client = spy_build(tok)
        svc._last_token = tok

    # Now rotate token
    auth._token = "tok-b"

    tok2 = await svc._current_token()
    if svc._client is None or tok2 != svc._last_token:
        svc._client = spy_build(tok2)
        svc._last_token = tok2

    assert built_tokens == ["tok-a", "tok-b"]
    assert svc._last_token == "tok-b"


@pytest.mark.asyncio
async def test_client_not_rebuilt_when_token_unchanged() -> None:
    auth = _FakeAuth("tok-stable")
    svc = GitHubService(org="acme", project_number=1, auth=auth)

    original_build = svc._build_client
    build_calls: list[str] = []

    def spy_build(token: str):
        build_calls.append(token)
        return original_build(token)

    svc._build_client = spy_build  # type: ignore[method-assign]

    # First resolution
    tok = await svc._current_token()
    if svc._client is None or tok != svc._last_token:
        svc._client = spy_build(tok)
        svc._last_token = tok

    # Second resolution — same token
    tok2 = await svc._current_token()
    if svc._client is None or tok2 != svc._last_token:
        svc._client = spy_build(tok2)
        svc._last_token = tok2

    assert build_calls == ["tok-stable"]  # only built once


def test_service_requires_auth_or_token() -> None:
    with pytest.raises(ValueError, match="requires either auth= or token="):
        GitHubService(org="acme", project_number=1)


def test_service_rejects_both_auth_and_token() -> None:
    auth = _FakeAuth("tok")
    with pytest.raises(ValueError, match="only one of auth= or token="):
        GitHubService(org="acme", project_number=1, auth=auth, token="also-tok")


def test_service_rejects_non_protocol_auth_object() -> None:
    """auth= that does not satisfy GitHubAuth protocol must raise ValueError."""
    with pytest.raises(ValueError, match="must satisfy the GitHubAuth protocol"):
        GitHubService(org="acme", project_number=1, auth=object())


def test_service_accepts_legacy_token_kwarg() -> None:
    svc = GitHubService(org="acme", project_number=1, token="legacy-tok")
    assert svc._auth is not None


@pytest.mark.asyncio
async def test_legacy_token_kwarg_returns_correct_token() -> None:
    svc = GitHubService(org="acme", project_number=1, token="legacy-tok")
    result = await svc._current_token()
    assert result == "legacy-tok"


@pytest.mark.asyncio
async def test_execute_serializes_concurrent_callers() -> None:
    """061: _execute must hold _gql_lock for the full request, not just
    client construction. The underlying AIOHTTPTransport cannot service
    concurrent execute_async calls — overlap produces 'Transport is
    already connected' and poisons the connector. This test fakes the
    request layer and asserts that two concurrent _execute calls never
    overlap inside _execute_request.
    """
    import asyncio

    svc = _make_service("tok")
    svc._client = object()  # bypass real client construction
    svc._last_token = "tok"

    in_flight = 0
    max_in_flight = 0

    async def fake_execute_request(_client, _query, _vars):
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return {"ok": True}

    svc._execute_request = fake_execute_request  # type: ignore[method-assign]

    await asyncio.gather(*[svc._execute("q", {}) for _ in range(5)])

    assert max_in_flight == 1, (
        f"_execute calls overlapped (max_in_flight={max_in_flight}); "
        "lock must guard the full request"
    )


# ===========================================================================
# 085 US1 — auth-failure classification + refresh-and-retry-once
# ===========================================================================

import aiohttp  # noqa: E402
from gql.transport.exceptions import (  # noqa: E402
    TransportQueryError,
    TransportServerError,
)

from coordinare.services.github import (  # noqa: E402
    AuthGitHubError,
    PermanentGitHubError,
    RateLimitedGitHubError,
    TransientGitHubError,
)


class _RaisingClient:
    """Fake gql client whose execute_async raises a preset exception."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def execute_async(self, _document, variable_values=None):
        raise self._exc


def _client_response_error(status: int, retry_after: str = "0") -> aiohttp.ClientResponseError:
    from multidict import CIMultiDict
    from yarl import URL

    url = URL("https://api.github.com/graphql")
    request_info = aiohttp.RequestInfo(
        url=url,
        method="POST",
        headers=CIMultiDict(),  # type: ignore[arg-type]
        real_url=url,
    )
    return aiohttp.ClientResponseError(
        request_info=request_info,
        history=(),
        status=status,
        headers=CIMultiDict({"Retry-After": retry_after}),
    )


# --- T007: classification (Contract B1-B6) ---------------------------------


@pytest.mark.asyncio
async def test_b1_transport_server_error_401_is_auth() -> None:
    svc = _make_service()
    client = _RaisingClient(TransportServerError("Bad credentials", code=401))
    with pytest.raises(AuthGitHubError):
        await svc._execute_request(client, "{ __typename }", {})


@pytest.mark.asyncio
async def test_b2_transport_query_error_unauthorized_is_auth() -> None:
    svc = _make_service()
    exc = TransportQueryError("unauthorized", errors=[{"type": "UNAUTHORIZED"}])
    client = _RaisingClient(exc)
    with pytest.raises(AuthGitHubError):
        await svc._execute_request(client, "{ __typename }", {})


@pytest.mark.asyncio
async def test_b3_client_response_error_401_is_auth() -> None:
    svc = _make_service()
    client = _RaisingClient(_client_response_error(401))
    with pytest.raises(AuthGitHubError):
        await svc._execute_request(client, "{ __typename }", {})


@pytest.mark.asyncio
async def test_b4_transport_server_error_502_stays_transient() -> None:
    svc = _make_service()
    client = _RaisingClient(TransportServerError("Bad Gateway", code=502))
    with pytest.raises(TransientGitHubError) as ei:
        await svc._execute_request(client, "{ __typename }", {})
    assert not isinstance(ei.value, AuthGitHubError)


@pytest.mark.asyncio
async def test_b5_transport_query_error_nonauth_stays_permanent() -> None:
    svc = _make_service()
    for t in ("FORBIDDEN", "NOT_FOUND", "UNPROCESSABLE"):
        exc = TransportQueryError(t.lower(), errors=[{"type": t}])
        client = _RaisingClient(exc)
        with pytest.raises(PermanentGitHubError) as ei:
            await svc._execute_request(client, "{ __typename }", {})
        assert not isinstance(ei.value, AuthGitHubError), t


@pytest.mark.asyncio
async def test_b6_client_response_error_429_stays_rate_limited() -> None:
    svc = _make_service()
    client = _RaisingClient(_client_response_error(429, retry_after="0"))
    with pytest.raises(RateLimitedGitHubError):
        await svc._execute_request(client, "{ __typename }", {})


# --- Controllable auth for refresh-and-retry tests -------------------------


class _RotatingAuth:
    """Auth stub: get_token() returns the current token; invalidate() advances
    to the next preset token (models AppAuth re-minting after invalidate)."""

    def __init__(self, tokens: list[str]) -> None:
        self._tokens = tokens
        self._idx = 0
        self.invalidate_calls = 0

    async def get_token(self) -> str:
        return self._tokens[min(self._idx, len(self._tokens) - 1)]

    async def invalidate(self) -> None:
        self.invalidate_calls += 1
        self._idx += 1


# --- T008: refresh-and-retry success (Contract C1) -------------------------


@pytest.mark.asyncio
async def test_c1_refresh_and_retry_recovers() -> None:
    auth = _RotatingAuth(["bad-tok", "good-tok"])
    svc = GitHubService(org="acme", project_number=1, auth=auth)

    built: list[str] = []
    original_build = svc._build_client
    svc._build_client = lambda tok: (built.append(tok) or original_build(tok))  # type: ignore[assignment]

    calls = {"n": 0}

    async def fake_req(_client, _q, _v):
        calls["n"] += 1
        if calls["n"] == 1:
            raise AuthGitHubError("auth failed")
        return {"ok": True}

    svc._execute_request = fake_req  # type: ignore[method-assign]

    result = await svc._execute("{ __typename }", {})

    assert result == {"ok": True}
    assert auth.invalidate_calls == 1
    assert calls["n"] == 2  # exactly one retry
    assert svc._last_token == "good-tok"
    assert built[-1] == "good-tok"


# --- T009: second auth failure is permanent (Contract C2, FR-004) ----------


@pytest.mark.asyncio
async def test_c2_second_auth_failure_is_permanent() -> None:
    auth = _RotatingAuth(["bad-1", "bad-2"])
    svc = GitHubService(org="acme", project_number=1, auth=auth)

    calls = {"n": 0}

    async def fake_req(_client, _q, _v):
        calls["n"] += 1
        raise AuthGitHubError("auth failed")

    svc._execute_request = fake_req  # type: ignore[method-assign]

    with pytest.raises(PermanentGitHubError):
        await svc._execute("{ __typename }", {})

    assert calls["n"] == 2  # no third attempt
    assert auth.invalidate_calls == 1


# --- T010: refreshed credential carries forward (Contract C6) --------------


@pytest.mark.asyncio
async def test_c6_refreshed_token_carries_forward() -> None:
    auth = _RotatingAuth(["bad-tok", "good-tok"])
    svc = GitHubService(org="acme", project_number=1, auth=auth)

    calls = {"n": 0}

    async def fake_req(_client, _q, _v):
        calls["n"] += 1
        if calls["n"] == 1:
            raise AuthGitHubError("auth failed")
        return {"ok": calls["n"]}

    svc._execute_request = fake_req  # type: ignore[method-assign]

    await svc._execute("{ __typename }", {})  # recovers; n -> 2
    assert svc._last_token == "good-tok"

    # Next call must reuse the refreshed token with no further invalidate.
    result = await svc._execute("{ __typename }", {})
    assert result == {"ok": 3}
    assert calls["n"] == 3
    assert auth.invalidate_calls == 1
    assert svc._last_token == "good-tok"


# ===========================================================================
# 085 US2 — static fast-fail, breaker/retry safety, mint-failure propagation
# ===========================================================================


class _StaticAuth:
    """Auth stub modeling a static (PAT-like) credential: invalidate() is a
    no-op and get_token() always returns the same token."""

    def __init__(self, token: str) -> None:
        self._token = token
        self.invalidate_calls = 0

    async def get_token(self) -> str:
        return self._token

    async def invalidate(self) -> None:
        self.invalidate_calls += 1


# --- T015: static fast-fail (Contract C3, FR-005) --------------------------


@pytest.mark.asyncio
async def test_c3_static_credential_fast_fails_without_retry() -> None:
    auth = _StaticAuth("static-tok")
    svc = GitHubService(org="acme", project_number=1, auth=auth)

    calls = {"n": 0}

    async def fake_req(_client, _q, _v):
        calls["n"] += 1
        raise AuthGitHubError("auth failed")

    svc._execute_request = fake_req  # type: ignore[method-assign]

    with pytest.raises(PermanentGitHubError):
        await svc._execute("{ __typename }", {})

    # Exactly one attempt — fresh == old means no retry, no loop.
    assert calls["n"] == 1
    assert auth.invalidate_calls == 1


# --- T016: breaker/retry integration (Contract D1/D2) ----------------------


@pytest.mark.asyncio
async def test_d2_auth_failure_not_retried_by_stamina() -> None:
    """An escaping PermanentGitHubError (from a static auth failure) must not
    be retried by stamina (on=TransientGitHubError only)."""
    auth = _StaticAuth("static-tok")
    svc = GitHubService(org="acme", project_number=1, auth=auth)

    calls = {"n": 0}

    async def fake_req(_client, _q, _v):
        calls["n"] += 1
        raise AuthGitHubError("auth failed")

    svc._execute_request = fake_req  # type: ignore[method-assign]

    with pytest.raises(PermanentGitHubError):
        await svc._retried_execute("{ __typename }", {})

    # One attempt inside _execute (static fast-fail); stamina does not retry.
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_d1_auth_failure_ignored_by_circuit_breaker() -> None:
    """PermanentGitHubError (incl. AuthGitHubError) must be ignored by the
    circuit breaker so a stale credential never trips service health."""
    from coordinare.resilience import CircuitBreaker, CircuitState

    auth = _StaticAuth("static-tok")
    breaker = CircuitBreaker(
        service_name="github",
        failure_threshold=3,
        recovery_window=30.0,
        observation_window=60.0,
    )
    svc = GitHubService(
        org="acme", project_number=1, auth=auth, circuit_breaker=breaker,
    )

    calls = {"n": 0}

    async def fake_req(_client, _q, _v):
        calls["n"] += 1
        raise AuthGitHubError("auth failed")

    svc._execute_request = fake_req  # type: ignore[method-assign]

    # Far more iterations than any breaker failure threshold. If auth failures
    # counted against health, the breaker would open and short-circuit later
    # calls WITHOUT invoking fake_req. A constant 1:1 call ratio proves the
    # breaker stayed closed (ignore=PermanentGitHubError).
    for i in range(20):
        with pytest.raises(PermanentGitHubError):
            await svc._guarded_execute("{ __typename }", {})
        assert calls["n"] == i + 1, "breaker tripped — auth failure counted against health"

    # 20 auth failures with threshold=3: breaker MUST still be closed.
    assert breaker.state == CircuitState.CLOSED


# --- T017: mint-failure propagation (Contract C4/C5, FR-007) ---------------


class _MintFailingAuth:
    """First get_token() returns a token; after invalidate(), get_token()
    raises the configured mint exception."""

    def __init__(self, token: str, mint_exc: BaseException) -> None:
        self._token = token
        self._mint_exc = mint_exc
        self._invalidated = False
        self.invalidate_calls = 0

    async def get_token(self) -> str:
        if self._invalidated:
            raise self._mint_exc
        return self._token

    async def invalidate(self) -> None:
        self.invalidate_calls += 1
        self._invalidated = True


@pytest.mark.asyncio
async def test_c4_transient_mint_failure_propagates_transient() -> None:
    auth = _MintFailingAuth("bad-tok", TransientGitHubError("mint 503"))
    svc = GitHubService(org="acme", project_number=1, auth=auth)

    async def fake_req(_client, _q, _v):
        raise AuthGitHubError("auth failed")

    svc._execute_request = fake_req  # type: ignore[method-assign]

    with pytest.raises(TransientGitHubError):
        await svc._execute("{ __typename }", {})
    assert auth.invalidate_calls == 1


@pytest.mark.asyncio
async def test_c5_permanent_mint_failure_propagates_permanent() -> None:
    auth = _MintFailingAuth("bad-tok", PermanentGitHubError("mint 403"))
    svc = GitHubService(org="acme", project_number=1, auth=auth)

    async def fake_req(_client, _q, _v):
        raise AuthGitHubError("auth failed")

    svc._execute_request = fake_req  # type: ignore[method-assign]

    with pytest.raises(PermanentGitHubError):
        await svc._execute("{ __typename }", {})
    assert auth.invalidate_calls == 1


# ===========================================================================
# 085 US3 — secret-free observability for refresh-and-retry
# ===========================================================================


@pytest.mark.asyncio
async def test_e1_recovered_refresh_retry_emits_outcome_recovered() -> None:
    from structlog.testing import capture_logs

    auth = _RotatingAuth(["bad-tok", "good-tok"])
    svc = GitHubService(org="acme", project_number=1, auth=auth)

    calls = {"n": 0}

    async def fake_req(_client, _q, _v):
        calls["n"] += 1
        if calls["n"] == 1:
            raise AuthGitHubError("auth failed")
        return {"ok": True}

    svc._execute_request = fake_req  # type: ignore[method-assign]

    with capture_logs() as cap:
        await svc._execute("{ __typename }", {})

    events = [e for e in cap if e.get("event") == "github.auth.refresh_retry"]
    assert len(events) == 1
    assert events[0]["outcome"] == "recovered"


@pytest.mark.asyncio
async def test_e1_failed_refresh_retry_emits_outcome_failed() -> None:
    from structlog.testing import capture_logs

    auth = _RotatingAuth(["bad-1", "bad-2"])
    svc = GitHubService(org="acme", project_number=1, auth=auth)

    async def fake_req(_client, _q, _v):
        raise AuthGitHubError("auth failed")

    svc._execute_request = fake_req  # type: ignore[method-assign]

    with capture_logs() as cap, pytest.raises(PermanentGitHubError):
        await svc._execute("{ __typename }", {})

    events = [e for e in cap if e.get("event") == "github.auth.refresh_retry"]
    assert len(events) == 1
    assert events[0]["outcome"] == "failed"


@pytest.mark.asyncio
async def test_e2_refresh_retry_records_contain_no_secrets() -> None:
    from structlog.testing import capture_logs

    auth = _RotatingAuth(["bad-tok", "good-tok"])
    svc = GitHubService(org="acme", project_number=1, auth=auth)

    calls = {"n": 0}

    async def fake_req(_client, _q, _v):
        calls["n"] += 1
        if calls["n"] == 1:
            raise AuthGitHubError("auth failed")
        return {"ok": True}

    svc._execute_request = fake_req  # type: ignore[method-assign]

    with capture_logs() as cap:
        await svc._execute("{ __typename }", {})

    events = [e for e in cap if e.get("event") == "github.auth.refresh_retry"]
    assert events
    for event in events:
        blob = " ".join(f"{k}={v}" for k, v in event.items()).lower()
        for secret in ("bad-tok", "good-tok"):
            assert secret not in blob, f"secret leaked in log event: {secret}"
        # Only the event name + outcome (+ structlog's log_level) are allowed.
        assert set(event) <= {"event", "outcome", "log_level"}

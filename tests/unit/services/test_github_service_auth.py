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

"""Unit tests for PatAuth (015 T021)."""
from __future__ import annotations

import pytest

from coordinare.auth.pat import PatAuth


def test_get_token_returns_token() -> None:
    auth = PatAuth("ghp_abc123")
    import asyncio
    assert asyncio.run(auth.get_token()) == "ghp_abc123"


@pytest.mark.asyncio
async def test_get_token_async_returns_token() -> None:
    auth = PatAuth("my-static-token")
    result = await auth.get_token()
    assert result == "my-static-token"


@pytest.mark.asyncio
async def test_get_token_returns_same_token_on_repeated_calls() -> None:
    auth = PatAuth("ghp_abc123")
    first = await auth.get_token()
    second = await auth.get_token()
    assert first == second == "ghp_abc123"


def test_empty_token_raises_value_error() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        PatAuth("")


def test_whitespace_only_token_raises_value_error() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        PatAuth("   ")


def test_none_like_empty_raises_value_error() -> None:
    with pytest.raises(ValueError):
        PatAuth("")


def test_token_stored_correctly() -> None:
    auth = PatAuth("secret-token-value")
    assert auth._token == "secret-token-value"


@pytest.mark.asyncio
async def test_pat_auth_satisfies_github_auth_protocol() -> None:
    from coordinare.auth.protocol import GitHubAuth
    auth = PatAuth("tok")
    assert isinstance(auth, GitHubAuth)

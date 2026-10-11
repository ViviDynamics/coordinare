"""Spec 551: bearer auth behavior for incoming MCP requests."""

from __future__ import annotations

from pydantic import SecretStr

from coordinare.mcp.auth import check_bearer, credential_fingerprint

TOKEN = "t" * 32


def make_token() -> SecretStr:
    return SecretStr(TOKEN)


def test_missing_header_rejected() -> None:
    assert check_bearer(None, make_token()) is False
    assert check_bearer("", make_token()) is False


def test_wrong_scheme_rejected() -> None:
    assert check_bearer("Basic dXNlcjpwYXNz", make_token()) is False


def test_wrong_token_rejected() -> None:
    assert check_bearer("Bearer " + "w" * 32, make_token()) is False


def test_correct_token_accepted() -> None:
    assert check_bearer(f"Bearer {TOKEN}", make_token()) is True


def test_scheme_case_insensitive() -> None:
    assert check_bearer(f"bearer {TOKEN}", make_token()) is True


def test_fingerprint_stable_and_never_the_token() -> None:
    first = credential_fingerprint(TOKEN)
    second = credential_fingerprint(TOKEN)
    assert first == second
    assert len(first) == 8
    assert int(first, 16) >= 0  # hex
    assert TOKEN not in first
    assert credential_fingerprint("other") != first

"""#142: coordinare's own login is resolved from its identity, never written into the code.

The App's slug comes from ``GET /app``, a token's login from ``GET /user``, and
``github_self_logins`` adds to them. A failed lookup never fails startup: it is
logged once, the filter uses the configured list, and the lookup is retried.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from structlog.testing import capture_logs

from coordinare.auth.app import AppAuth
from coordinare.config import ProjectConfiguration
from coordinare.graph.nodes.route_issue_comments import route_issue_comments
from coordinare.graph.state import initial_state
from coordinare.services.card_identity import CardIdentityMap
from coordinare.services.github import GitHubService
from coordinare.services.issue_comment_service import fetch_new_issue_comments, normalize_logins

_API = "https://api.github.com"
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _response(status: int, body: dict[str, Any], url: str) -> httpx.Response:
    return httpx.Response(status, json=body, request=httpx.Request("GET", url))


@pytest.fixture(scope="module")
def key_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A synthetic RSA key for the App JWT. No real credentials."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path = tmp_path_factory.mktemp("keys") / "app.pem"
    path.write_bytes(pem)
    return path


def _app_auth(key: Path) -> AppAuth:
    return AppAuth(app_id=1, private_key_path=key, installation_id=2, api_url=_API)


# --- App auth and token auth ------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_app_slug_is_read_from_get_app(key_file: Path) -> None:
    url = f"{_API}/app"
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=_response(200, {"slug": "acme-coord"}, url))) as get:
        assert await _app_auth(key_file).get_app_slug() == "acme-coord"
    assert get.await_args.args[0] == url
    assert get.await_args.kwargs["headers"]["Authorization"].startswith("Bearer ")


@pytest.mark.asyncio
async def test_a_github_app_is_known_by_its_slug_and_its_bot_login(key_file: Path) -> None:
    svc = GitHubService(org="acme", project_number=1, auth=_app_auth(key_file))
    url = f"{_API}/app"
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=_response(200, {"slug": "acme-coord"}, url))):
        assert await svc.self_logins() == frozenset({"acme-coord", "acme-coord[bot]"})


@pytest.mark.asyncio
async def test_a_token_is_known_by_its_users_login() -> None:
    svc = GitHubService(org="acme", project_number=1, token="token-example")
    url = f"{_API}/user"
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=_response(200, {"login": "Jason-Berry"}, url))) as get:
        assert await svc.self_logins() == frozenset({"jason-berry"})
    assert get.await_args.args[0] == url
    assert get.await_args.kwargs["headers"]["Authorization"] == "Bearer token-example"


@pytest.mark.asyncio
async def test_the_identity_is_resolved_once_and_then_cached(key_file: Path) -> None:
    svc = GitHubService(org="acme", project_number=1, auth=_app_auth(key_file))
    url = f"{_API}/app"
    responses = [_response(200, {"slug": "acme-coord"}, url)]  # a second call would run out of responses
    with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=responses)) as get:
        await svc.self_logins()
        await svc.self_logins()
    assert get.await_count == 1


# --- resolution failure never fails startup ----------------------------------------------- #


@pytest.mark.asyncio
async def test_a_failed_lookup_is_logged_once_and_retried_on_a_later_call(key_file: Path) -> None:
    svc = GitHubService(org="acme", project_number=1, auth=_app_auth(key_file))
    url = f"{_API}/app"
    responses = [
        _response(500, {}, url),
        _response(500, {}, url),
        _response(200, {"slug": "acme-coord"}, url),
    ]
    with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=responses)), capture_logs() as logs:
        assert await svc.self_logins() == frozenset()
        assert await svc.self_logins() == frozenset()
        assert await svc.self_logins() == frozenset({"acme-coord", "acme-coord[bot]"})
    warnings = [entry for entry in logs if entry["event"] == "github.self_logins_unresolved"]
    assert len(warnings) == 1


def test_the_default_configured_list_is_empty() -> None:
    assert ProjectConfiguration.model_fields["github_self_logins"].default_factory() == []


def test_the_example_config_documents_the_option() -> None:
    text = (_REPO_ROOT / "config.example.yaml").read_text(encoding="utf-8")
    assert "github_self_logins" in text


def test_logins_are_compared_without_case_or_blanks() -> None:
    assert normalize_logins([" Acme-Coord[bot] ", "", "ACME-COORD"]) == frozenset({"acme-coord[bot]", "acme-coord"})


# --- the filter, with a login other than this deployment's ---------------------------------- #


class _Board:
    def __init__(self, comments: list[dict[str, Any]]) -> None:
        self._comments = comments

    async def get_card_comments(self, card_id: str, since_id: int | None = None) -> list[dict[str, Any]]:
        return [c for c in self._comments if since_id is None or c["id"] > since_id]


@pytest.mark.asyncio
async def test_the_filter_skips_the_login_it_is_given_and_nothing_else() -> None:
    comments = [
        {"id": 1, "author": "ACME-Coord[BOT]", "body": "Needs human: Which?", "created_at": "2026-10-14T10:00:00Z"},
        {"id": 2, "author": "vivi-coordinare[bot]", "body": "Needs human: Which?", "created_at": "2026-10-14T10:00:01Z"},
        {"id": 3, "author": "alice", "body": "Which?", "created_at": "2026-10-14T10:00:02Z"},
    ]
    events = await fetch_new_issue_comments(
        "ITEM_1", None, _Board(comments), 42, self_logins=normalize_logins(["acme-coord[bot]"]),
    )
    assert [e.comment_id for e in events] == [2, 3]


class _Stub(CardIdentityMap):
    """The comment read the route node makes, with the identity this test gives it."""

    def __init__(self, comments: list[dict[str, Any]], logins: frozenset[str]) -> None:
        self._comments = comments
        self._logins = logins
        self._remember_issue_numbers({"ITEM_1": 42})

    async def get_issue_details(self, card_id: str) -> dict[str, Any]:
        return {}

    async def get_issue_comments(self, issue_number: int, since_id: int | None = None) -> list[dict[str, Any]]:
        return [c for c in self._comments if since_id is None or c["id"] > since_id]

    async def self_logins(self) -> frozenset[str]:
        return self._logins


async def _route(comments: list[dict[str, Any]], logins: frozenset[str], configured: list[str] | None = None) -> list[int]:
    state = initial_state()
    state["github_service"] = _Stub(comments, logins)
    state["config"] = SimpleNamespace(github_self_logins=list(configured or []))
    state["current_card"] = {"id": "ITEM_1", "issue_number": 42}
    result = await route_issue_comments(state)
    return [c["comment_id"] for c in result.get("card_clarifications") or []]


_BOT_TEXT = "Needs human: Which provider?"


@pytest.mark.asyncio
async def test_a_deployment_with_another_app_name_filters_its_own_comments() -> None:
    comments = [
        {"id": 1, "author": "acme-coord[bot]", "body": _BOT_TEXT, "created_at": "2026-10-14T10:00:00Z"},
        {"id": 2, "author": "alice", "body": "Which provider?", "created_at": "2026-10-14T10:00:01Z"},
    ]
    assert await _route(comments, frozenset({"acme-coord", "acme-coord[bot]"})) == [2]


@pytest.mark.asyncio
async def test_another_deployments_app_is_not_filtered_here() -> None:
    comments = [{"id": 1, "author": "vivi-coordinare[bot]", "body": _BOT_TEXT, "created_at": "2026-10-14T10:00:00Z"}]
    assert await _route(comments, frozenset({"acme-coord", "acme-coord[bot]"})) == [1]


@pytest.mark.asyncio
async def test_a_configured_extra_login_is_filtered_alongside_the_resolved_one() -> None:
    comments = [
        {"id": 1, "author": "helper-bot[bot]", "body": _BOT_TEXT, "created_at": "2026-10-14T10:00:00Z"},
        {"id": 2, "author": "acme-coord[bot]", "body": _BOT_TEXT, "created_at": "2026-10-14T10:00:01Z"},
    ]
    assert await _route(comments, frozenset({"acme-coord[bot]"}), ["Helper-Bot[bot]"]) == []


@pytest.mark.asyncio
async def test_while_unresolved_the_configured_list_alone_filters() -> None:
    comments = [
        {"id": 1, "author": "acme-coord[bot]", "body": _BOT_TEXT, "created_at": "2026-10-14T10:00:00Z"},
        {"id": 2, "author": "alice", "body": _BOT_TEXT, "created_at": "2026-10-14T10:00:01Z"},
    ]
    assert await _route(comments, frozenset(), ["acme-coord[bot]"]) == [2]


@pytest.mark.asyncio
async def test_while_unresolved_with_nothing_configured_nothing_is_filtered() -> None:
    comments = [{"id": 1, "author": "acme-coord[bot]", "body": _BOT_TEXT, "created_at": "2026-10-14T10:00:00Z"}]
    assert await _route(comments, frozenset()) == [1]

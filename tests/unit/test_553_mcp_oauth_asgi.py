"""Spec 553: OAuth ASGI assembly — SDK bearer stack + per-request fingerprint."""

from __future__ import annotations

from typing import Any

import httpx
from mcp.server.auth.provider import AccessToken
from pydantic import SecretStr

import coordinare.mcp.server as mcp_server
from coordinare.mcp.auth import credential_fingerprint
from coordinare.mcp.config import McpOAuthConfig, McpServerConfig
from coordinare.mcp.oauth import IntrospectionTokenVerifier
from coordinare.mcp.server import build_asgi_app

_OAUTH_TOKEN = "oauth-access-token"
_UNAUTHORIZED = 401


def _oauth_config(**overrides: object) -> McpServerConfig:
    kwargs: dict[str, object] = {
        "transport": "http",
        "dashboard_token": SecretStr("y" * 32),
        "oauth": {
            "enabled": True,
            "issuer": "https://as.example.com",
            "introspection_url": "https://as.example.com/introspect",
            "client_id": "coordinare-mcp",
            "client_secret": "s" * 32,
        },
    }
    kwargs.update(overrides)
    return McpServerConfig.model_validate(kwargs)


class _StubVerifier(IntrospectionTokenVerifier):
    def __init__(self, accepted: set[str]) -> None:
        super().__init__(
            McpOAuthConfig.model_validate({"enabled": True, "issuer": "https://as.example.com"}),
        )
        self._accepted = accepted

    async def verify_token(self, token: str) -> AccessToken | None:
        if token not in self._accepted:
            return None
        return AccessToken(
            token=token,
            client_id="agent-7",
            scopes=["coordinare:mcp"],
            expires_at=None,
            resource=None,
            subject="agent-7",
            claims={},
        )


def _app_with(verifier: _StubVerifier) -> Any:
    return build_asgi_app(_oauth_config(), token_verifier=verifier)


async def _request(app: Any, token: str | None) -> tuple[int, dict[str, str]]:
    transport = httpx.ASGITransport(app=app)
    headers = {"Accept": "application/json"} if token is None else {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
    }
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=transport, base_url="http://test",
    ) as client:
        response = await client.get("/mcp", headers=headers)
    return response.status_code, dict(response.headers)


async def test_unauthenticated_request_gets_sdk_challenge() -> None:
    app = _app_with(_StubVerifier({_OAUTH_TOKEN}))
    status, headers = await _request(app, None)
    assert status == _UNAUTHORIZED
    assert any(key.lower() == "www-authenticate" for key in headers)


async def test_rejected_token_gets_401() -> None:
    app = _app_with(_StubVerifier(set()))
    status, _ = await _request(app, _OAUTH_TOKEN)
    assert status == _UNAUTHORIZED


async def test_accepted_token_passes_the_bearer_gate() -> None:
    app = _app_with(_StubVerifier({_OAUTH_TOKEN}))
    status, _ = await _request(app, _OAUTH_TOKEN)
    assert status != _UNAUTHORIZED


async def test_fingerprint_set_from_the_presented_token() -> None:
    app = _app_with(_StubVerifier({_OAUTH_TOKEN}))
    await _request(app, _OAUTH_TOKEN)
    assert mcp_server._caller_fingerprint.get() == credential_fingerprint(_OAUTH_TOKEN)
    assert mcp_server._caller_client_id.get() == "agent-7"


async def test_static_token_rejected_when_oauth_enabled() -> None:
    static_token = "x" * 32
    config = _oauth_config(agent_token=SecretStr(static_token))
    app = build_asgi_app(config, token_verifier=_StubVerifier(set()))
    status, _ = await _request(app, static_token)
    assert status == _UNAUTHORIZED


async def test_oauth_disabled_uses_the_static_gate() -> None:
    config = McpServerConfig.model_validate({
        "transport": "http",
        "agent_token": SecretStr("x" * 32),
        "dashboard_token": SecretStr("y" * 32),
        "oauth": {"enabled": False},
    })
    app = build_asgi_app(config)
    status, _ = await _request(app, "x" * 32)
    assert status != _UNAUTHORIZED

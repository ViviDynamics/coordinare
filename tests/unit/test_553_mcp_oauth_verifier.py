"""Spec 553: the RFC 7662 introspection token verifier (FR2), fail closed."""

from __future__ import annotations

import base64
import json
from typing import cast

import httpx
import structlog

from coordinare.mcp.config import McpOAuthConfig
from coordinare.mcp.oauth import IntrospectionTokenVerifier

TOKEN = "secret-token-abc"
_TEST_EXP = 1910000000


def _config(**overrides: object) -> McpOAuthConfig:
    kwargs: dict[str, object] = {
        "enabled": True,
        "issuer": "https://as.example.com",
        "introspection_url": "https://as.example.com/introspect",
        "client_id": "coordinare-mcp",
        "client_secret": "s" * 32,
    }
    kwargs.update(overrides)
    return McpOAuthConfig.model_validate(kwargs)


def _verifier(
    responses: list[object],
    **overrides: object,
) -> tuple[IntrospectionTokenVerifier, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        outcome = responses[min(len(requests) - 1, len(responses) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return cast("httpx.Response", outcome)

    verifier = IntrospectionTokenVerifier(
        _config(**overrides),
        transport=httpx.MockTransport(handler),
    )
    return verifier, requests


def _active_payload(**overrides: object) -> bytes:
    payload: dict[str, object] = {
        "active": True,
        "client_id": "agent-7",
        "scope": "coordinare:mcp coordinare:write",
        "exp": _TEST_EXP,
        "sub": "agent-7",
        "aud": "https://mcp.example.com/mcp",
    }
    payload.update(overrides)
    return json.dumps(payload).encode()


async def test_active_token_maps_to_access_token() -> None:
    verifier, requests = _verifier([httpx.Response(200, content=_active_payload())])
    access = await verifier.verify_token(TOKEN)
    assert access is not None
    assert access.client_id == "agent-7"
    assert access.scopes == ["coordinare:mcp", "coordinare:write"]
    assert access.subject == "agent-7"
    assert access.expires_at == _TEST_EXP
    assert access.token == TOKEN
    body = requests[0].read().decode()
    assert f"token={TOKEN}" in body
    creds = requests[0].headers.get("Authorization", "")
    assert creds.startswith("Basic ")
    assert base64.b64decode(creds[6:]).decode() == f"coordinare-mcp:{'s' * 32}"


async def test_inactive_token_denies_without_error_log() -> None:
    verifier, _ = _verifier([httpx.Response(200, content=b'{"active": false}')])
    with structlog.testing.capture_logs() as captured:
        assert await verifier.verify_token(TOKEN) is None
    assert not [log for log in captured if log["level"] == "error"]


async def test_server_error_fails_closed_with_error_log() -> None:
    verifier, _ = _verifier([httpx.Response(500, content=b"boom")])
    with structlog.testing.capture_logs() as captured:
        assert await verifier.verify_token(TOKEN) is None
    failures = [log for log in captured if log.get("event") == "mcp_oauth.introspection_failed"]
    assert failures
    assert all(TOKEN not in str(log) for log in failures)


async def test_malformed_payload_fails_closed() -> None:
    verifier, _ = _verifier([httpx.Response(200, content=b"not-json")])
    with structlog.testing.capture_logs() as captured:
        assert await verifier.verify_token(TOKEN) is None
    assert any(log.get("event") == "mcp_oauth.introspection_failed" for log in captured)


async def test_transport_error_fails_closed() -> None:
    verifier, _ = _verifier([httpx.ConnectError("dns failure")])
    with structlog.testing.capture_logs() as captured:
        assert await verifier.verify_token(TOKEN) is None
    assert any(log.get("event") == "mcp_oauth.introspection_failed" for log in captured)


async def test_audience_mismatch_denies() -> None:
    verifier, _ = _verifier(
        [httpx.Response(200, content=_active_payload())],
        resource_server_url="https://other.example.com/mcp",
    )
    with structlog.testing.capture_logs() as captured:
        assert await verifier.verify_token(TOKEN) is None
    assert any(log.get("event") == "mcp_oauth.token_rejected" for log in captured)


async def test_without_client_credentials_no_auth_header() -> None:
    verifier, requests = _verifier(
        [httpx.Response(200, content=_active_payload())],
        client_id=None,
        client_secret=None,
    )
    assert await verifier.verify_token(TOKEN) is not None
    assert "Authorization" not in requests[0].headers


async def test_active_as_string_never_admits() -> None:
    verifier, _ = _verifier([httpx.Response(200, content=b'{"active": "false"}')])
    assert await verifier.verify_token(TOKEN) is None


async def test_json_array_payload_fails_closed() -> None:
    verifier, _ = _verifier([httpx.Response(200, content=b"[]")])
    with structlog.testing.capture_logs() as captured:
        assert await verifier.verify_token(TOKEN) is None
    assert any(log.get("event") == "mcp_oauth.introspection_failed" for log in captured)


async def test_null_payload_fails_closed() -> None:
    verifier, _ = _verifier([httpx.Response(200, content=b"null")])
    with structlog.testing.capture_logs() as captured:
        assert await verifier.verify_token(TOKEN) is None
    assert any(log.get("event") == "mcp_oauth.introspection_failed" for log in captured)


async def test_non_int_exp_fails_closed() -> None:
    verifier, _ = _verifier([httpx.Response(200, content=_active_payload(exp="abc"))])
    with structlog.testing.capture_logs() as captured:
        assert await verifier.verify_token(TOKEN) is None
    assert any(log.get("event") == "mcp_oauth.introspection_failed" for log in captured)


async def test_audience_list_single_entry_maps_to_resource() -> None:
    verifier, _ = _verifier(
        [httpx.Response(200, content=_active_payload(aud=["https://mcp.example.com/mcp"]))],
        resource_server_url="https://mcp.example.com/mcp",
    )
    access = await verifier.verify_token(TOKEN)
    assert access is not None
    assert access.resource == "https://mcp.example.com/mcp"


async def test_non_string_audience_denies() -> None:
    verifier, _ = _verifier([httpx.Response(200, content=_active_payload(aud=123))])
    assert await verifier.verify_token(TOKEN) is None

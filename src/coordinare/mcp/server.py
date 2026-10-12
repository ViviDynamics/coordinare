"""MCP server assembly, tool registry, and transport selection (T014).

HTTP (phase 1) mounts the streamable transport at ``/mcp`` behind the bearer
check in :mod:`coordinare.mcp.auth`; stdio (552) serves the same registry over
the local pipe with no bearer gate.
"""

from __future__ import annotations

import asyncio
import functools
import json
import time
from collections.abc import MutableMapping
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Literal

import structlog
from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend, RequireAuthMiddleware
from mcp.server.mcpserver import MCPServer
from starlette.middleware.authentication import AuthenticationMiddleware

from coordinare.mcp.audit import emit_tool_invoked
from coordinare.mcp.auth import check_bearer, credential_fingerprint
from coordinare.mcp.client import DashboardClient, set_client
from coordinare.mcp.oauth import IntrospectionTokenVerifier
from coordinare.mcp.tools import REGISTRY, ToolDefinition

if TYPE_CHECKING:
    from collections.abc import MutableMapping

    from mcp.server.auth.provider import TokenVerifier
    from pydantic import SecretStr
    from starlette.applications import Starlette

    from coordinare.mcp.config import McpServerConfig

log = structlog.get_logger(__name__)

#: Identity of the caller, derived from the bearer token per request (US6).
_caller_fingerprint: ContextVar[str] = ContextVar("mcp_caller_fingerprint", default="")

#: Principal identity of the caller in OAuth mode (553 FR4); empty in static mode.
_caller_client_id: ContextVar[str] = ContextVar("mcp_caller_client_id", default="")

_401_BODY = json.dumps(
    {
        "error": {
            "kind": "auth_failed",
            "status": 401,
            "detail": "missing or invalid bearer token",
            "recovery_hint": "send Authorization: Bearer <COORDINARE_MCP_TOKEN>",
        },
    },
).encode()


class BearerAuthMiddleware:
    """Pure-ASGI bearer check on every HTTP request (FR3, US1)."""

    def __init__(self, app: Any, token: SecretStr) -> None:
        self._app = app
        self._token = token

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        auth_header = None
        for key, value in scope["headers"]:
            if key == b"authorization":
                auth_header = value.decode("latin-1")
                break
        if not check_bearer(auth_header, self._token):
            log.info("mcp_server.auth_refused")
            body = _401_BODY
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                    ],
                },
            )
            await send({"type": "http.response.body", "body": body})
            return
        if auth_header is not None:
            _, _, value = auth_header.partition(" ")
            _caller_fingerprint.set(credential_fingerprint(value))
        await self._app(scope, receive, send)


def _register_tools(mcp_server: MCPServer) -> None:
    for tool in REGISTRY:
        _register(mcp_server, tool)


def _extract_action(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str | None:
    action = kwargs.get("action")
    if action is None:
        for arg in args:
            action = getattr(arg, "action", None)
            if action is not None:
                break
    return action


def _audit_wrapped(tool: ToolDefinition, handler: Any) -> Any:
    @functools.wraps(handler)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        action = _extract_action(args, kwargs)
        started = time.perf_counter()
        identity: dict[str, Any] = {"credential_fingerprint": _caller_fingerprint.get()}
        if principal := _caller_client_id.get():
            identity["caller_client_id"] = principal
        try:
            result = await handler(*args, **kwargs)
        except Exception as exc:
            emit_tool_invoked(
                tool=tool.name,
                action=action,
                outcome="error",
                error_kind=type(exc).__name__,
                **identity,
                duration_ms=round((time.perf_counter() - started) * 1000),
            )
            raise
        error_kind: str | None = None
        outcome: Literal["ok", "error"] = "ok"
        if isinstance(result, dict):
            error = result.get("error")
            if error:
                outcome = "error"
                error_kind = error.get("kind") if isinstance(error, dict) else str(error)
        emit_tool_invoked(
            tool=tool.name,
            action=action,
            outcome=outcome,
            error_kind=error_kind,
            **identity,
            duration_ms=round((time.perf_counter() - started) * 1000),
        )
        return result

    return wrapper


def _register(mcp_server: MCPServer, tool: ToolDefinition) -> None:
    handler = tool.handler
    if tool.annotations.destructive_hint:
        handler = _audit_wrapped(tool, handler)
    mcp_server.add_tool(
        handler,
        name=tool.name,
        description=tool.description,
        annotations=tool.annotations,
    )


def _build_mcp_server(config: McpServerConfig) -> MCPServer:
    """Assemble the MCPServer with the registry tools, shared by both transports."""
    set_client(DashboardClient(config))
    mcp_server = MCPServer(name=config.server_name)
    _register_tools(mcp_server)
    return mcp_server


_STATIC_CREDENTIAL_REQUIRED_MSG = "static-token mode requires an agent token"
_STDIO_CREDENTIAL_REQUIRED_MSG = "stdio transport requires the static agent token"


class _OAuthAuditFingerprint:
    """Set the per-request audit identity from the authenticated access token (553 FR4).

    Runs inside ``AuthenticationMiddleware``, so ``scope["user"]`` already
    carries a verified ``AccessToken``; the fingerprint and principal reach the
    audit context via the module contextvars.
    """

    def __init__(self, app: Any) -> None:
        self._app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        user = scope.get("user")
        access_token = getattr(user, "access_token", None)
        if access_token is not None:
            _caller_fingerprint.set(credential_fingerprint(access_token.token))
            _caller_client_id.set(getattr(access_token, "client_id", "") or "")
        await self._app(scope, receive, send)


class _RequireAuth(RequireAuthMiddleware):
    """SDK ``RequireAuthMiddleware`` with non-HTTP scopes (lifespan) passed through."""

    async def __call__(
        self,
        scope: MutableMapping[str, Any],
        receive: Any,
        send: Any,
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        await super().__call__(scope, receive, send)


def build_asgi_app(
    config: McpServerConfig,
    *,
    token_verifier: TokenVerifier | None = None,
) -> Starlette:
    """Assemble the MCP ASGI app: registry tools, streamable HTTP, bearer gate."""
    mcp_server = _build_mcp_server(config)
    streamable = mcp_server.streamable_http_app(stateless_http=True, streamable_http_path="/mcp")
    streamable.middleware_stack = streamable.build_middleware_stack()
    if config.oauth.enabled:
        verifier = token_verifier if token_verifier is not None else IntrospectionTokenVerifier(config.oauth)
        streamable.middleware_stack = AuthenticationMiddleware(
            _RequireAuth(
                _OAuthAuditFingerprint(streamable.middleware_stack),
                required_scopes=config.oauth.required_scopes,
            ),
            backend=BearerAuthBackend(
                verifier,
                resource_server_url=config.oauth.resource_server_url,
            ),
        )
        return streamable
    agent_token = config.agent_token
    if agent_token is None:
        raise ValueError(_STATIC_CREDENTIAL_REQUIRED_MSG)
    streamable.middleware_stack = BearerAuthMiddleware(
        streamable.middleware_stack,
        agent_token,
    )
    return streamable


def run_stdio(config: McpServerConfig) -> None:
    """Run the MCP server over stdio (552 FR2/FR3): local trust, no bearer gate.

    The stream is inherited from the operator's shell, so authentication
    happens at the shell; the audit fingerprint identifies the agent token
    this process runs with (FR5).
    """
    agent_token = config.agent_token
    if agent_token is None:
        raise ValueError(_STDIO_CREDENTIAL_REQUIRED_MSG)
    mcp_server = _build_mcp_server(config)
    _caller_fingerprint.set(credential_fingerprint(agent_token.get_secret_value()))
    asyncio.run(mcp_server.run_stdio_async())


__all__ = ["BearerAuthMiddleware", "build_asgi_app", "run_stdio"]

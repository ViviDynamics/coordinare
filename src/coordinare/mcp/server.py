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
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Literal

import structlog
from mcp.server.mcpserver import MCPServer

from coordinare.mcp.audit import emit_tool_invoked
from coordinare.mcp.auth import check_bearer, credential_fingerprint
from coordinare.mcp.client import DashboardClient, set_client
from coordinare.mcp.tools import REGISTRY, ToolDefinition

if TYPE_CHECKING:
    from pydantic import SecretStr

    from coordinare.mcp.config import McpServerConfig

log = structlog.get_logger(__name__)

#: Identity of the caller, derived from the bearer token per request (US6).
_caller_fingerprint: ContextVar[str] = ContextVar("mcp_caller_fingerprint", default="")

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
        try:
            result = await handler(*args, **kwargs)
        except Exception as exc:
            emit_tool_invoked(
                tool=tool.name,
                action=action,
                outcome="error",
                error_kind=type(exc).__name__,
                credential_fingerprint=_caller_fingerprint.get(),
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
            credential_fingerprint=_caller_fingerprint.get(),
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


def build_asgi_app(config: McpServerConfig) -> Any:
    """Assemble the MCP ASGI app: registry tools, streamable HTTP, bearer gate."""
    mcp_server = _build_mcp_server(config)
    return BearerAuthMiddleware(
        mcp_server.streamable_http_app(stateless_http=True, streamable_http_path="/mcp"),
        config.agent_token,
    )


def run_stdio(config: McpServerConfig) -> None:
    """Run the MCP server over stdio (552 FR2/FR3): local trust, no bearer gate.

    The stream is inherited from the operator's shell, so authentication
    happens at the shell; the audit fingerprint identifies the agent token
    this process runs with (FR5).
    """
    mcp_server = _build_mcp_server(config)
    _caller_fingerprint.set(credential_fingerprint(config.agent_token.get_secret_value()))
    asyncio.run(mcp_server.run_stdio_async())


__all__ = ["BearerAuthMiddleware", "build_asgi_app", "run_stdio"]

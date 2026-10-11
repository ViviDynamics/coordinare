"""Typed MCP tool functions grouped by domain, plus the tool registry (T011).

The registry is immutable at runtime: every domain module contributes a static
``TOOLS`` tuple and this package assembles them at import time per data-model.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any

    from mcp.types import ToolAnnotations

ToolDomain = Literal["config", "symphonies", "personas", "lifecycle", "state"]

#: A (method, path template) pair, e.g. ``("PUT", "/api/config/global")``.
RESTCall = tuple[str, str]

#: REST surfaces deliberately NOT proxied in phase 1 (contracts/mcp-tools.md).
DEFERRED_REST_PREFIXES = (
    "/oidc",
    "/static",
    "/events",
    "/webhook",
    "/api/assistant",
    "/health",
    "/live",
    "/ready",
)

#: Human dashboard page paths (HTML), deferred like the static assets.
DEFERRED_PAGE_PATHS = (
    "/",
    "/performers",
    "/personas",
    "/history",
    "/symphonies",
    "/symphonies/{name}",
    "/admin/config",
    "/config",
)


@dataclass(frozen=True)
class ToolDefinition:
    """Static registration record describing one MCP tool (data-model.md)."""

    name: str
    domain: ToolDomain
    description: str
    input_model: type[Any]
    annotations: ToolAnnotations
    rest_calls: tuple[RESTCall, ...]
    handler: Callable[..., Any]


def _all_tools() -> tuple[ToolDefinition, ...]:
    from . import (
        config_tools,
        lifecycle_tools,
        persona_tools,
        state_tools,
        symphony_tools,
    )

    modules: tuple[tuple[ToolDefinition, ...], ...] = (
        config_tools.TOOLS,
        symphony_tools.TOOLS,
        persona_tools.TOOLS,
        lifecycle_tools.TOOLS,
        state_tools.TOOLS,
    )
    return tuple(tool for module_tools in modules for tool in module_tools)


#: Immutable registry assembled at import time (data-model.md).
REGISTRY: tuple[ToolDefinition, ...] = _all_tools()


def find_tool(name: str) -> ToolDefinition | None:
    """Return the tool registered under ``name``, or None."""
    for tool in REGISTRY:
        if tool.name == name:
            return tool
    return None


def is_deferred(path: str) -> bool:
    """True when a dashboard route is on the asserted defer-list."""
    if any(path.startswith(prefix) for prefix in DEFERRED_REST_PREFIXES):
        return True
    return path in DEFERRED_PAGE_PATHS


__all__ = [
    "DEFERRED_PAGE_PATHS",
    "DEFERRED_REST_PREFIXES",
    "REGISTRY",
    "RESTCall",
    "ToolDefinition",
    "ToolDomain",
    "find_tool",
    "is_deferred",
]

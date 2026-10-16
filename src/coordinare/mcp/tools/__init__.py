"""Typed MCP tool functions grouped by domain, plus the tool registry (T011).

The registry is immutable at runtime: every domain module contributes a static
``TOOLS`` tuple and this package assembles them at import time per data-model.md.
"""

from __future__ import annotations

from . import (
    config_tools,
    event_tools,
    lifecycle_tools,
    persona_tools,
    state_tools,
    stats_tools,
    symphony_tools,
)
from ._registry import (
    DEFERRED_PAGE_PATHS,
    DEFERRED_REST_PREFIXES,
    RESTCall,
    ToolDefinition,
    ToolDomain,
)


def _all_tools() -> tuple[ToolDefinition, ...]:
    modules: tuple[tuple[ToolDefinition, ...], ...] = (
        config_tools.TOOLS,
        symphony_tools.TOOLS,
        persona_tools.TOOLS,
        lifecycle_tools.TOOLS,
        state_tools.TOOLS,
        stats_tools.TOOLS,
        event_tools.TOOLS,
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

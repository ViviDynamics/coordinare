"""Shared registration types for the MCP tool registry (T011).

Lives outside ``__init__`` so the domain modules can import these without a
circular import: the package assembles the registry from the domain modules'
``TOOLS`` tuples, and the domain modules need these types to declare them.
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
    "/api/account",  # Human browser identity, not an agent operator capability.
    "/static",
    "/events",
    "/webhook",
    "/api/assistant",
    "/api/vocabulary",  # 136: display labels for the human dashboard, static data.
    "/api/personas/{role}/history",  # 130: persona version history, read by the transcript viewer (#133).
    "/api/runs",  # 132 and 133: a run's full log, and run transcripts for the viewer (human dashboard).
    "/api/card-runs",  # 133: a card's runs across symphonies, for the card detail.
    "/api/run-lookup",  # 133: a run id resolved to its address, for activity links.
    "/api/cards",  # 147: a card's pipeline timeline (stages, runs, lifecycle, cost), for the card detail.
    "/api/history",  # 150: the History page's paginated run listing (human dashboard, read-only).
    "/api/symphonies/{name}/bootstrap-runs",  # Bootstrap run detail for the human dashboard.
    "/health",
    "/live",
    "/ready",
)

#: Human dashboard page paths (HTML), deferred like the static assets.
DEFERRED_PAGE_PATHS = (
    "/",
    "/login",
    "/performers",
    "/performers/{role}",
    "/runs/{run}",  # 133: the transcript viewer, by run id.
    "/runs/{symphony}/{card}/{run}",  # 133: the transcript viewer, by address.
    "/personas",
    "/history",
    "/logs",
    "/glossary",
    "/symphonies",
    "/symphonies/{name}",
    "/symphonies/{name}/bootstrap/{run}",
    "/cards/{symphony}/{card}",  # 233: the card page, a card's timeline by address.
    "/cards/{rest:path}",  # 233: any other /cards path, served as the shell with a 404.
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


__all__ = [
    "DEFERRED_PAGE_PATHS",
    "DEFERRED_REST_PREFIXES",
    "RESTCall",
    "ToolDefinition",
    "ToolDomain",
]

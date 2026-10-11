"""State tools: snapshot, performer logs, activity feed, and metrics inspection (US3)."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from mcp.types import ToolAnnotations
from pydantic import BaseModel

from coordinare.mcp.client import DashboardClient, ToolResult, get_client
from coordinare.mcp.tools._registry import ToolDefinition

_READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)


class _NoParams(BaseModel):
    """Input shape for tools that take no parameters."""


async def get_metrics_handler(client: DashboardClient) -> ToolResult:
    """Fetch the Prometheus exposition text from the health server (T015)."""
    return await client.get_text("/metrics", timeout_seconds=client.read_budget)


async def get_metrics() -> dict[str, Any]:
    """Read the daemon's Prometheus metrics exposition (health server, verbatim)."""
    return (await get_metrics_handler(get_client())).model_dump(exclude_none=True)


async def get_state_handler(client: DashboardClient) -> ToolResult:
    """Fetch the JSON board state snapshot (T025)."""
    return await client.get_json("/api/state", timeout_seconds=client.read_budget)


async def get_state() -> dict[str, Any]:
    """Read the dashboard board state snapshot (same payload as the SSE stream)."""
    return (await get_state_handler(get_client())).model_dump(exclude_none=True)


async def get_performer_logs_handler(client: DashboardClient) -> ToolResult:
    """Fetch the buffered head of the performer-logs stream (T025)."""
    return await client.get_stream_head(
        "/api/performer-logs", timeout_seconds=client.read_budget,
    )


async def get_performer_logs() -> dict[str, Any]:
    """Read the buffered tail of live performer output (no SSE proxying)."""
    return (await get_performer_logs_handler(get_client())).model_dump(exclude_none=True)


class GetActivityInput(BaseModel):
    card_id: str | None = None


async def get_activity_handler(
    input: GetActivityInput, client: DashboardClient,
) -> ToolResult:
    """Fetch the activity feed, optionally narrowed to one card (T025)."""
    path = "/api/activity"
    if input.card_id:
        path = f"{path}?card_id={quote(input.card_id, safe='')}"
    return await client.get_json(path, timeout_seconds=client.read_budget)


async def get_activity(card_id: str | None = None) -> dict[str, Any]:
    """Read the newest-first activity feed, optionally filtered by card."""
    parsed = GetActivityInput(card_id=card_id)
    return (await get_activity_handler(parsed, get_client())).model_dump(exclude_none=True)


TOOLS: tuple[ToolDefinition, ...] = (
    ToolDefinition(
        name="get_metrics",
        domain="state",
        description=(
            "Read the daemon's Prometheus metrics exposition verbatim (health server)."
        ),
        input_model=_NoParams,
        annotations=_READ_ONLY,
        rest_calls=(("GET", "{health_url}/metrics"),),
        handler=get_metrics,
    ),
    ToolDefinition(
        name="get_state",
        domain="state",
        description="Read the dashboard board state snapshot (same payload as the SSE stream).",
        input_model=_NoParams,
        annotations=_READ_ONLY,
        rest_calls=(("GET", "/api/state"),),
        handler=get_state,
    ),
    ToolDefinition(
        name="get_performer_logs",
        domain="state",
        description="Read the buffered head of live performer output; never proxies SSE.",
        input_model=_NoParams,
        annotations=_READ_ONLY,
        rest_calls=(("GET", "/api/performer-logs"),),
        handler=get_performer_logs,
    ),
    ToolDefinition(
        name="get_activity",
        domain="state",
        description="Read the activity feed (newest-first), optionally narrowed to one card.",
        input_model=GetActivityInput,
        annotations=_READ_ONLY,
        rest_calls=(("GET", "/api/activity"),),
        handler=get_activity,
    ),
)

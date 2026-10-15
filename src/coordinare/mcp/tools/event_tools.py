"""Read persistent incident events through the authenticated dashboard client."""
from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict

from coordinare.mcp.client import DashboardClient, ToolResult, get_client
from coordinare.mcp.tools._registry import ToolDefinition
from coordinare.services.event_store_params import EventQuery

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)


async def query_events_handler(input: EventQuery, client: DashboardClient) -> ToolResult:
    path = "/api/events?" + urlencode(input.model_dump(exclude_none=True))
    return await client.get_json(path, timeout_seconds=client.read_budget)


class QueryEventsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: EventQuery


async def query_events(query: EventQuery) -> dict[str, Any]:
    """Query events newest first, by insertion ID when searching message text."""
    parsed = EventQuery.model_validate(query)
    return (await query_events_handler(parsed, get_client())).model_dump(exclude_none=True)


TOOLS = (
    ToolDefinition(
        name="query_events", domain="state",
        description="Query redacted persistent events with time, symphony, card, run, stage, source, kind, level and text filters. Returns at most 500 rows and an opaque cursor. Text results use insertion order, differing from timestamp order only for late arriving performer events. Other results use timestamp order. Symphony is a display filter.",
        input_model=QueryEventsInput, annotations=READ_ONLY,
        rest_calls=(("GET", "/api/events"),), handler=query_events,
    ),
)

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict

from coordinare.mcp.client import DashboardClient, ToolResult, get_client
from coordinare.mcp.tools._registry import ToolDefinition
from coordinare.services.stats_params import StatsQuery

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)


class QueryStatsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: StatsQuery


async def query_stats_handler(input: StatsQuery, client: DashboardClient) -> ToolResult:
    path = "/api/stats?" + urlencode(input.model_dump(exclude_none=True))
    return await client.get_json(path, timeout_seconds=client.read_budget)


async def query_stats(query: StatsQuery) -> dict[str, Any]:
    """Read the dashboard's bounded run aggregates and card flow measures."""
    parsed = StatsQuery.model_validate(query)
    return (await query_stats_handler(parsed, get_client())).model_dump(exclude_none=True)


TOOLS = (
    ToolDefinition(
        name="query_stats",
        domain="state",
        description="Read run statistics grouped by agent (persona), stage, symphony, dispatch model, backend or outcome, with 24h, 7d, 30d or all retained windows. Returns daily series, tokens and cost, retries, bounces, failure reasons, card flow, definitions and coverage. Symphony is a display filter restricted to configured symphonies.",
        input_model=QueryStatsInput,
        annotations=READ_ONLY,
        rest_calls=(("GET", "/api/stats"),),
        handler=query_stats,
    ),
)

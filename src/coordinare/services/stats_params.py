from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

MAX_FILTER_CHARS = 1024
DEFAULT_TOP_N = 5
MAX_TOP_N = 20
StatsDimension = Literal["agent", "stage", "symphony", "model", "backend", "outcome"]
StatsWindow = Literal["24h", "7d", "30d", "all"]


class StatsQuery(BaseModel):
    """Shared HTTP and MCP contract. Agent means the recorded persona name."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    group_by: StatsDimension = "agent"
    window: StatsWindow = "7d"
    symphony: str | None = Field(None, max_length=MAX_FILTER_CHARS)
    stage: str | None = Field(None, max_length=MAX_FILTER_CHARS)
    agent: str | None = Field(None, max_length=MAX_FILTER_CHARS)
    model: str | None = Field(None, max_length=MAX_FILTER_CHARS)
    backend: str | None = Field(None, max_length=MAX_FILTER_CHARS)
    outcome: (
        Literal["success", "changes_requested", "blocked", "system_error", "cancelled", "unknown"]
        | None
    ) = None
    top_n: int = Field(DEFAULT_TOP_N, ge=1, le=MAX_TOP_N)

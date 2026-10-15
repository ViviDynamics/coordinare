from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

DEFAULT_RETENTION_DAYS = 30
DEFAULT_MAX_BYTES = 2 * 1024 * 1024 * 1024
MIN_MAX_BYTES = 64 * 1024
MAX_RETENTION_DAYS = 3650


class EventStoreConfig(BaseModel):
    """Always-on event history. Only retention budgets are configurable."""

    model_config = ConfigDict(extra="forbid")

    retention_days: int = Field(default=DEFAULT_RETENTION_DAYS, ge=1, le=MAX_RETENTION_DAYS)
    max_bytes: int = Field(default=DEFAULT_MAX_BYTES, ge=MIN_MAX_BYTES)

"""Shared validated HTTP and MCP query contract for persistent events."""
from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from coordinare.services.event_store_query import (
    DEFAULT_LIMIT,
    MAX_CURSOR_LENGTH,
    MAX_LIMIT,
    EventFilters,
    utc_timestamp,
    validate_event_cursor,
)

MAX_FILTER_CHARS = 1024
EventText = str | None
EventSource = Literal["daemon", "performer", "bootstrap", "operator"]
EventLevel = Literal["debug", "info", "warning", "warn", "error", "exception", "critical", "fatal"]


class EventQuery(BaseModel):
    """One newest first page. Symphony is a display filter, not authorization."""

    model_config = ConfigDict(extra="forbid")

    start: EventText = Field(None, max_length=MAX_FILTER_CHARS)
    end: EventText = Field(None, max_length=MAX_FILTER_CHARS)
    symphony: EventText = Field(None, max_length=MAX_FILTER_CHARS)
    card_id: EventText = Field(None, max_length=MAX_FILTER_CHARS)
    run_id: EventText = Field(None, max_length=MAX_FILTER_CHARS)
    stage: EventText = Field(None, max_length=MAX_FILTER_CHARS)
    source: EventSource | None = None
    kind: EventText = Field(None, max_length=MAX_FILTER_CHARS)
    level: EventLevel | None = None
    text: EventText = Field(None, max_length=MAX_FILTER_CHARS)
    cursor: EventText = Field(None, max_length=MAX_CURSOR_LENGTH)
    limit: int = Field(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)

    @field_validator("start", "end")
    @classmethod
    def timestamp(cls, value: EventText) -> EventText:
        return utc_timestamp(value) if value is not None else None

    @field_validator("cursor")
    @classmethod
    def valid_cursor(cls, value: EventText) -> EventText:
        if value is not None:
            validate_event_cursor(value)
        return value

    @model_validator(mode="after")
    def ordered_range(self) -> Self:
        if self.start and self.end and self.start > self.end:
            message = "start must be before or equal to end"
            raise ValueError(message)
        return self

    def filters(self) -> EventFilters:
        return EventFilters(**self.model_dump(exclude={"cursor", "limit"}))

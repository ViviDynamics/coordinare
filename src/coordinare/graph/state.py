from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, TypedDict


class CoordinareState(TypedDict):
    current_card: Any | None
    board_snapshot: dict[str, list[str]]
    phase: str
    pending_reviews: list[dict[str, object]]
    last_poll_at: datetime | None
    error_count: int
    github_field_cache: dict[str, object]


def initial_state() -> CoordinareState:
    return {
        "current_card": None,
        "board_snapshot": {},
        "phase": "idle",
        "pending_reviews": [],
        "last_poll_at": datetime.now(UTC),
        "error_count": 0,
        "github_field_cache": {},
    }

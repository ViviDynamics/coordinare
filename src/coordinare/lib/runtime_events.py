from __future__ import annotations

from typing import Any, Literal

RuntimeCategory = Literal["startup", "heartbeat", "activity", "state_change", "failure", "shutdown"]


def build_runtime_event(
    *,
    category: RuntimeCategory,
    message: str,
    level: str = "info",
    **details: Any,
) -> dict[str, Any]:
    return {
        "category": category,
        "message": message,
        "level": level.lower(),
        **details,
    }


def event_level_for_category(category: RuntimeCategory) -> str:
    if category == "failure":
        return "error"
    if category in {"startup", "shutdown", "state_change"}:
        return "info"
    if category in {"heartbeat", "activity"}:
        return "debug"
    return "info"


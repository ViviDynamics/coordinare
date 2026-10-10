"""Copy human clarification rounds with missing owner attribution filled in."""
from __future__ import annotations

from typing import Any


def annotate_clarifications(
    rounds: list[Any], attribution: dict[str, Any],
) -> list[dict[str, Any]]:
    """Preserve explicit metadata and content without mutating live history."""
    annotated: list[dict[str, Any]] = []
    for round_ in rounds:
        if isinstance(round_, dict):
            entry = dict(round_)
            for key, value in attribution.items():
                entry.setdefault(key, value)
            annotated.append(entry)
        else:
            annotated.append(round_)
    return annotated

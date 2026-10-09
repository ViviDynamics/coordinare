"""Render retained human comments alongside legacy clarification Q&A."""
from __future__ import annotations

from typing import Any


def clarification_comment_lines(entry: dict[str, Any]) -> list[str]:
    """Preserve the original comment text and its source identity."""
    body = entry.get("body")
    if not isinstance(body, str) or not body.strip():
        return []
    identity = ", ".join(
        f"{key}: {entry[key]}"
        for key in ("source", "comment_id", "author")
        if entry.get(key)
    )
    label = f"**Human clarification ({identity}):**" if identity else "**Human clarification:**"
    return [label, body, ""]

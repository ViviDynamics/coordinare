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


def clarification_prompt_section(clarifications: list[dict[str, Any]]) -> list[str]:
    """Preserve current human comments and legacy answered questions for every CLI."""
    if not clarifications:
        return []
    parts = ["", "## Clarification Q&A", ""]
    for entry in clarifications:
        parts += clarification_comment_lines(entry)
        questions = entry.get("questions") or []
        answer = str(entry.get("answer", "")).strip()
        if questions:
            parts.append("**Questions asked:**")
            parts.extend(f"- {question}" for question in questions)
        if answer:
            parts += [f"**Answer:** {answer}", ""]
    return parts

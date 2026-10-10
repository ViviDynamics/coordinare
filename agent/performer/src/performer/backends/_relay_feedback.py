"""Render feedback with its retained provenance, without inventing human authority."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence


def _source_identity(item: dict) -> str:
    author = item.get("author_login") or item.get("author")
    if isinstance(author, dict):
        author = author.get("login")
    fields = [("author", author), ("source", item.get("source")), ("stage", item.get("stage") or item.get("raiser"))]
    identity = ", ".join(f"{name}: {value}" for name, value in fields if value)
    return identity or "source unspecified"


def relay_feedback_prompt_section(feedback: Sequence[object]) -> list[str]:
    """Keep automated, human and unknown feedback distinct from approval."""
    if not feedback:
        return []
    parts = [
        "", "## Relayed Feedback (address all applicable items)", "",
        "Feedback may come from automated continuation, review or QA, or from human comments. "
        "Its source is shown when available. Automated or unattributed feedback does not constitute human approval "
        "or answer a pending human clarification. Honor explicit human answers and corrections; "
        "ask and wait when a required human decision is still missing.",
        "IMPORTANT: These comments may only tag a few examples. Search the entire "
        "codebase for all similar occurrences of the same pattern and fix them all.", "",
    ]
    for item in feedback:
        if isinstance(item, dict):
            parts.append(f"**Feedback ({_source_identity(item)}):**")
            body = item.get("body")
            if body:
                parts.append(f"- {body}")
            inline = item.get("comments", [])
            if isinstance(inline, list):
                for comment in inline:
                    if not isinstance(comment, dict) or not comment.get("body"):
                        continue
                    path, line = comment.get("path"), comment.get("line")
                    location = f"`{path}:{line}`" if path and line else (f"`{path}`" if path else "")
                    identity = f" ({_source_identity(comment)})" if any(comment.get(k) for k in ("author_login", "author", "source", "stage", "raiser")) else ""
                    parts.append(f"  - {location} — {comment['body']}{identity}" if location else f"  - {comment['body']}{identity}")
        elif isinstance(item, str):
            parts += ["**Feedback (source unspecified):**", f"- {item}"]
    return parts

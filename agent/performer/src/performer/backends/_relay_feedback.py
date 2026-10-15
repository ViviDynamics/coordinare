"""Render feedback with its retained provenance, without inventing human authority."""
from __future__ import annotations

from typing import TYPE_CHECKING

from performer.workflows.untrusted import PATH_LIMIT

if TYPE_CHECKING:
    from collections.abc import Sequence


def _location(path: object, line: object) -> str:
    """``path:line`` on one line, capped (spec 178).

    A path is untrusted: git allows newlines in file names, and a newline would
    start a new line of the prompt. It is flattened and capped, and it is never
    placed in a code span. It is not markup-escaped: this text goes to the model,
    not to GitHub's renderer, and escaping would change the file name the model
    has to match (``synthetic\\_review.py``). The GitHub-facing form is in the
    closer's posted review, which uses the full inert escape.
    """
    if not path:
        return ""
    where = " ".join(str(path).split())[:PATH_LIMIT]
    return f"{where}:{line}" if line else where


def _source_identity(item: dict) -> str:
    author = item.get("author_login") or item.get("author")
    if isinstance(author, dict):
        author = author.get("login")
    fields = [("author", author), ("source", item.get("source")), ("stage", item.get("stage") or item.get("raiser"))]
    identity = ", ".join(f"{name}: {value}" for name, value in fields if value)
    return identity or "source unspecified"


def _is_format_repair(item: object) -> bool:
    return isinstance(item, dict) and item.get("kind") == "format_repair"


def _format_repair_section(feedback: Sequence[object]) -> list[str]:
    """223: a format repair asks for the reply's shape, not for code changes.

    Rendered on its own, so the codebase-wide "fix them all" instruction below
    never reaches a role that should only re-emit its JSON object.
    """
    bodies = [item.get("body") for item in feedback if _is_format_repair(item) and item.get("body")]
    if not bodies:
        return []
    return ["", "## Output Format Correction", "", *bodies]


def relay_feedback_prompt_section(feedback: Sequence[object]) -> list[str]:
    """Keep automated, human and unknown feedback distinct from approval."""
    if not feedback:
        return []
    repair = _format_repair_section(feedback)
    feedback = [item for item in feedback if not _is_format_repair(item)]
    if not feedback:
        return repair
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
            body = item.get("body") or item.get("description")
            if not body:
                qa_details = [f"{key.capitalize()}: {item[key]}" for key in ("criterion", "expected", "actual", "test") if key in item]
                if qa_details:
                    body = "\n".join(qa_details)
                    if item.get("id"):
                        body = f"[{item['id']}] {body}"
            if body:
                location = _location(item.get("file") or item.get("path"), item.get("line"))
                parts.append(f"- {location} — {body}" if location else f"- {body}")
            inline = item.get("comments", [])
            if isinstance(inline, list):
                for comment in inline:
                    if not isinstance(comment, dict) or not comment.get("body"):
                        continue
                    location = _location(comment.get("path"), comment.get("line"))
                    identity = f" ({_source_identity(comment)})" if any(comment.get(k) for k in ("author_login", "author", "source", "stage", "raiser")) else ""
                    parts.append(f"  - {location} — {comment['body']}{identity}" if location else f"  - {comment['body']}{identity}")
        elif isinstance(item, str):
            parts += ["**Feedback (source unspecified):**", f"- {item}"]
    return parts + repair

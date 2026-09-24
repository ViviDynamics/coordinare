"""Pointer section rendering for documenter workflow (spec 171)."""
from __future__ import annotations

from performer.workflows.documenter.models import POINTER_MARKERS, POINTER_MAX_LINES

__all__ = [
    "render_pointer_section",
    "replace_between_markers",
]


def render_pointer_section(entrypoint: str, first_three: list[tuple[str, str]]) -> str:
    """Render the pointer section for AGENTS.md or CLAUDE.md (FR-011).

    The section is a template under 40 lines that names:
    - What the wiki is
    - The entrypoint path
    - The three pages to read first
    - The rule about reading the index first and loading pages as needed

    Args:
        entrypoint: Path to the README/index page.
        first_three: List of (title, path) tuples for the first three pages.

    Returns:
        The pointer section between markers.
    """
    docs_dir = entrypoint.rsplit("/", 1)[0] + "/" if "/" in entrypoint else "./"
    lines = [
        POINTER_MARKERS[0],  # Start marker
        "",
        "# Living Documentation",
        "",
        f"This repository maintains a living wiki in `{docs_dir}` that serves as the",
        "source of truth for how the project works. The wiki is designed to be read",
        "by both humans and AI agents.",
        "",
        f"**Start here**: [`{entrypoint}`]({entrypoint})",
        "",
        "**First reads** before making changes:",
    ]

    for title, path in first_three:
        lines.append(f"- [`{title}`]({path})")

    lines.extend(
        [
            "",
            "**Rules**:",
            "- Read the index first to understand the structure and purpose of each page.",
            "- Load pages as you need them; do not copy wiki content into this file.",
            "- Keep this pointer section short and current; it links, never duplicates.",
            "",
            POINTER_MARKERS[1],  # End marker
        ],
    )

    result = "\n".join(lines)

    # Check line count
    line_count = len(result.split("\n"))
    if line_count > POINTER_MAX_LINES:
        # Trim to fit (this shouldn't happen with the basic template)
        trimmed = "\n".join(result.split("\n")[: POINTER_MAX_LINES - 2])
        result = trimmed + "\n" + POINTER_MARKERS[1]

    return result


def replace_between_markers(text: str, section: str) -> str:
    """Replace content between markers or append if missing (FR-011).

    Replaces the region between POINTER_MARKERS (inclusive), leaves everything
    else byte for byte unchanged. If markers are missing, appends the section
    with a blank line before it.

    Args:
        text: The file content.
        section: The new pointer section (including markers).

    Returns:
        The updated file content.
    """
    start_marker = POINTER_MARKERS[0]
    end_marker = POINTER_MARKERS[1]

    text_normalized = text.replace("\r\n", "\n")

    # Find markers
    start_idx = text_normalized.find(start_marker)
    end_idx = text_normalized.find(end_marker)

    if start_idx >= 0 and end_idx >= 0 and start_idx < end_idx:
        # Replace between markers
        before = text_normalized[:start_idx]
        after = text_normalized[end_idx + len(end_marker) :]
        result = before + section + after
    else:
        # A dangling marker (start without end, or end without start) is removed
        # before the section is appended, so the file never carries two pairs.
        kept = [line for line in text_normalized.split("\n") if line.strip() not in (start_marker, end_marker)]
        text_normalized = "\n".join(kept)
        if text_normalized and not text_normalized.endswith("\n"):
            text_normalized += "\n"
        result = text_normalized + "\n" + section

    return result

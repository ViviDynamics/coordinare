"""Markdown parsing utilities for documenter workflow (spec 171).

Line-based scanner with no external library dependencies.
"""
from __future__ import annotations

from typing import NamedTuple

__all__ = [
    "parse_frontmatter",
    "headings",
    "fenced_blocks",
    "links",
    "backticked_tokens",
    "blockquote_under_h1",
    "body_chars",
    "Heading",
    "Fence",
    "Link",
]


class Heading(NamedTuple):
    """A markdown heading with metadata."""

    level: int
    text: str
    line_no: int
    has_link: bool
    has_code: bool


class Fence(NamedTuple):
    """A fenced code block."""

    language: str
    line_no: int


class Link(NamedTuple):
    """A markdown link [text](target)."""

    text: str
    target: str
    line_no: int


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Parse YAML-style frontmatter from the start of text.

    Args:
        text: The markdown text, possibly with frontmatter.

    Returns:
        (metadata dict, body text) where metadata is empty if no frontmatter.
    """
    text = text.replace("\r\n", "\n")
    lines = text.split("\n")

    if not lines or not lines[0].startswith("---"):
        return {}, text

    # Find the closing ---
    meta_lines = []
    body_start = 1
    for i in range(1, len(lines)):
        if lines[i].startswith("---"):
            body_start = i + 1
            break
        meta_lines.append(lines[i])

    # Parse key: value lines
    meta = {}
    for line in meta_lines:
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()

    body = "\n".join(lines[body_start:])
    return meta, body


def headings(text: str) -> list[Heading]:
    """Extract headings from markdown, ignoring those in fenced blocks.

    Args:
        text: The markdown text.

    Returns:
        List of Heading objects.
    """
    text = text.replace("\r\n", "\n")
    lines = text.split("\n")
    result = []

    # Track which lines are inside fenced blocks
    in_fence = False

    for line_no, line in enumerate(lines):
        stripped = line.lstrip()

        # Toggle fence state
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue

        # Skip headings inside fences
        if in_fence:
            continue

        # Check for heading (# at start after whitespace followed by space or tab)
        if stripped.startswith("#"):
            # Count the hash marks for level
            level = 0
            for char in stripped:
                if char == "#":
                    level += 1
                else:
                    break

            # Must be followed by space or tab
            if level < len(stripped) and stripped[level] in (" ", "\t"):
                text_part = stripped[level:].lstrip()

                # Check for links and code
                has_link = "[" in text_part and "](" in text_part
                has_code = "`" in text_part

                result.append(
                    Heading(
                        level=level,
                        text=text_part,
                        line_no=line_no,
                        has_link=has_link,
                        has_code=has_code,
                    )
                )

    return result


def fenced_blocks(text: str) -> list[Fence]:
    """Extract fenced code blocks.

    Args:
        text: The markdown text.

    Returns:
        List of Fence objects with language and line number.
    """
    text = text.replace("\r\n", "\n")
    lines = text.split("\n")
    result = []

    in_fence = False
    fence_marker = None
    opening_fence_count = 0

    for line_no, line in enumerate(lines):
        stripped = line.lstrip()

        # Check if line contains a fence marker (``` or ~~~)
        fence_type = None
        marker_count = 0

        if stripped.startswith("```"):
            fence_type = "`"
            # Count consecutive backticks
            for char in stripped:
                if char == "`":
                    marker_count += 1
                else:
                    break
        elif stripped.startswith("~~~"):
            fence_type = "~"
            # Count consecutive tildes
            for char in stripped:
                if char == "~":
                    marker_count += 1
                else:
                    break

        # Check if this is a valid fence marker
        # Valid if: >= 3 markers AND followed by nothing or only spaces
        is_valid_fence = False
        if marker_count >= 3 and fence_type:
            after = stripped[marker_count:]
            # Valid if nothing after, or only spaces/tabs after (for closing)
            # Or any characters after (for opening with language)
            is_valid_fence = True

        if is_valid_fence and fence_type and marker_count >= 3:
            if not in_fence:
                # Opening a fence
                in_fence = True
                fence_marker = fence_type
                opening_fence_count = marker_count
                language = stripped[marker_count:].strip()
                result.append(Fence(language=language, line_no=line_no))
            elif fence_marker == fence_type and marker_count >= opening_fence_count:
                # Check if after the marker is only whitespace (valid closing)
                after = stripped[marker_count:]
                if not after or all(c in " \t" for c in after):
                    # Closing a fence
                    in_fence = False
                    fence_marker = None
                    opening_fence_count = 0

    return result


def _outside_fences(text: str) -> list[tuple[int, str]]:
    """(line_no, line) pairs for lines outside fenced code blocks."""
    out: list[tuple[int, str]] = []
    in_fence = False
    fence_char = ""
    for line_no, line in enumerate(text.replace("\r\n", "\n").split("\n")):
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            marker = stripped[:3]
            if not in_fence:
                in_fence, fence_char = True, marker
            elif marker == fence_char:
                in_fence = False
            continue
        if not in_fence:
            out.append((line_no, line))
    return out


def links(text: str) -> list[Link]:
    """Extract markdown links [text](target).

    Args:
        text: The markdown text.

    Returns:
        List of Link objects.
    """
    result = []

    for line_no, line in _outside_fences(text):
        # Simple regex-free link extraction: find [...](...) patterns
        i = 0
        while i < len(line):
            # Look for [
            bracket_start = line.find("[", i)
            if bracket_start == -1:
                break

            # Look for matching ]
            bracket_end = line.find("]", bracket_start + 1)
            if bracket_end == -1:
                break

            # Check for (
            paren_start = bracket_end + 1
            if paren_start >= len(line) or line[paren_start] != "(":
                i = bracket_end + 1
                continue

            # Find matching )
            paren_end = line.find(")", paren_start + 1)
            if paren_end == -1:
                break

            link_text = line[bracket_start + 1 : bracket_end]
            link_target = line[paren_start + 1 : paren_end]

            # Handle title syntax [text](url "title")
            if " " in link_target:
                link_target = link_target.split(" ")[0].strip('"')

            result.append(Link(text=link_text, target=link_target, line_no=line_no))
            i = paren_end + 1

    return result


def backticked_tokens(text: str) -> list[str]:
    """Extract backticked tokens from markdown.

    Args:
        text: The markdown text.

    Returns:
        List of tokens found between single backticks, deduplicated.
    """
    tokens = []

    for _line_no, line in _outside_fences(text):
        i = 0
        while i < len(line):
            start = line.find("`", i)
            if start == -1:
                break

            end = line.find("`", start + 1)
            if end == -1:
                break

            token = line[start + 1 : end]
            if token and token not in tokens:
                tokens.append(token)

            i = end + 1

    return tokens


def blockquote_under_h1(text: str) -> str | None:
    """The blockquote directly under the first H1 (blank lines allowed between), as
    plain text with the ``>`` markers removed and lines joined by spaces; None when
    the first H1 is not followed by a blockquote."""
    _fm, body = parse_frontmatter(text)
    lines = body.replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines) and not lines[i].startswith("# "):
        i += 1
    if i >= len(lines):
        return None
    i += 1
    while i < len(lines) and not lines[i].strip():
        i += 1
    quote: list[str] = []
    while i < len(lines) and lines[i].startswith(">"):
        quote.append(lines[i][1:].strip())
        i += 1
    if not quote:
        return None
    return " ".join(q for q in quote if q).strip() or None


def body_chars(text: str) -> int:
    """Count characters in the markdown body, excluding frontmatter.

    Args:
        text: The markdown text.

    Returns:
        Character count of the body only.
    """
    _, body = parse_frontmatter(text)
    return len(body)

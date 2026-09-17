"""README generation for documenter workflow (spec 171)."""
from __future__ import annotations

from performer.workflows.documenter.models import (
    KIND_TO_SECTION,
    README_SECTIONS,
    SUMMARY_MAX_CHARS,
    WikiPage,
)

__all__ = [
    "generate_readme",
    "readme_shape_ok",
]


def generate_readme(project_name: str, summary: str, pages: list[WikiPage]) -> str:
    """Generate README in llms.txt shape (FR-009).

    Structure:
    - H1 with project name
    - Blockquote with summary (<=300 chars)
    - H2 sections from README_SECTIONS
    - Each section lists pages under KIND_TO_SECTION[kind]
    - Pages without kind under Optional

    Args:
        project_name: The project name.
        summary: Summary text (will be truncated to 300 chars).
        pages: Inventory pages to link.

    Returns:
        The generated README markdown.
    """
    summary = " ".join(str(summary or "").split())
    # Truncate summary
    if len(summary) > SUMMARY_MAX_CHARS:
        summary = summary[:SUMMARY_MAX_CHARS]

    lines = []

    # H1 title
    lines.append(f"# {project_name}")
    lines.append("")

    # Blockquote summary
    for summary_line in summary.split("\n"):
        if summary_line:
            lines.append(f"> {summary_line}")
        else:
            lines.append(">")
    lines.append("")

    # Build section content by kind
    section_pages: dict[str, list[WikiPage]] = {section: [] for section in README_SECTIONS}
    section_pages["Optional"] = []

    for page in pages:
        if page.kind and page.kind in KIND_TO_SECTION:
            section = KIND_TO_SECTION[page.kind]
            section_pages[section].append(page)
        else:
            section_pages["Optional"].append(page)

    # Write each section
    for section in README_SECTIONS:
        lines.append(f"## {section}")

        section_list = section_pages.get(section, [])
        if section_list:
            for page in section_list:
                # Page name is relative path from project root
                relative_path = page.path
                relative_path = relative_path.removeprefix("docs/wiki/")  # Strip docs/wiki/
                # Get one-line description (first paragraph)
                one_line = (page.summary or "No summary yet.").rstrip(".") + "."
                # Could extract first line from page content, but we don't have it here
                lines.append(f"- [{page.title}]({relative_path}): {one_line}")
        else:
            lines.append("- Nothing here yet.")

        lines.append("")

    # Optional section
    return "\n".join(lines)


def readme_shape_ok(content: str, pages: list[WikiPage]) -> list[str]:
    """Validate README shape (FR-009 gate).

    Checks:
    - Exactly one H1
    - Blockquote directly under H1
    - Every page linked exactly once
    - Only README_SECTIONS as H2

    Args:
        content: The README content.
        pages: The expected pages.

    Returns:
        List of failure messages (empty if OK).
    """
    failures = []

    lines = content.split("\n")

    # Count H1 headings (exactly one # followed by space, not ## or ###)
    def is_h1(line: str) -> bool:
        return line.startswith("# ") and not line.startswith("## ")

    h1_lines = [i for i, line in enumerate(lines) if is_h1(line)]
    if len(h1_lines) != 1:
        failures.append(f"Expected exactly one H1, found {len(h1_lines)}")

    # Check blockquote under H1
    h1_line = None
    for i, line in enumerate(lines):
        if is_h1(line):
            h1_line = i
            break

    if h1_line is not None:
        # Look for blockquote in next few lines
        has_blockquote = False
        for i in range(h1_line + 1, min(h1_line + 3, len(lines))):
            if lines[i].startswith(">"):
                has_blockquote = True
                break
        if not has_blockquote:
            failures.append("No blockquote found directly under H1")

    # Check each page linked exactly once
    for page in pages:
        # Get just the filename
        title = page.title
        # Search for the link in content
        link_pattern = f"[{title}]"
        count = content.count(link_pattern)
        if count != 1:
            failures.append(f"Page '{title}' linked {count} times, expected 1")

    # Check only valid H2 sections
    valid_sections = set(README_SECTIONS) | {"Optional"}
    seen_sections: list[str] = []
    for line in lines:
        if line.startswith("## "):
            section_name = line[3:].strip()
            if section_name not in valid_sections:
                failures.append(f"Invalid H2 section: {section_name}")
            if section_name in seen_sections:
                failures.append(f"Duplicate H2 section: {section_name}")
            seen_sections.append(section_name)

    return failures

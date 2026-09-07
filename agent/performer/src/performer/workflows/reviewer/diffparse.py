"""Unified diff parser for reviewer workflow (spec 169).

Parses a unified diff into ChangedFile structures with hunks and line ranges.
Detects truncation marker appended by _sanitize_pr_diff in dispatch_performer.py.
"""
from __future__ import annotations

import re

from performer.workflows.reviewer.models import ChangedFile, Hunk

__all__ = ["parse_unified_diff", "detect_truncation", "diff_lines", "line_in_hunks"]

#: The exact truncation note format written by dispatch_performer._sanitize_pr_diff
TRUNCATION_MARKER_PATTERN = r"\[coordinare: .*?diff truncated to \d+ chars.*?\]"


def parse_unified_diff(text: str) -> list[ChangedFile]:
    """Parse unified diff into ChangedFile structures with hunks.

    Args:
        text: The unified diff text, possibly with a truncation marker appended.

    Returns:
        List of ChangedFile objects with path, hunks, fully_in_diff, opened_by_survey.
    """
    changed_files: dict[str, ChangedFile] = {}
    current_file: str | None = None
    current_hunks: list[Hunk] = []

    lines = text.split("\n")
    for line in lines:
        # Detect file header: "diff --git a/path b/path"
        if line.startswith("diff --git "):
            # Save the previous file if any
            if current_file is not None:
                changed_files[current_file] = ChangedFile(
                    path=current_file,
                    hunks=current_hunks,
                    fully_in_diff=True,
                    opened_by_survey=False,
                )
            current_hunks = []

            # Extract the target path from "diff --git a/x b/x"
            parts = line.split(" ")
            if len(parts) >= 4:
                # The b/ part is at parts[3], e.g., "b/src/foo.py"
                b_path = parts[3]
                if b_path.startswith("b/"):
                    current_file = b_path[2:]
                else:
                    current_file = b_path
            continue

        # Detect rename: "similarity index" or "rename from/to"
        if line.startswith("rename from ") or line.startswith("rename to "):
            continue

        # Detect new file: "new file mode"
        if line.startswith("new file mode"):
            continue

        # Detect deleted file: "deleted file mode"
        if line.startswith("deleted file mode"):
            continue

        # Detect hunk header: "@@ -a,b +c,d @@"
        hunk_match = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
        if hunk_match:
            start_line = int(hunk_match.group(1))
            count = int(hunk_match.group(2)) if hunk_match.group(2) else 1

            # Skip hunks with no new-side lines (e.g., deleted files with +0,0)
            if count == 0:
                continue

            end_line = start_line + count - 1

            # Collect hunk lines until the next hunk or file header
            hunk_lines: list[str] = []
            current_hunks.append(
                Hunk(
                    header=line,
                    start_line=start_line,
                    end_line=end_line,
                    lines=hunk_lines,
                )
            )
            continue

        # Collect lines in the current hunk (context or added/removed)
        if current_hunks and (line.startswith("+") or line.startswith("-") or line.startswith(" ")):
            if current_hunks:
                current_hunks[-1].lines.append(line)

    # Save the last file
    if current_file is not None:
        changed_files[current_file] = ChangedFile(
            path=current_file,
            hunks=current_hunks,
            fully_in_diff=True,
            opened_by_survey=False,
        )

    return list(changed_files.values())


def detect_truncation(text: str) -> bool:
    """Detect whether the diff was truncated by the sanitizer.

    Returns True if the text contains the truncation marker pattern.

    Args:
        text: The diff text, possibly with a truncation marker appended.

    Returns:
        True if truncation marker is present.
    """
    return bool(re.search(TRUNCATION_MARKER_PATTERN, text, re.DOTALL))


def diff_lines(text: str) -> list[str]:
    """Extract all added and context lines from the diff.

    Returns lines without the +/space prefix, for evidence matching.

    Args:
        text: The unified diff text.

    Returns:
        List of lines (added and context, with leading +/space removed).
    """
    result = []
    for line in text.split("\n"):
        if line.startswith("+") and not line.startswith("+++"):
            result.append(line[1:])
        elif line.startswith(" "):
            result.append(line[1:])
    return result


def line_in_hunks(file: ChangedFile, line: int) -> bool:
    """Check if a line number falls within any hunk's new-side range.

    Args:
        file: The ChangedFile object with hunks.
        line: The new-side line number to check.

    Returns:
        True if the line is in any hunk's range [start_line, end_line].
    """
    for hunk in file.hunks:
        if hunk.start_line <= line <= hunk.end_line:
            return True
    return False

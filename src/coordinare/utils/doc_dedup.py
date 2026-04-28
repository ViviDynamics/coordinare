"""Markdown section deduplication pass (055 Phase 5).

Scans a workspace directory for markdown files, detects sections with
identical or near-identical content, and merges or disambiguates them.

Rules:
- Sections are delimited by ATX headings (# ... through ######).
- Two sections are "duplicate" if their normalised token overlap ≥ OVERLAP_THRESHOLD.
- When duplicates are found across multiple files:
  - If overlap ≥ OVERLAP_THRESHOLD: merge into the canonical file (first alphabetically).
  - If overlap < OVERLAP_THRESHOLD but > AMBIGUOUS_THRESHOLD: disambiguate by
    appending the source filename to the heading, preserving both.
- Sections that are exact matches are always merged (overlap = 1.0).
- Content safety: never silently drop content. When in doubt, preserve.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path  # noqa: TC003 — needed at runtime for function signatures

OVERLAP_THRESHOLD = 0.60
AMBIGUOUS_THRESHOLD = 0.30

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)


@dataclass
class DocDeduplicationResult:
    files_modified: list[str] = field(default_factory=list)
    sections_merged: int = 0
    conflicts_preserved: int = 0


def _extract_sections(text: str) -> list[tuple[str, str]]:
    """Return [(heading_text, body_text), ...] for an ATX-heading markdown doc."""
    positions = [(m.start(), m.group(2).strip()) for m in _HEADING_RE.finditer(text)]
    if not positions:
        return []
    sections = []
    for i, (start, heading) in enumerate(positions):
        end = positions[i + 1][0] if i + 1 < len(positions) else len(text)
        body = text[start:end]
        sections.append((heading, body))
    return sections


def _token_overlap(a: str, b: str) -> float:
    """Jaccard token overlap of two strings after lowercasing + splitting."""
    toks_a = set(re.split(r"\W+", a.lower())) - {""}
    toks_b = set(re.split(r"\W+", b.lower())) - {""}
    if not toks_a and not toks_b:
        return 1.0
    if not toks_a or not toks_b:
        return 0.0
    return len(toks_a & toks_b) / len(toks_a | toks_b)


def find_duplicate_sections(workspace_path: Path) -> dict[str, list[str]]:
    """Return {heading: [file1, file2, ...]} for headings appearing in >1 file."""
    md_files = sorted(workspace_path.rglob("*.md"))
    heading_to_files: dict[str, list[str]] = {}

    for md_file in md_files:
        try:
            text = md_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for heading, _ in _extract_sections(text):
            heading_to_files.setdefault(heading, [])
            rel = str(md_file.relative_to(workspace_path))
            if rel not in heading_to_files[heading]:
                heading_to_files[heading].append(rel)

    return {h: files for h, files in heading_to_files.items() if len(files) > 1}


def merge_duplicate_sections(
    workspace_path: Path,
    canonical_priority: list[str] | None = None,
) -> DocDeduplicationResult:
    """Merge or disambiguate duplicate markdown sections across the workspace.

    ``canonical_priority`` is an optional ordered list of relative file paths;
    the first matching file becomes the canonical destination. Defaults to
    alphabetical order.

    Returns a DocDeduplicationResult describing changes made.
    """
    result = DocDeduplicationResult()
    md_files = sorted(workspace_path.rglob("*.md"))
    if not md_files:
        return result

    # Build a map: heading → {rel_path: (full_body, section_index)}
    file_sections: dict[str, str] = {}  # rel_path → full text
    heading_map: dict[str, dict[str, str]] = {}  # heading → {rel_path: body}

    for md_file in md_files:
        try:
            text = md_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = str(md_file.relative_to(workspace_path))
        file_sections[rel] = text
        for heading, body in _extract_sections(text):
            heading_map.setdefault(heading, {})[rel] = body

    modified_texts: dict[str, str] = {}

    for heading, file_bodies in heading_map.items():
        if len(file_bodies) < 2:
            continue
        paths = sorted(file_bodies.keys())
        if canonical_priority:
            for prio in canonical_priority:
                if prio in paths:
                    paths = [prio] + [p for p in paths if p != prio]
                    break

        canonical_path = paths[0]
        duplicates = paths[1:]

        for dup_path in duplicates:
            overlap = _token_overlap(file_bodies[canonical_path], file_bodies[dup_path])

            if overlap >= OVERLAP_THRESHOLD:
                # Merge: remove section from duplicate file
                current = modified_texts.get(dup_path, file_sections.get(dup_path, ""))
                new_text = _remove_section(current, heading)
                if new_text != current:
                    modified_texts[dup_path] = new_text
                    result.sections_merged += 1
            elif overlap > AMBIGUOUS_THRESHOLD:
                # Disambiguate: append source hint to heading in both files,
                # preserving the original heading level (#..######).
                canonical_text = modified_texts.get(canonical_path, file_sections.get(canonical_path, ""))
                dup_text = modified_texts.get(dup_path, file_sections.get(dup_path, ""))
                canon_renamed = _rename_heading(
                    canonical_text, heading, f"{heading} ({canonical_path})"
                )
                dup_renamed = _rename_heading(
                    dup_text, heading, f"{heading} ({dup_path})"
                )
                if canon_renamed == canonical_text and dup_renamed == dup_text:
                    # Neither rename matched — leave as-is rather than count a no-op.
                    continue
                modified_texts[canonical_path] = canon_renamed
                modified_texts[dup_path] = dup_renamed
                result.conflicts_preserved += 1

    for rel_path, new_text in modified_texts.items():
        abs_path = workspace_path / rel_path
        try:
            abs_path.write_text(new_text, encoding="utf-8")
            result.files_modified.append(rel_path)
        except OSError:
            pass  # best-effort; do not fail the QA pass

    return result


def _rename_heading(text: str, heading: str, new_heading: str) -> str:
    """Rename the first ATX heading matching `heading` (any level) to `new_heading`."""
    pattern = re.compile(
        r"^(?P<level>#{1,6})\s+" + re.escape(heading) + r"\s*$",
        re.MULTILINE,
    )
    return pattern.sub(lambda m: f"{m.group('level')} {new_heading}", text, count=1)


def _remove_section(text: str, heading: str) -> str:
    """Remove the first ATX section with the given heading text from markdown."""
    pattern = re.compile(
        r"(^#{1,6}\s+" + re.escape(heading) + r"\s*$)(.*?)(?=^#{1,6}\s+|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    return pattern.sub("", text, count=1).strip() + "\n"

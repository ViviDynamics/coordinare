from __future__ import annotations

import re

_CHECKLIST_RE = re.compile(r"^\s*-\s*\[[ xX]\]\s*(.+)$", re.MULTILINE)
_HEADING_RE = re.compile(
    r"^##\s*Acceptance\s+Criteria\s*$", re.MULTILINE | re.IGNORECASE,
)


def parse_acceptance_criteria(body: str) -> list[str]:
    """Extract acceptance criteria from a markdown issue body.

    Supports two patterns:
    1. Checklist items anywhere: ``- [ ] criterion`` or ``- [x] criterion``
    2. Plain list items under an ``## Acceptance Criteria`` heading
       (up to the next heading)

    Returns deduplicated criteria in order of first appearance.
    """
    if not body:
        return []

    criteria: list[str] = []
    seen: set[str] = set()

    for match in _CHECKLIST_RE.finditer(body):
        text = match.group(1).strip()
        if text and text not in seen:
            criteria.append(text)
            seen.add(text)

    heading_match = _HEADING_RE.search(body)
    if heading_match:
        section_start = heading_match.end()
        next_heading = re.search(r"^#{1,3}\s", body[section_start:], re.MULTILINE)
        section_end = (
            section_start + next_heading.start() if next_heading else len(body)
        )
        section = body[section_start:section_end]

        for line in section.split("\n"):
            stripped = line.strip()
            if stripped.startswith(("- ", "* ")) and not re.match(
                r"^\s*-\s*\[[ xX]\]", line,
            ):
                text = stripped[2:].strip()
                if text and text not in seen:
                    criteria.append(text)
                    seen.add(text)

    return criteria

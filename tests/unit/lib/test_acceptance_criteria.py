from __future__ import annotations

from coordinare.lib.acceptance_criteria import parse_acceptance_criteria


def test_parse_checklist_items() -> None:
    body = "## Overview\nSome text\n- [ ] First criterion\n- [x] Second criterion\n- [ ] Third"
    result = parse_acceptance_criteria(body)
    assert result == ["First criterion", "Second criterion", "Third"]


def test_parse_heading_with_plain_list() -> None:
    body = "## Acceptance Criteria\n- Must handle errors\n- Must log output\n## Notes\nSomething"
    result = parse_acceptance_criteria(body)
    assert result == ["Must handle errors", "Must log output"]


def test_parse_heading_with_checklist() -> None:
    body = "## Acceptance Criteria\n- [ ] AC one\n- [x] AC two"
    result = parse_acceptance_criteria(body)
    assert result == ["AC one", "AC two"]


def test_parse_empty_body() -> None:
    assert parse_acceptance_criteria("") == []


def test_parse_no_criteria() -> None:
    assert parse_acceptance_criteria("Just a description with no lists.") == []


def test_deduplication() -> None:
    body = "- [ ] Same item\n## Acceptance Criteria\n- Same item"
    result = parse_acceptance_criteria(body)
    assert result == ["Same item"]

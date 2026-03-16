"""Tests for lib/acceptance_criteria.py — parse_acceptance_criteria."""
from __future__ import annotations

from coordinare.lib.acceptance_criteria import parse_acceptance_criteria

# ---------------------------------------------------------------------------
# Empty / falsy body
# ---------------------------------------------------------------------------


def test_empty_string_returns_empty_list() -> None:
    assert parse_acceptance_criteria("") == []


def test_none_equivalent_falsy_body() -> None:
    # The function checks `if not body`, so any falsy value works.
    # In practice callers pass str, but an empty string is the canonical case.
    assert parse_acceptance_criteria("") == []


# ---------------------------------------------------------------------------
# Checklist items extracted correctly
# ---------------------------------------------------------------------------


def test_unchecked_checklist_item_extracted() -> None:
    body = "- [ ] User can log in"
    result = parse_acceptance_criteria(body)
    assert result == ["User can log in"]


def test_checked_checklist_item_extracted() -> None:
    body = "- [x] Feature is complete"
    result = parse_acceptance_criteria(body)
    assert result == ["Feature is complete"]


def test_uppercase_x_checked_item_extracted() -> None:
    body = "- [X] Tests pass"
    result = parse_acceptance_criteria(body)
    assert result == ["Tests pass"]


def test_multiple_checklist_items_in_order() -> None:
    body = (
        "- [ ] First criterion\n"
        "- [x] Second criterion\n"
        "- [ ] Third criterion\n"
    )
    result = parse_acceptance_criteria(body)
    assert result == ["First criterion", "Second criterion", "Third criterion"]


# ---------------------------------------------------------------------------
# Deduplication — line 29: `if text and text not in seen`
# ---------------------------------------------------------------------------


def test_duplicate_checklist_items_appear_only_once() -> None:
    body = (
        "- [ ] Do the thing\n"
        "- [ ] Do the thing\n"
        "- [x] Do the thing\n"
    )
    result = parse_acceptance_criteria(body)
    assert result == ["Do the thing"]


def test_duplicate_items_preserve_first_occurrence_order() -> None:
    body = (
        "- [ ] Alpha\n"
        "- [ ] Beta\n"
        "- [ ] Alpha\n"
    )
    result = parse_acceptance_criteria(body)
    assert result == ["Alpha", "Beta"]


# ---------------------------------------------------------------------------
# Items under ## Acceptance Criteria heading
# ---------------------------------------------------------------------------


def test_plain_list_items_under_heading_extracted() -> None:
    body = (
        "## Acceptance Criteria\n"
        "- First item\n"
        "- Second item\n"
    )
    result = parse_acceptance_criteria(body)
    assert result == ["First item", "Second item"]


def test_asterisk_list_items_under_heading_extracted() -> None:
    body = (
        "## Acceptance Criteria\n"
        "* Item A\n"
        "* Item B\n"
    )
    result = parse_acceptance_criteria(body)
    assert result == ["Item A", "Item B"]


def test_heading_extraction_stops_at_next_heading() -> None:
    body = (
        "## Acceptance Criteria\n"
        "- Criterion one\n"
        "## Other Section\n"
        "- Should not appear\n"
    )
    result = parse_acceptance_criteria(body)
    assert "Criterion one" in result
    assert "Should not appear" not in result


def test_heading_case_insensitive() -> None:
    body = (
        "## acceptance criteria\n"
        "- Lower case heading item\n"
    )
    result = parse_acceptance_criteria(body)
    assert result == ["Lower case heading item"]


# ---------------------------------------------------------------------------
# Items under heading that are already in checklist not duplicated
# ---------------------------------------------------------------------------


def test_heading_items_already_in_checklist_not_duplicated() -> None:
    """A plain list item under ## Acceptance Criteria that matches a checklist
    item earlier in the body must not appear twice."""
    body = (
        "- [ ] Shared criterion\n"
        "\n"
        "## Acceptance Criteria\n"
        "- Shared criterion\n"
        "- Unique heading item\n"
    )
    result = parse_acceptance_criteria(body)
    assert result.count("Shared criterion") == 1
    assert "Unique heading item" in result


def test_checklist_items_inside_heading_section_not_double_counted() -> None:
    """Checklist-style items inside the heading section are processed by
    _CHECKLIST_RE first; the heading extractor skips them (they match the
    checklist regex guard). The final list must not contain duplicates."""
    body = (
        "## Acceptance Criteria\n"
        "- [ ] Checkbox inside section\n"
        "- Plain item\n"
    )
    result = parse_acceptance_criteria(body)
    assert result.count("Checkbox inside section") == 1
    assert "Plain item" in result


def test_combined_checklist_and_heading_items() -> None:
    body = (
        "Some intro text.\n"
        "\n"
        "- [x] Done already\n"
        "- [ ] Still to do\n"
        "\n"
        "## Acceptance Criteria\n"
        "- Extra criterion\n"
        "- Done already\n"
    )
    result = parse_acceptance_criteria(body)
    assert result == ["Done already", "Still to do", "Extra criterion"]

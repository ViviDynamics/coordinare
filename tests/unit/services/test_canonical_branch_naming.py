"""Spec 076 T105 — canonical_branch_name edge-case unit tests.

Complements ``tests/contract/test_canonical_branch_contract.py`` (which
exercises the 8 mandatory test vectors).  Here we cover the
construction wrapper's error paths.
"""
from __future__ import annotations

import pytest

from coordinare.services.dispatch_guard import (
    canonical_branch_name,
    compute_title_slug,
)


def test_exactly_60_char_slug_passes_through() -> None:
    title = "a" * 60
    slug = compute_title_slug(title)
    assert len(slug) == 60


def test_61_char_slug_truncates_to_60() -> None:
    title = "a" * 61
    slug = compute_title_slug(title)
    assert len(slug) == 60


def test_slug_with_no_alphanumerics_is_empty_string() -> None:
    assert compute_title_slug("!!!@@@$$$") == ""


def test_slug_with_only_whitespace_is_empty() -> None:
    assert compute_title_slug("   \t\n   ") == ""


def test_canonical_branch_name_refuses_empty_id() -> None:
    with pytest.raises(ValueError, match=r"card\.id"):
        canonical_branch_name({"id": "", "title": "valid title"})


def test_canonical_branch_name_refuses_non_string_id() -> None:
    with pytest.raises(ValueError, match=r"card\.id"):
        canonical_branch_name({"id": 12345, "title": "valid"})


def test_canonical_branch_name_refuses_non_string_title() -> None:
    with pytest.raises(ValueError, match=r"card\.title"):
        canonical_branch_name({"id": "PVTI_X", "title": 12345})


def test_canonical_branch_name_refuses_slug_emptying_title() -> None:
    """A title that slugifies to empty MUST raise so the caller cannot
    launch a performer on a malformed branch like
    ``coordinare/PVTI_X/`` (trailing slash, no slug)."""
    with pytest.raises(ValueError, match="empty slug"):
        canonical_branch_name({"id": "PVTI_X", "title": "!!!"})


def test_full_name_format() -> None:
    branch = canonical_branch_name({
        "id": "PVTI_lADO_ABCDEF",
        "title": "Some feature",
    })
    assert branch.full_name == "coordinare/PVTI_lADO_ABCDEF/some-feature"

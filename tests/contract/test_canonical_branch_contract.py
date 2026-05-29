"""Canonical branch name contract test (spec 076 T031).

Exhaustive matrix of the 8 mandatory test vectors from
``specs/076-qa-cycle/contracts/canonical-branch.md`` plus the algorithm-
version pin.  Any change to ``compute_title_slug`` MUST update these
vectors AND bump ``SLUG_ALGORITHM_VERSION`` so reviewers cannot
accidentally drift the canonical naming.
"""
from __future__ import annotations

import pytest

from coordinare.services.dispatch_guard import (
    SLUG_ALGORITHM_VERSION,
    canonical_branch_name,
    compute_title_slug,
)
from coordinare.services.dispatcher_dedup_models import CanonicalBranchName


def test_slug_algorithm_version_pinned_to_1() -> None:
    """If you change ``compute_title_slug``, bump this constant and the
    vectors below.  Reviewers MUST see both edits in the same PR."""
    assert SLUG_ALGORITHM_VERSION == 1


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        # Vector 1: canonical real-world card title (today's incident, PR #148)
        (
            "Feature: Time tracking schema and model foundation",
            "feature-time-tracking-schema-and-model-foundation",
        ),
        # Vector 2: special characters collapse to single hyphen
        (
            "Fix bug #123 in OAuth/SSO flow",
            "fix-bug-123-in-oauth-sso-flow",
        ),
        # Vector 3: whitespace runs collapse
        (
            "  Already   Has   Whitespace  ",
            "already-has-whitespace",
        ),
        # Vector 4: leading/trailing dashes stripped
        (
            "---leading and trailing dashes---",
            "leading-and-trailing-dashes",
        ),
        # Vector 5: all caps lowercased
        (
            "ALLCAPS",
            "allcaps",
        ),
        # Vector 6: non-ASCII titles produce empty slug (caller MUST refuse)
        (
            "超長標題不是ASCII",
            "ascii",  # only the ASCII subword survives the regex
        ),
        # Vector 7: long run of same char with no separators truncates at length cap
        (
            "a" * 200,
            "a" * 60,
        ),
        # Vector 8: long sentence truncates cleanly at a `-` boundary at/before 60 chars
        (
            "this-is-a-very-long-title-that-will-need-to-be-truncated-at-the-sixty-character-boundary-and-should-do-so-cleanly",
            "this-is-a-very-long-title-that-will-need-to-be-truncated-at",
        ),
    ],
)
def test_compute_title_slug_test_vectors(title: str, expected: str) -> None:
    assert compute_title_slug(title) == expected


def test_slug_truncation_never_exceeds_60_chars() -> None:
    """Length cap is hard — no 61-char slug should exist."""
    for length in (1, 60, 61, 100, 1000):
        title = "x" * length
        slug = compute_title_slug(title)
        assert len(slug) <= 60, f"slug for {length}x'x' exceeded cap: {len(slug)}"


def test_slug_is_deterministic() -> None:
    """Same input → byte-identical output across many invocations."""
    title = "Feature: Time tracking schema and model foundation"
    first = compute_title_slug(title)
    for _ in range(50):
        assert compute_title_slug(title) == first


def test_canonical_branch_name_builds_full_name() -> None:
    card = {
        "id": "PVTI_lADOBjmjsc4ApgHnzgq-8Gc",
        "title": "Feature: Time tracking schema and model foundation",
    }
    branch = canonical_branch_name(card)
    assert isinstance(branch, CanonicalBranchName)
    assert branch.card_node_id == "PVTI_lADOBjmjsc4ApgHnzgq-8Gc"
    assert branch.title_slug == "feature-time-tracking-schema-and-model-foundation"
    assert (
        branch.full_name
        == "coordinare/PVTI_lADOBjmjsc4ApgHnzgq-8Gc/feature-time-tracking-schema-and-model-foundation"
    )


def test_canonical_branch_name_refuses_missing_id() -> None:
    with pytest.raises(ValueError, match=r"card\.id"):
        canonical_branch_name({"title": "Whatever"})


def test_canonical_branch_name_refuses_missing_title() -> None:
    with pytest.raises(ValueError, match=r"card\.title"):
        canonical_branch_name({"id": "PVTI_X"})


def test_canonical_branch_name_refuses_empty_slug() -> None:
    """A title that slugifies to nothing (e.g. all symbols) MUST raise so
    the dispatcher refuses to launch a performer with a malformed branch."""
    with pytest.raises(ValueError, match="empty slug"):
        canonical_branch_name({"id": "PVTI_X", "title": "!!!@@@###"})

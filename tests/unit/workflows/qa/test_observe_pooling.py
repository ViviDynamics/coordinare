"""FR-011/FR-015 — the before/after DOM comparison.

The model-description pooling this file once tested was superseded by direct
DOM reads and removed as dead code (round-two review). The lesson survives in
_diff_key: pool on structure, never on model-supplied labels.
"""
from __future__ import annotations

from performer.workflows.models import Observation
from performer.workflows.qa.observe import diff_observations


def test_removed_element_is_the_regression_signal():
    before = [Observation(kind="password_input", position=1), Observation(kind="button", position=2)]
    after = [Observation(kind="button", position=2)]
    added, removed = diff_observations(before, after)
    assert not added
    assert [o.kind for o in removed] == ["password_input"]

def test_added_element_is_the_feature_signal():
    before = [Observation(kind="button", position=0)]
    after = [Observation(kind="button", position=0), Observation(kind="dropdown", position=1)]
    added, removed = diff_observations(before, after)
    assert [o.kind for o in added] == ["dropdown"]
    assert not removed

def test_a_replaced_field_at_the_same_position_is_a_regression():
    """Found by the first full six-scenario eval run -- as a FALSE PASS.

    A form's password field was replaced by a workspace field at the same
    position. Both render as `text_input`, so a (kind, position) key made them
    identical and the delta was empty: the workflow reported a clean pass on a
    change that deleted the password field.

    FR-011 excludes labels from POOLING because the MODEL cannot supply them
    reliably -- three description passes named one dropdown three ways. But
    diffing compares two DOM reads, where labels are exact. Applying the pooling
    rule to the diff threw away the only distinguishing information.
    """
    before = [
        Observation(kind="text_input", position=1, label="email"),
        Observation(kind="text_input", position=2, label="password"),
    ]
    after = [
        Observation(kind="text_input", position=1, label="email"),
        Observation(kind="text_input", position=2, label="workspace"),
    ]

    added, removed = diff_observations(before, after)

    assert [o.label for o in removed] == ["password"], "the lost field must be named"
    assert [o.label for o in added] == ["workspace"]

def test_an_unchanged_page_still_produces_no_delta():
    """The label-aware key must not manufacture churn on an identical page."""
    page = [
        Observation(kind="heading", position=0, label="Sign in"),
        Observation(kind="text_input", position=1, label="email"),
    ]
    added, removed = diff_observations(list(page), list(page))
    assert not added and not removed

def test_diffing_ignores_label_whitespace_and_case():
    """DOM text varies harmlessly between renders; that is not a regression."""
    before = [Observation(kind="button", position=0, label="Continue")]
    after = [Observation(kind="button", position=0, label="  continue ")]
    added, removed = diff_observations(before, after)
    assert not added and not removed

def test_an_element_with_no_label_still_diffs_on_kind_and_position():
    before = [Observation(kind="banner", position=0, label=None)]
    after: list[Observation] = []
    _added, removed = diff_observations(before, after)
    assert removed and removed[0].kind == "banner"

def test_inserting_an_element_does_not_regress_the_ones_below_it():
    """Found by the six-scenario eval.

    Adding a field shifts every element after it down one position. A key that
    includes position reports the untouched submit button as both removed and
    added -- a spurious regression on a perfectly good change, which is the
    false-failure half of the problem.

    A DOM label is a stable identity across insertion; position is not. So the
    label identifies an element when it has one, and position only breaks ties
    between unlabelled elements of the same kind.
    """
    before = [
        Observation(kind="text_input", position=1, label="email"),
        Observation(kind="button", position=2, label="Continue"),
    ]
    after = [
        Observation(kind="text_input", position=1, label="email"),
        Observation(kind="dropdown", position=2, label="workspace"),
        Observation(kind="button", position=3, label="Continue"),
    ]

    added, removed = diff_observations(before, after)

    assert not removed, "nothing was lost; the button merely moved down"
    assert [o.label for o in added] == ["workspace"]

def test_a_replaced_field_is_still_caught_when_positions_shift():
    """The two rules must hold together: insertion is not a regression, but a
    genuinely lost field still is."""
    before = [
        Observation(kind="text_input", position=1, label="password"),
        Observation(kind="button", position=2, label="Continue"),
    ]
    after = [
        Observation(kind="text_input", position=1, label="workspace"),
        Observation(kind="text_input", position=2, label="email"),
        Observation(kind="button", position=3, label="Continue"),
    ]

    _added, removed = diff_observations(before, after)
    assert [o.label for o in removed] == ["password"]

def test_unlabelled_elements_of_one_kind_are_still_told_apart_by_position():
    before = [
        Observation(kind="banner", position=0, label=None),
        Observation(kind="banner", position=1, label=None),
    ]
    after = [Observation(kind="banner", position=0, label=None)]

    _added, removed = diff_observations(before, after)
    assert len(removed) == 1

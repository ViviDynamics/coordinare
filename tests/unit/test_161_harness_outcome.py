"""Spec 161 — four-way classification of a dispatch's terminal marker.

T004 (US1): the marker mapping, one case per outcome class (FR-001..FR-004).
T005 (US1): classification reads terminal_marker, NOT the collapsed status (FR-002).
T006 (US1): the credit set is the IMPORTED constant, not a copied literal (FR-002, R4).
T007 (US1): absent and unrecognized markers are inconclusive and surfaced (FR-005, FR-006).
"""

from __future__ import annotations

import pytest

from coordinare.bench.artifact import PersonaDispatch
from coordinare.bench.harness_outcome import (
    ENVIRONMENT_MARKERS,
    HARNESS_DEFECT_MARKERS,
    NEGATIVE_VERDICT_MARKERS,
    OutcomeClass,
    classify,
    credit_markers,
)
from coordinare.graph.nodes.monitor_performer import TERMINAL_SUCCESS_STATES


def _dispatch(marker: str | None, status: str = "succeeded") -> PersonaDispatch:
    """A dispatch carrying an explicit marker/status pair.

    `status` defaults to something *inconsistent* with most markers on purpose: the
    classifier must ignore it entirely.
    """
    return PersonaDispatch(
        stage="implementing",
        role="implementer",
        backend="codex",
        status=status,  # type: ignore[arg-type]
        terminal_marker=marker,
    )


# --- T004: one case per outcome class -------------------------------------------------


@pytest.mark.parametrize("marker", sorted(TERMINAL_SUCCESS_STATES))
def test_success_markers_are_credited(marker: str) -> None:
    """FR-001/FR-002: every terminal success state earns credit."""
    assert classify(_dispatch(marker)) is OutcomeClass.CREDIT


@pytest.mark.parametrize(
    "marker", ["changes_requested", "qa_failed", "security_failed", "blocked"],
)
def test_negative_verdicts_are_credited_not_defects(marker: str) -> None:
    """FR-002: a role rendering a legitimate negative verdict did its job.

    This is the whole point of the feature. A reviewer that correctly rejects a bad PR
    emits `changes_requested`; penalizing it would rank a rubber-stamping harness above
    a discerning one.
    """
    assert classify(_dispatch(marker)) is OutcomeClass.CREDIT


@pytest.mark.parametrize("marker", ["malformed_output", "system_error"])
def test_harness_defect_markers(marker: str) -> None:
    """FR-003: the harness could not produce usable output."""
    assert classify(_dispatch(marker)) is OutcomeClass.HARNESS_DEFECT


def test_env_blocked_is_environment() -> None:
    """FR-004: neither the harness nor the model is at fault."""
    assert classify(_dispatch("env_blocked")) is OutcomeClass.ENVIRONMENT


def test_every_class_is_reachable() -> None:
    """FR-001: the mapping is total and covers all four classes."""
    observed = {
        classify(_dispatch("pr_opened")),
        classify(_dispatch("malformed_output")),
        classify(_dispatch("env_blocked")),
        classify(_dispatch(None)),
    }
    assert observed == set(OutcomeClass)


# --- T005: the marker, never the collapsed status -------------------------------------


def test_classification_ignores_status_and_reads_the_marker() -> None:
    """FR-002: `runner._dispatch_status` collapses every non-success marker to "failed",
    merging legitimate negative verdicts with real defects. Reading `status` would
    therefore penalize a reviewer for being right, so classification must not consult it.
    """
    dispatch = _dispatch("changes_requested", status="failed")

    assert dispatch.status == "failed"  # the collapsed value really is misleading
    assert classify(dispatch) is OutcomeClass.CREDIT


def test_status_succeeded_does_not_rescue_a_defect_marker() -> None:
    """The converse: a success-looking status must not launder a defect marker."""
    assert classify(_dispatch("malformed_output", status="succeeded")) is (
        OutcomeClass.HARNESS_DEFECT
    )


# --- T006: the credit set is imported, not copied -------------------------------------


def test_credit_set_is_the_imported_constant_plus_negative_verdicts() -> None:
    """R4: a copied literal would drift the moment a success marker is added upstream.

    Asserting against the imported constant means this test fails if the module ever
    hardcodes its own copy and upstream gains a member.
    """
    assert credit_markers() == set(TERMINAL_SUCCESS_STATES) | NEGATIVE_VERDICT_MARKERS


def test_credit_set_contains_every_upstream_success_state() -> None:
    """A subset check that fails loudly if a success state stops being credited."""
    assert set(TERMINAL_SUCCESS_STATES) <= credit_markers()


def test_marker_classes_are_disjoint() -> None:
    """No marker may fall into two classes, or the mapping would be ambiguous."""
    assert not (credit_markers() & HARNESS_DEFECT_MARKERS)
    assert not (credit_markers() & ENVIRONMENT_MARKERS)
    assert not (HARNESS_DEFECT_MARKERS & ENVIRONMENT_MARKERS)


# --- T007: absent and unknown markers -------------------------------------------------


@pytest.mark.parametrize("marker", [None, ""])
def test_absent_marker_is_inconclusive(marker: str | None) -> None:
    """FR-005: never reached terminal (budget or teardown cut it off)."""
    assert classify(_dispatch(marker)) is OutcomeClass.INCONCLUSIVE


def test_unknown_marker_is_inconclusive_not_credited() -> None:
    """FR-006: an unrecognized marker must never be silently credited or penalized."""
    assert classify(_dispatch("some_future_marker")) is OutcomeClass.INCONCLUSIVE


def test_unknown_marker_is_reported_as_unknown() -> None:
    """FR-006: the value must be surfaceable so a new marker is noticed, not absorbed."""
    from coordinare.bench.harness_outcome import is_unknown_marker

    assert is_unknown_marker("some_future_marker") is True
    assert is_unknown_marker("pr_opened") is False
    # An absent marker is inconclusive but is NOT an unknown vocabulary item.
    assert is_unknown_marker(None) is False
    assert is_unknown_marker("") is False


def test_classification_never_raises() -> None:
    """FR-001: the mapping is total, so no input escapes it."""
    for marker in [None, "", "pr_opened", "malformed_output", "env_blocked", "\x00weird"]:
        assert isinstance(classify(_dispatch(marker)), OutcomeClass)

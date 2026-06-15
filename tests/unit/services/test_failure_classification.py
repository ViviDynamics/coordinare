"""090 US2 (L2) — tests for failure-origin classification.

Background
----------
Layer 2 of spec 090 (Baseline Repair Autonomy) classifies each failing HEAD
check against a merge-base baseline so the coordinare can later (L3) repair
*inherited* failures without ever masking an *introduced* one.  The classifier
is pure (no I/O, no clock, no RNG) and implements a strict **source-order**
decision table (data-model §5, first match wins):

  1. HEAD conclusion is transient                       → FLAKE      (FR-010)
  2. baseline indeterminate (no base rollup) OR a
     16-char signature collision is detected            → UNKNOWN    (FR-012)
  3. same-name baseline failure but its conclusion is
     transient (a flaky baseline)                       → INTRODUCED (FR-011)
  4. baseline failure stable AND signature matches      → INHERITED  (FR-008)
  5. otherwise (no same-name baseline failure, or a
     stable baseline with a *different* signature)      → INTRODUCED (FR-009)

The Stable/Transient boundary is the keystone of anti-masking:

  - **Stable** (INHERITED-eligible): exactly ``{"failure"}``.
  - **Transient** (never INHERITED → FLAKE/INTRODUCED): the seven conclusions
    ``timed_out, cancelled, neutral, skipped, action_required, stale,
    startup_failure``.

``baseline_index`` is ``dict | None``: ``None`` means the base rollup was
indeterminate/unfetchable (row 2 → every failure UNKNOWN); an **empty dict**
means the base WAS fetched and had zero failures (row 5 → INTRODUCED).  The two
are deliberately distinct — conflating them would either mask introduced
failures or refuse to ever inherit.
"""
from __future__ import annotations

import pytest

from coordinare.services.ci_gate import FailedCheckWithSignature
from coordinare.services.failure_classification import (
    BaselineFailure,
    classify_failure_origin,
)
from coordinare.services.failure_signature import make_failure_signature

_SIG_A = "0123456789abcdef"
_SIG_B = "fedcba9876543210"

# The full transient set (data-model §5) — wider than FR-010's named four.
_TRANSIENT = (
    "timed_out",
    "cancelled",
    "neutral",
    "skipped",
    "action_required",
    "stale",
    "startup_failure",
)


def _head(
    *,
    name: str = "ci/test",
    conclusion: str = "failure",
    head_signature: str = _SIG_A,
    baseline_signature: str | None = None,
) -> FailedCheckWithSignature:
    return FailedCheckWithSignature(
        name=name,
        conclusion=conclusion,
        head_signature=head_signature,
        baseline_signature=baseline_signature,
    )


def _baseline(
    *,
    name: str = "ci/test",
    conclusion: str = "failure",
    signature: str = _SIG_A,
    normalized_reason: str = "boom",
) -> BaselineFailure:
    return BaselineFailure(
        name=name,
        conclusion=conclusion,
        signature=signature,
        normalized_reason=normalized_reason,
    )


# ---------------------------------------------------------------------------
# Row 1 — transient HEAD conclusion → FLAKE (and it beats every later row).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("conclusion", _TRANSIENT)
def test_transient_head_conclusion_is_flake(conclusion: str) -> None:
    # Row 1 wins even when an otherwise-perfect inherited match exists.
    head = _head(conclusion=conclusion, head_signature=_SIG_A)
    index = {"ci/test": _baseline(signature=_SIG_A, normalized_reason="boom")}
    assert classify_failure_origin(head, "boom", index) == "flake"


def test_transient_head_beats_indeterminate_baseline() -> None:
    # Source order: row 1 (FLAKE) is evaluated before row 2 (UNKNOWN).
    head = _head(conclusion="timed_out")
    assert classify_failure_origin(head, "boom", None) == "flake"


def test_failure_is_the_only_stable_conclusion() -> None:
    # "failure" must NOT be treated as transient — with a matching stable
    # baseline it inherits rather than flaking.
    head = _head(conclusion="failure", head_signature=_SIG_A)
    index = {"ci/test": _baseline(signature=_SIG_A, normalized_reason="boom")}
    assert classify_failure_origin(head, "boom", index) == "inherited"


def test_unrecognized_conclusion_is_not_transient() -> None:
    # A conclusion outside the known transient set is not a flake; with no
    # matching baseline it is INTRODUCED (the transient set is exactly the 7).
    head = _head(conclusion="weird_new_state")
    assert classify_failure_origin(head, "boom", {}) == "introduced"


# ---------------------------------------------------------------------------
# Row 2 — indeterminate baseline or signature collision → UNKNOWN.
# ---------------------------------------------------------------------------


def test_indeterminate_baseline_is_unknown() -> None:
    # Row 2a: base rollup unfetchable (None) → UNKNOWN, never INHERITED (FR-012).
    head = _head(conclusion="failure")
    assert classify_failure_origin(head, "boom", None) == "unknown"


def test_signature_collision_is_unknown() -> None:
    # Row 2b: equal 16-char signature but a *different* normalized reason is a
    # truncation collision — it must route to UNKNOWN, never masquerade as
    # INHERITED (data-model §1 collision handling).
    head = _head(conclusion="failure", head_signature=_SIG_A)
    index = {
        "ci/test": _baseline(signature=_SIG_A, normalized_reason="different reason")
    }
    assert classify_failure_origin(head, "boom", index) == "unknown"


# ---------------------------------------------------------------------------
# Row 3 — same-name baseline failure with a transient conclusion → INTRODUCED.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("baseline_conclusion", _TRANSIENT)
def test_flaky_baseline_makes_head_introduced(baseline_conclusion: str) -> None:
    # A flaky (transient) baseline failure does not let a stable HEAD failure
    # inherit — it is INTRODUCED (FR-011).  Distinct signatures because the
    # differing conclusion is part of the signed payload.
    head = _head(conclusion="failure", head_signature=_SIG_A)
    index = {
        "ci/test": _baseline(
            conclusion=baseline_conclusion, signature=_SIG_B, normalized_reason="boom"
        )
    }
    assert classify_failure_origin(head, "boom", index) == "introduced"


# ---------------------------------------------------------------------------
# Row 4 — stable baseline + matching signature → INHERITED.
# ---------------------------------------------------------------------------


def test_stable_baseline_matching_signature_is_inherited() -> None:
    # Row 4: stable baseline, equal signature, equal reason → INHERITED (FR-008).
    head = _head(conclusion="failure", head_signature=_SIG_A)
    index = {"ci/test": _baseline(signature=_SIG_A, normalized_reason="boom")}
    assert classify_failure_origin(head, "boom", index) == "inherited"


# ---------------------------------------------------------------------------
# Row 5 — no baseline match, or stable baseline different signature → INTRODUCED.
# ---------------------------------------------------------------------------


def test_no_same_name_baseline_failure_is_introduced() -> None:
    # Determinate baseline whose failures don't include this check → INTRODUCED.
    head = _head(name="ci/test", conclusion="failure")
    index = {
        "ci/other": _baseline(name="ci/other", signature=_SIG_A, normalized_reason="x")
    }
    assert classify_failure_origin(head, "boom", index) == "introduced"


def test_empty_determinate_baseline_is_introduced() -> None:
    # An empty dict ≠ None: the base WAS fetched and had zero failures, so a HEAD
    # failure is newly INTRODUCED — emphatically NOT unknown.
    head = _head(conclusion="failure")
    assert classify_failure_origin(head, "boom", {}) == "introduced"


def test_stable_baseline_different_signature_is_introduced() -> None:
    # The anti-masking ceiling (FR-009): a same-name stable baseline failure with
    # a DIFFERENT signature means HEAD fails for a different reason → INTRODUCED.
    head = _head(conclusion="failure", head_signature=_SIG_A)
    index = {"ci/test": _baseline(signature=_SIG_B, normalized_reason="other")}
    assert classify_failure_origin(head, "boom", index) == "introduced"


# ---------------------------------------------------------------------------
# Integration with make_failure_signature — the realistic classifier path.
# ---------------------------------------------------------------------------


def test_real_signatures_inherited_through_drift() -> None:
    head_sig, head_reason = make_failure_signature(
        "ci/test",
        "failure",
        "AssertionError at 2026-06-14T00:00:00Z run #123: expected 200",
        None,
    )
    base_sig, base_reason = make_failure_signature(
        "ci/test",
        "failure",
        "AssertionError at 2026-06-13T23:59:59Z run #456: expected 200",
        None,
    )
    head = _head(
        conclusion="failure", head_signature=head_sig, baseline_signature=base_sig
    )
    index = {
        "ci/test": _baseline(
            conclusion="failure", signature=base_sig, normalized_reason=base_reason
        )
    }
    assert classify_failure_origin(head, head_reason, index) == "inherited"


def test_real_signatures_introduced_on_different_root_cause() -> None:
    head_sig, head_reason = make_failure_signature(
        "ci/test", "failure", "ConnectionError: refused", None
    )
    base_sig, base_reason = make_failure_signature(
        "ci/test", "failure", "TimeoutError: deadline", None
    )
    head = _head(
        conclusion="failure", head_signature=head_sig, baseline_signature=base_sig
    )
    index = {
        "ci/test": _baseline(
            conclusion="failure", signature=base_sig, normalized_reason=base_reason
        )
    }
    assert classify_failure_origin(head, head_reason, index) == "introduced"


def test_classification_is_deterministic() -> None:
    head = _head(conclusion="failure", head_signature=_SIG_A)
    index = {"ci/test": _baseline(signature=_SIG_A, normalized_reason="boom")}
    first = classify_failure_origin(head, "boom", index)
    second = classify_failure_origin(head, "boom", index)
    assert first == second == "inherited"


# ---------------------------------------------------------------------------
# BaselineFailure value object — shape.
# ---------------------------------------------------------------------------


def test_baseline_failure_carries_reason_for_collision_detection() -> None:
    bf = _baseline(
        name="ci/test",
        conclusion="failure",
        signature=_SIG_A,
        normalized_reason="assertionerror: expected 200",
    )
    assert bf.name == "ci/test"
    assert bf.conclusion == "failure"
    assert bf.signature == _SIG_A
    assert bf.normalized_reason == "assertionerror: expected 200"

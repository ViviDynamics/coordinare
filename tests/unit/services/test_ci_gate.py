"""090 US2 (L2) — tests for the signature-bearing failed check and the
signature comparator.

Background
----------
Layer 2 of spec 090 (Baseline Repair Autonomy) classifies each failing HEAD
check against a merge-base baseline.  Two new pieces live in ``ci_gate.py``:

- ``FailedCheckWithSignature`` — a subclass of the existing ``FailedCheck``
  (``extra="forbid"``) that carries the head check's 16-char failure signature
  and, when a same-name baseline failure exists, the baseline's signature.  It
  must serialize identically to ``FailedCheck`` plus those two fields so a
  decision produced with L2 disabled is byte-identical to a pre-spec-090 one
  (SC-006).
- ``compare_signatures`` — the byte-equal comparator with **collision
  detection**.  Two genuinely distinct normalized reasons can (astronomically
  rarely) truncate to the same 16-char hash; the comparator must not let that
  masquerade as a real inherited match.  Because a real 64-bit truncation
  collision cannot be constructed deterministically, the comparator takes the
  normalized reasons alongside the hashes and returns a three-valued result:

  - ``"match"``     — equal hash **and** equal reason → INHERITED-eligible.
  - ``"distinct"``  — different hash → genuinely different reason → INTRODUCED.
  - ``"collision"`` — equal hash but **different** reason → route to UNKNOWN,
    never INHERITED (data-model §1 collision handling, §5 decision-table row 2).
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from coordinare.services.ci_gate import (
    CIGateDecision,
    FailedCheck,
    FailedCheckWithSignature,
    compare_signatures,
)
from coordinare.services.failure_signature import make_failure_signature

_SIG_A = "0123456789abcdef"
_SIG_B = "fedcba9876543210"


# ---------------------------------------------------------------------------
# FailedCheckWithSignature — shape, inheritance, extra="forbid".
# ---------------------------------------------------------------------------


def test_with_signature_is_a_failed_check_subclass() -> None:
    fc = FailedCheckWithSignature(
        name="ci/test", conclusion="failure", head_signature=_SIG_A
    )
    assert isinstance(fc, FailedCheck)


def test_with_signature_baseline_defaults_none() -> None:
    fc = FailedCheckWithSignature(
        name="ci/test", conclusion="failure", head_signature=_SIG_A
    )
    assert fc.head_signature == _SIG_A
    assert fc.baseline_signature is None


def test_with_signature_carries_both_signatures() -> None:
    fc = FailedCheckWithSignature(
        name="ci/test",
        conclusion="failure",
        head_signature=_SIG_A,
        baseline_signature=_SIG_A,
    )
    assert fc.head_signature == _SIG_A
    assert fc.baseline_signature == _SIG_A


def test_with_signature_requires_head_signature() -> None:
    with pytest.raises(ValidationError):
        FailedCheckWithSignature(name="ci/test", conclusion="failure")


def test_with_signature_forbids_extra_fields() -> None:
    # extra="forbid" must be preserved through the subclass.
    with pytest.raises(ValidationError):
        FailedCheckWithSignature(
            name="ci/test",
            conclusion="failure",
            head_signature=_SIG_A,
            bogus="nope",
        )


def test_with_signature_inherits_optional_failed_check_fields() -> None:
    fc = FailedCheckWithSignature(
        name="ci/test",
        conclusion="failure",
        html_url="https://example.test/run/1",
        last_log_line="AssertionError: boom",
        head_signature=_SIG_A,
    )
    assert fc.html_url == "https://example.test/run/1"
    assert fc.last_log_line == "AssertionError: boom"


def test_with_signature_truncates_last_log_line() -> None:
    # The inherited field_validator must still run on the subclass.
    fc = FailedCheckWithSignature(
        name="ci/test",
        conclusion="failure",
        last_log_line="x" * 500,
        head_signature=_SIG_A,
    )
    assert fc.last_log_line is not None
    assert len(fc.last_log_line) == 200


def test_with_signature_serializes_as_failed_check_plus_two_fields() -> None:
    base_keys = set(
        FailedCheck(name="ci/test", conclusion="failure").model_dump().keys()
    )
    sig_dump = FailedCheckWithSignature(
        name="ci/test", conclusion="failure", head_signature=_SIG_A
    ).model_dump()
    assert set(sig_dump.keys()) == base_keys | {
        "head_signature",
        "baseline_signature",
    }


# ---------------------------------------------------------------------------
# compare_signatures — match / distinct / collision.
# ---------------------------------------------------------------------------


def test_compare_equal_hash_equal_reason_is_match() -> None:
    assert (
        compare_signatures(
            head_signature=_SIG_A,
            head_reason="assertionerror: expected 200",
            baseline_signature=_SIG_A,
            baseline_reason="assertionerror: expected 200",
        )
        == "match"
    )


def test_compare_different_hash_is_distinct() -> None:
    assert (
        compare_signatures(
            head_signature=_SIG_A,
            head_reason="connectionerror: refused",
            baseline_signature=_SIG_B,
            baseline_reason="timeouterror: deadline",
        )
        == "distinct"
    )


def test_compare_equal_hash_different_reason_is_collision() -> None:
    # The masking guard: two distinct normalized reasons that truncate to the
    # same 16-char hash must NOT be treated as an inherited match.
    assert (
        compare_signatures(
            head_signature=_SIG_A,
            head_reason="connectionerror: refused",
            baseline_signature=_SIG_A,
            baseline_reason="timeouterror: deadline",
        )
        == "collision"
    )


def test_compare_different_hash_short_circuits_before_reason() -> None:
    # When the hashes differ we never trust the reasons for a match; the result
    # is "distinct" even if the (inconsistent) reasons happen to be equal.
    assert (
        compare_signatures(
            head_signature=_SIG_A,
            head_reason="same reason",
            baseline_signature=_SIG_B,
            baseline_reason="same reason",
        )
        == "distinct"
    )


def test_compare_empty_reasons_match() -> None:
    # Both reasons absent normalize to "" — still a legitimate match.
    assert (
        compare_signatures(
            head_signature=_SIG_A,
            head_reason="",
            baseline_signature=_SIG_A,
            baseline_reason="",
        )
        == "match"
    )


# ---------------------------------------------------------------------------
# Integration with make_failure_signature — the realistic classifier path.
# ---------------------------------------------------------------------------


def test_compare_real_signatures_same_root_cause_with_drift_match() -> None:
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
    assert head_sig == base_sig  # drift stripped → equal hashes
    assert (
        compare_signatures(
            head_signature=head_sig,
            head_reason=head_reason,
            baseline_signature=base_sig,
            baseline_reason=base_reason,
        )
        == "match"
    )


def test_compare_real_signatures_different_root_cause_distinct() -> None:
    head_sig, head_reason = make_failure_signature(
        "ci/test", "failure", "ConnectionError: refused", None
    )
    base_sig, base_reason = make_failure_signature(
        "ci/test", "failure", "TimeoutError: deadline", None
    )
    assert head_sig != base_sig
    assert (
        compare_signatures(
            head_signature=head_sig,
            head_reason=head_reason,
            baseline_signature=base_sig,
            baseline_reason=base_reason,
        )
        == "distinct"
    )


# ---------------------------------------------------------------------------
# CIGateDecision classification lists — observe-only (SC-006).
# ---------------------------------------------------------------------------
# L2 attaches four classification lists to the decision.  The exactly-one-
# classification model_validator is observe-only: a no-op when all four are
# empty (so every pre-spec-090 decision stays valid and byte-identical), and a
# completeness guard when populated.  It must never read or mutate ``verdict``,
# and the original ``_validate_verdict_invariants`` must keep running.

_SHA40 = "a" * 40


def _bounce(**overrides: object) -> CIGateDecision:
    base: dict[str, object] = dict(
        verdict="bounce",
        head_sha=_SHA40,
        required_checks=["integration", "lint", "unit-tests"],
        failed_checks=[
            FailedCheck(name="unit-tests", conclusion="failure"),
        ],
        pending_checks=[],
        resolver_source="persona_check_map",
        bounce_count_after=1,
        decided_at="2026-06-14T00:00:00.000Z",
    )
    base.update(overrides)
    return CIGateDecision(**base)  # type: ignore[arg-type]


def test_classification_lists_default_empty() -> None:
    d = _bounce()
    assert d.inherited_checks == []
    assert d.introduced_checks == []
    assert d.flake_checks == []
    assert d.unknown_checks == []


def test_classification_validator_is_noop_when_all_empty() -> None:
    # A pre-spec-090 decision (no lists populated) stays valid — SC-006.
    d = _bounce()
    assert d.verdict == "bounce"
    assert d.failed_checks[0].name == "unit-tests"


def test_classification_lists_do_not_change_verdict_or_signature() -> None:
    # Observe-only: attaching classification leaves verdict and the dedup
    # signature byte-identical to the un-classified decision (SC-006).
    plain = _bounce()
    classified = _bounce(
        introduced_checks=[
            FailedCheckWithSignature(
                name="unit-tests", conclusion="failure", head_signature=_SIG_A
            )
        ],
    )
    assert classified.verdict == plain.verdict == "bounce"
    assert classified.signature() == plain.signature()


def test_every_failed_check_classified_exactly_once_is_valid() -> None:
    d = _bounce(
        failed_checks=[
            FailedCheck(name="unit-tests", conclusion="failure"),
            FailedCheck(name="lint", conclusion="failure"),
        ],
        inherited_checks=[
            FailedCheckWithSignature(
                name="unit-tests", conclusion="failure", head_signature=_SIG_A
            )
        ],
        flake_checks=[FailedCheck(name="lint", conclusion="timed_out")],
    )
    assert {c.name for c in d.inherited_checks} == {"unit-tests"}
    assert {c.name for c in d.flake_checks} == {"lint"}


def test_unclassified_failed_check_when_lists_populated_is_error() -> None:
    # Lists are non-empty but a failing check is classified by none of them.
    with pytest.raises(ValidationError):
        _bounce(
            failed_checks=[
                FailedCheck(name="unit-tests", conclusion="failure"),
                FailedCheck(name="lint", conclusion="failure"),
            ],
            inherited_checks=[
                FailedCheckWithSignature(
                    name="unit-tests", conclusion="failure", head_signature=_SIG_A
                )
            ],
        )


def test_classification_naming_absent_check_is_error() -> None:
    # A classification list names a check that is not in failed_checks.
    with pytest.raises(ValidationError):
        _bounce(
            unknown_checks=[FailedCheck(name="ghost", conclusion="failure")],
        )


def test_check_classified_more_than_once_is_error() -> None:
    # "exactly one list" — the same check may not appear in two lists.
    with pytest.raises(ValidationError):
        _bounce(
            inherited_checks=[
                FailedCheckWithSignature(
                    name="unit-tests", conclusion="failure", head_signature=_SIG_A
                )
            ],
            unknown_checks=[FailedCheck(name="unit-tests", conclusion="failure")],
        )


def test_verdict_invariants_still_run_alongside_classification() -> None:
    # The original _validate_verdict_invariants is untouched: a pass decision
    # with a failed check still raises regardless of the classification lists.
    with pytest.raises(ValidationError):
        CIGateDecision(
            verdict="pass",
            head_sha=_SHA40,
            required_checks=["lint"],
            failed_checks=[FailedCheck(name="lint", conclusion="failure")],
            resolver_source="all_head_checks",
            bounce_count_after=0,
            decided_at="2026-06-14T00:00:00.000Z",
        )

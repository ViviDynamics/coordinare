"""095 (T006/T007): ENV_BLOCKED is a Row-0 short-circuit in classify_failure_origin.

An infra/environment failure (matched by signature) is classified ``env_blocked``
BEFORE the inherited/introduced/flake rows — it is never the card's fault and
never a repair candidate. Evaluated on the HEAD failure alone (no baseline
dependency). When the gate is off (``env_patterns=None``) classification is
identical to the pre-feature baseline.
"""
from __future__ import annotations

from coordinare.services.ci_gate import FailedCheckWithSignature
from coordinare.services.failure_classification import (
    BaselineFailure,
    classify_failure_origin,
)

_SIG = "0123456789abcdef"
_QUOTA = "artifact storage quota has been hit"


def _head(reason_sig: str = _SIG, conclusion: str = "failure") -> FailedCheckWithSignature:
    return FailedCheckWithSignature(
        name="Build Pull Request",
        conclusion=conclusion,
        head_signature=reason_sig,
        baseline_signature=None,
    )


def _baseline(reason: str = _QUOTA, sig: str = _SIG) -> dict[str, BaselineFailure]:
    return {
        "Build Pull Request": BaselineFailure(
            name="Build Pull Request",
            conclusion="failure",
            signature=sig,
            normalized_reason=reason,
        )
    }


def test_env_blocked_takes_priority_over_inherited() -> None:
    """#159 (SC-007): a stable failure that ALSO fails identically on the
    baseline (would be INHERITED) but whose reason is an infra signature is
    classified env_blocked, not inherited."""
    result = classify_failure_origin(
        _head(), _QUOTA, _baseline(), env_patterns=[]
    )
    assert result == "env_blocked"


def test_non_infra_failure_falls_through_to_existing_logic() -> None:
    """FR-003: a non-infra reason is classified by the existing rows unchanged."""
    result = classify_failure_origin(
        _head(), "expected 0px got 24px", _baseline(reason="expected 0px got 24px"),
        env_patterns=[],
    )
    assert result == "inherited"  # same-signature stable baseline → inherited


def test_env_blocked_independent_of_baseline() -> None:
    """FR-011: env detection works on the HEAD alone — even when the baseline
    is indeterminate (None)."""
    result = classify_failure_origin(_head(), _QUOTA, None, env_patterns=[])
    assert result == "env_blocked"


def test_gate_off_is_identical_to_baseline() -> None:
    """FR-012/SC-006: with env_patterns=None the infra reason classifies exactly
    as before (no env_blocked)."""
    result = classify_failure_origin(_head(), _QUOTA, _baseline(), env_patterns=None)
    assert result == "inherited"


def test_env_block_does_not_mask_introduced() -> None:
    """095 (T015/FR-009): on a MIXED card the env-blocked check is labeled
    env_blocked while an unrelated NEW failure is still classified INTRODUCED —
    classification is per-check, so the infra hold never swallows a code defect."""
    # Check A — infra reason, also fails identically on base → env_blocked.
    check_a = FailedCheckWithSignature(
        name="Build Pull Request", conclusion="failure",
        head_signature=_SIG, baseline_signature=_SIG,
    )
    a = classify_failure_origin(check_a, _QUOTA, _baseline(), env_patterns=[])
    # Check B — a genuine new test failure absent from the baseline → introduced.
    check_b = FailedCheckWithSignature(
        name="unit-tests", conclusion="failure",
        head_signature="feedfacefeedface", baseline_signature=None,
    )
    b = classify_failure_origin(check_b, "2 examples, 1 failure", {}, env_patterns=[])

    assert a == "env_blocked"
    assert b == "introduced"

"""Unit tests for the L1 base-precondition gate (spec 090, US1).

`evaluate_base_gate` is pure: it consumes a *base*-origin `CheckRollup` (fetched
upstream by `get_base_branch_check_rollup`) and returns a `BaseGateDecision`
whose `decision` literal drives the `monitor_pr.py` merge-hold branch. These
tests pin the contract in data-model §11 and FR-001 through FR-006 / SC-002:

* a **required** base check in `failure` → BLOCK naming that check (+ URL),
* only **non-required** base failures → PROCEED (FR-004),
* a `None` base rollup → INDETERMINATE — fall-safe, **never** BLOCK (FR-005),
* the gate is re-read each cycle and never latched (FR-006).

A base rollup has `head_pushed_at=None` (the base is a long-lived branch, not a
PR HEAD), so these tests also exercise the None-timestamp guard that
`pr_checks_policy.decide` needs for base rollups.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from coordinare.services.base_gate import BaseGateDecision, evaluate_base_gate
from coordinare.services.pr_checks_service import CheckEntry, CheckRollup

NOW = datetime(2026, 6, 14, 12, 0, 0, tzinfo=UTC)


def _check(
    name: str,
    status: str,
    conclusion: str | None = None,
    *,
    required: bool = False,
    url: str | None = None,
) -> CheckEntry:
    return CheckEntry(
        name=name,
        status=status,  # type: ignore[arg-type]
        conclusion=conclusion,  # type: ignore[arg-type]
        is_required=required,
        details_url=url,
    )


def _base_rollup(checks: list[CheckEntry], *, bp_readable: bool = True) -> CheckRollup:
    """A base-origin rollup. `head_pushed_at=None` mirrors `parse_base_rollup`."""
    return CheckRollup(
        pr_number=0,
        head_sha="base0000",
        head_pushed_at=None,
        branch_protection_readable=bp_readable,
        checks=checks,
        rollup_origin="base",
        base_ref="main",
    )


# --- BLOCK: a required base check is red (FR-002, FR-003) -------------------


def test_required_base_failure_blocks_naming_check_and_url() -> None:
    rollup = _base_rollup(
        [
            _check(
                "ci/test",
                "completed",
                "failure",
                required=True,
                url="https://github.com/o/r/runs/42",
            ),
        ],
    )
    d = evaluate_base_gate(rollup, now=NOW)
    assert d.decision == "BLOCK"
    assert [c.name for c in d.failing_checks] == ["ci/test"]
    assert d.failing_checks[0].html_url == "https://github.com/o/r/runs/42"
    assert d.failing_checks[0].conclusion == "failure"


@pytest.mark.parametrize(
    "conclusion",
    ["failure", "cancelled", "timed_out", "action_required", "stale", "startup_failure"],
)
def test_all_terminal_failure_conclusions_block(conclusion: str) -> None:
    rollup = _base_rollup([_check("ci/test", "completed", conclusion, required=True)])
    d = evaluate_base_gate(rollup, now=NOW)
    assert d.decision == "BLOCK"
    assert [c.name for c in d.failing_checks] == ["ci/test"]


def test_block_lists_only_the_offending_required_check() -> None:
    rollup = _base_rollup(
        [
            _check("ci/test", "completed", "failure", required=True),
            _check("build", "completed", "success", required=True),
            _check("lint", "completed", "success", required=True),
        ],
    )
    d = evaluate_base_gate(rollup, now=NOW)
    assert d.decision == "BLOCK"
    assert [c.name for c in d.failing_checks] == ["ci/test"]


# --- PROCEED: green base, or only non-required red (FR-004) -----------------


def test_all_green_required_base_proceeds() -> None:
    rollup = _base_rollup(
        [
            _check("ci/test", "completed", "success", required=True),
            _check("build", "completed", "success", required=True),
        ],
    )
    d = evaluate_base_gate(rollup, now=NOW)
    assert d.decision == "PROCEED"
    assert d.failing_checks == []


def test_non_required_base_failure_proceeds() -> None:
    """Only a *non-required* base check is red → PROCEED, not BLOCK (FR-004)."""
    rollup = _base_rollup(
        [
            _check("ci/test", "completed", "success", required=True),
            _check("optional-coverage", "completed", "failure", required=False),
        ],
    )
    d = evaluate_base_gate(rollup, now=NOW)
    assert d.decision == "PROCEED"
    assert d.failing_checks == []


def test_pending_required_base_does_not_block() -> None:
    """L1 blocks only on a RED required base, not a still-running one.

    The required check is in_progress → policy HOLDs → the gate PROCEEDs (L1's
    job is to refuse merge onto a *red* base, not to wait on the base build).
    Exercises the `head_pushed_at=None` guard in `decide`: a base rollup has no
    push timestamp, so the pending-timeout arithmetic must not crash.
    """
    rollup = _base_rollup(
        [
            _check("ci/test", "in_progress", required=True),
            _check("build", "completed", "success", required=True),
        ],
    )
    d = evaluate_base_gate(rollup, now=NOW)
    assert d.decision == "PROCEED"
    assert d.failing_checks == []


def test_branch_protection_unreadable_base_proceeds() -> None:
    """Fail-safe: an unreadable base required-set must never hard-block (FR-005).

    Without a readable required set we cannot prove a base check is *required*,
    so we must fall through rather than block on advisory failures.
    """
    rollup = _base_rollup(
        [_check("ci/test", "completed", "failure", required=False)],
        bp_readable=False,
    )
    d = evaluate_base_gate(rollup, now=NOW)
    assert d.decision == "PROCEED"
    assert d.failing_checks == []


# --- INDETERMINATE: unfetchable base, never BLOCK (FR-005, SC-002) ----------


def test_none_base_rollup_is_indeterminate() -> None:
    d = evaluate_base_gate(None, now=NOW)
    assert d.decision == "INDETERMINATE"
    assert d.failing_checks == []


def test_indeterminate_is_never_block() -> None:
    """SC-002: an indeterminate base must fall through to head-only, never BLOCK."""
    d = evaluate_base_gate(None, now=NOW)
    assert d.decision != "BLOCK"


# --- No latch (FR-006): re-evaluation re-reads, holds no state --------------


def test_no_latch_block_then_proceed_across_cycles() -> None:
    """A red base BLOCKs; the same gate PROCEEDs once the base turns green.

    `evaluate_base_gate` is pure, so "no latch" means a later call with a green
    base is not poisoned by an earlier BLOCK.
    """
    red = _base_rollup([_check("ci/test", "completed", "failure", required=True)])
    green = _base_rollup([_check("ci/test", "completed", "success", required=True)])

    assert evaluate_base_gate(red, now=NOW).decision == "BLOCK"
    assert evaluate_base_gate(green, now=NOW).decision == "PROCEED"
    # And re-reading the still-red base BLOCKs again (no "already escalated" memo).
    assert evaluate_base_gate(red, now=NOW).decision == "BLOCK"


# --- Determinism: no clock/RNG/network in the unit path (T038) --------------


def test_evaluate_base_gate_is_deterministic() -> None:
    """Given a fixed rollup + injected `now`, repeated calls are byte-identical.

    Pins the Constitution II determinism requirement for the L1 gate: with `now`
    injected there is no clock read, no RNG, and no network in the decision path,
    so the result is a pure function of its inputs.
    """
    rollup = _base_rollup(
        [
            _check("ci/test", "completed", "failure", required=True, url="https://x/1"),
            _check("build", "completed", "success", required=True),
        ],
    )
    first = evaluate_base_gate(rollup, now=NOW)
    for _ in range(50):
        again = evaluate_base_gate(rollup, now=NOW)
        assert again.model_dump() == first.model_dump()


# --- persona_check_map narrowing flows through the resolver -----------------


def test_persona_check_map_narrows_required_set() -> None:
    """A persona_check_map that excludes the failing check → PROCEED.

    The failing base check is not in the narrowed required set, so it is treated
    as non-required and does not block (proves the resolver is consulted).
    """
    rollup = _base_rollup(
        [
            _check("ci/test", "completed", "failure", required=True),
            _check("build", "completed", "success", required=True),
        ],
    )
    persona_check_map = {"implementer": {"any": ["build"]}}
    d = evaluate_base_gate(rollup, None, persona_check_map=persona_check_map, now=NOW)
    assert d.decision == "PROCEED"
    assert d.failing_checks == []


# --- BaseGateDecision model invariants --------------------------------------


def test_decision_defaults_to_empty_failing_checks() -> None:
    d = BaseGateDecision(decision="PROCEED")
    assert d.failing_checks == []


def test_decision_model_is_frozen() -> None:
    d = BaseGateDecision(decision="PROCEED")
    with pytest.raises(ValidationError):
        d.decision = "BLOCK"  # type: ignore[misc]


def test_decision_model_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        BaseGateDecision(decision="PROCEED", unknown_field=True)  # type: ignore[call-arg]

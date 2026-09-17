"""Spec 161 — per-(role, backend) rollup of dispatch evidence.

T008 (US1): grouping, dedup, count invariant (FR-007, FR-008).
T009 (US1): absent-vs-zero — no conclusive dispatches yields None, never 0.0 (FR-013).
T010 (US1): a never-dispatched role produces no row; a retry still records its defect.
T011 (US1): an unknown artifact schema version is refused (edge case).
T015a (US1): pooling retains per_run_scalars, so the tie rule keeps its variance (FR-025).
"""

from __future__ import annotations

import pytest

from coordinare.bench.harness_rollup import RollupError, roll_up
from tests.unit.test_161_fixtures import artifact, card, dispatch, simple_artifact


def _row(rollup, role: str, backend: str):
    matches = [r for r in rollup.rows if r.role == role and r.backend == backend]
    assert len(matches) == 1, f"expected exactly one row for {role}/{backend}, got {matches}"
    return matches[0]


# --- T008: grouping, dedup, invariants ------------------------------------------------


def test_groups_by_role_and_backend() -> None:
    """FR-007: one row per (role, backend) pair observed in the dispatches."""
    art = simple_artifact([
        ("reviewer", "openclaw", "approved"),
        ("reviewer", "openclaw", "changes_requested"),
        ("assessor", "junie", "malformed_output"),
    ])
    rollup = roll_up([art])

    assert {(r.role, r.backend) for r in rollup.rows} == {
        ("reviewer", "openclaw"),
        ("assessor", "junie"),
    }
    assert _row(rollup, "reviewer", "openclaw").dispatches == 2


def test_grouping_uses_the_dispatch_not_the_config() -> None:
    """The artifact is the evidence: a dispatch's own backend decides its row."""
    art = simple_artifact([("reviewer", "junie", "approved")])
    assert _row(roll_up([art]), "reviewer", "junie").credit == 1


def test_count_invariant_holds() -> None:
    """FR-008: the four class counts must sum to the dispatch total."""
    art = simple_artifact([
        ("qa", "claude_code", "qa_passed"),
        ("qa", "claude_code", "malformed_output"),
        ("qa", "claude_code", "env_blocked"),
        ("qa", "claude_code", None),
    ])
    row = _row(roll_up([art]), "qa", "claude_code")

    assert row.credit == 1
    assert row.harness_defect == 1
    assert row.environment == 1
    assert row.inconclusive == 1
    assert row.credit + row.harness_defect + row.environment + row.inconclusive == row.dispatches
    assert row.conclusive == 2  # credit + defect only


def test_rates_are_over_conclusive_only() -> None:
    """FR-008: environment and inconclusive leave the denominator alone."""
    art = simple_artifact([
        ("qa", "claude_code", "qa_passed"),
        ("qa", "claude_code", "malformed_output"),
        ("qa", "claude_code", "env_blocked"),
        ("qa", "claude_code", None),
    ])
    row = _row(roll_up([art]), "qa", "claude_code")

    assert row.defect_rate == pytest.approx(0.5)
    assert row.credit_rate == pytest.approx(0.5)


def test_a_dispatch_under_several_stages_is_counted_once() -> None:
    """FR-007: the same session registered under multiple stages is one dispatch."""
    art = artifact([
        card("c1", [
            dispatch("reviewer", "openclaw", "approved", stage="reviewing", session_id="s1"),
            dispatch("reviewer", "openclaw", "approved", stage="monitoring", session_id="s1"),
        ]),
    ])
    assert _row(roll_up([art]), "reviewer", "openclaw").dispatches == 1


def test_negative_verdict_is_credit_in_the_rollup() -> None:
    """The feature's whole point, asserted at the rollup level too."""
    art = simple_artifact([
        ("reviewer", "openclaw", "changes_requested"),
        ("reviewer", "openclaw", "qa_failed"),
    ])
    row = _row(roll_up([art]), "reviewer", "openclaw")

    assert row.credit == 2
    assert row.harness_defect == 0
    assert row.defect_rate == pytest.approx(0.0)


# --- T009: absent is not zero ---------------------------------------------------------


def test_no_conclusive_dispatches_yields_none_rates_not_zero() -> None:
    """FR-013: a 0.0 defect rate would read as a flawless harness. Nothing is known here,
    so the rate must be withheld."""
    art = simple_artifact([
        ("env_bootstrap", "opencode", "env_blocked"),
        ("env_bootstrap", "opencode", None),
    ])
    row = _row(roll_up([art]), "env_bootstrap", "opencode")

    assert row.conclusive == 0
    assert row.defect_rate is None
    assert row.credit_rate is None

    # `is None` above IS the assertion that separates "nothing measured" from "measured
    # clean": if the implementation returned 0.0 it would fail. Nothing further is needed.
    #
    # An earlier version added `assert row.defect_rate is not None or row.defect_rate != 0.0`
    # thinking it strengthened this. It is a TAUTOLOGY — for None the second clause holds,
    # for any number the first does — so it passed for every possible value and enforced
    # nothing. Recorded here because it is the exact shape of a test that looks like it
    # guards an invariant and does not.


def test_tokens_absent_is_none_not_zero() -> None:
    """FR-008: summing must treat missing tokens as absent, never as zero."""
    art = artifact([
        card("c1", [dispatch("qa", "claude_code", "qa_passed", tokens=None)]),
    ])
    assert _row(roll_up([art]), "qa", "claude_code").tokens_total is None


def test_a_partial_token_sum_is_flagged_not_presented_as_complete() -> None:
    """FR-013's spirit: a partial measurement must not pass as a complete one.

    When some dispatches report tokens and others do not, summing the ones present yields
    a number that LOOKS like the total but understates it by however much is missing. The
    sum is still useful as a lower bound, so it is kept — but flagged, so the cost term can
    decline to use it rather than penalize a harness on an undercount.
    """
    art = artifact([
        card("c1", [
            dispatch("reviewer", "openclaw", "approved", tokens=1000, session_id="s1"),
            dispatch("reviewer", "openclaw", "approved", tokens=None, session_id="s2"),
        ]),
    ])
    row = _row(roll_up([art]), "reviewer", "openclaw")

    assert row.tokens_total == 1000
    assert row.tokens_partial is True, "1 of 2 dispatches reported tokens"


def test_a_complete_token_sum_is_not_flagged_partial() -> None:
    art = artifact([
        card("c1", [
            dispatch("reviewer", "openclaw", "approved", tokens=100, session_id="s1"),
            dispatch("reviewer", "openclaw", "approved", tokens=50, session_id="s2"),
        ]),
    ])
    row = _row(roll_up([art]), "reviewer", "openclaw")

    assert row.tokens_total == 150
    assert row.tokens_partial is False


def test_no_tokens_at_all_is_absent_not_partial() -> None:
    """Nothing reported is `None` (absent), which is distinct from a partial sum."""
    art = artifact([
        card("c1", [dispatch("qa", "claude_code", "qa_passed", tokens=None)]),
    ])
    row = _row(roll_up([art]), "qa", "claude_code")

    assert row.tokens_total is None
    assert row.tokens_partial is False


def test_tokens_sum_when_present() -> None:
    art = artifact([
        card("c1", [
            dispatch("qa", "claude_code", "qa_passed", tokens=100),
            dispatch("qa", "claude_code", "qa_passed", tokens=50),
        ]),
    ])
    assert _row(roll_up([art]), "qa", "claude_code").tokens_total == 150


# --- T010: no evidence vs measured zero, and retries ----------------------------------


def test_a_never_dispatched_role_produces_no_row() -> None:
    """Edge case: absence of evidence must not look like a measured result."""
    art = simple_artifact([("reviewer", "openclaw", "approved")])
    rollup = roll_up([art])

    assert not [r for r in rollup.rows if r.role == "security"]


def test_a_retry_that_later_succeeds_still_records_the_defect() -> None:
    """Edge case: a harness needing retries is worse than one that does not, so the
    defect is counted per dispatch rather than absorbed by the card's happy ending."""
    art = artifact([
        card("c1", [
            dispatch("qa", "claude_code", "malformed_output", session_id="s1"),
            dispatch("qa", "claude_code", "qa_passed", session_id="s2"),
        ], final_state="merged"),
    ])
    row = _row(roll_up([art]), "qa", "claude_code")

    assert row.harness_defect == 1
    assert row.credit == 1
    assert row.defect_rate == pytest.approx(0.5)


def test_unknown_markers_are_surfaced() -> None:
    """FR-006: a new marker must be visible, not absorbed into inconclusive silently."""
    art = simple_artifact([("qa", "claude_code", "some_future_marker")])
    row = _row(roll_up([art]), "qa", "claude_code")

    assert row.inconclusive == 1
    assert row.unknown_markers == ["some_future_marker"]


# --- T011: schema refusal -------------------------------------------------------------


def test_unknown_artifact_schema_version_is_refused() -> None:
    """Edge case: refuse rather than partially interpret, matching existing scoring."""
    art = simple_artifact([("qa", "claude_code", "qa_passed")])
    art.schema_version = 999

    with pytest.raises(RollupError, match="schema version"):
        roll_up([art])


# --- T015a: pooling must retain per-run variance --------------------------------------


def test_pooling_sums_counts_and_records_contributing_runs() -> None:
    """FR-025: rates come from the pooled total."""
    a = simple_artifact([("qa", "claude_code", "qa_passed")], run_id="run-1")
    b = simple_artifact([("qa", "claude_code", "malformed_output")], run_id="run-2")
    row = _row(roll_up([a, b]), "qa", "claude_code")

    assert row.dispatches == 2
    assert row.runs_contributing == 2
    assert row.defect_rate == pytest.approx(0.5)


def test_pooling_retains_per_run_scalars() -> None:
    """FR-025 / FR-020: pooling alone collapses the sample to a single value, leaving the
    tie rule with no standard deviation and silently degrading it to exact equality. The
    per-run credit rates must survive pooling so run-to-run spread stays computable.
    """
    a = simple_artifact([("qa", "claude_code", "qa_passed")], run_id="run-1")
    b = simple_artifact([("qa", "claude_code", "malformed_output")], run_id="run-2")
    row = _row(roll_up([a, b]), "qa", "claude_code")

    assert len(row.per_run_scalars) == 2
    assert sorted(row.per_run_scalars) == [pytest.approx(0.0), pytest.approx(1.0)]


def test_runs_contributing_counts_runs_with_any_dispatch_not_just_conclusive_ones() -> None:
    """FR-025: `runs_contributing` records how many runs fed the row.

    Regression: it was incremented only for runs that produced CONCLUSIVE dispatches, so a
    pair whose dispatches were all environment or inconclusive reported 0 contributing runs
    while carrying real dispatches. That understates the evidence base and feeds
    `single_run` in the ranking, which reads `runs_contributing <= 1`.
    """
    a = simple_artifact([
        ("env_bootstrap", "opencode", "env_blocked"),
        ("env_bootstrap", "opencode", None),
    ], run_id="run-1")
    b = simple_artifact([("env_bootstrap", "opencode", "env_blocked")], run_id="run-2")

    row = _row(roll_up([a, b]), "env_bootstrap", "opencode")

    assert row.dispatches == 3
    assert row.conclusive == 0
    assert row.runs_contributing == 2, "both runs fed this row, conclusive or not"
    # No conclusive evidence, so still no fabricated spread.
    assert row.per_run_scalars == []


def test_runs_contributing_does_not_double_count_within_one_run() -> None:
    """Several dispatches in one run are still one contributing run."""
    art = simple_artifact([
        ("reviewer", "openclaw", "approved"),
        ("reviewer", "openclaw", "approved"),
        ("reviewer", "openclaw", "malformed_output"),
    ], run_id="run-1")

    assert _row(roll_up([art]), "reviewer", "openclaw").runs_contributing == 1


def test_per_run_scalars_omit_runs_with_no_conclusive_evidence() -> None:
    """A run contributing only environment failures has no rate, so it must not
    contribute a fabricated 0.0 to the spread."""
    a = simple_artifact([("qa", "claude_code", "qa_passed")], run_id="run-1")
    b = simple_artifact([("qa", "claude_code", "env_blocked")], run_id="run-2")
    row = _row(roll_up([a, b]), "qa", "claude_code")

    assert row.per_run_scalars == [pytest.approx(1.0)]


def test_rollup_round_trips_against_its_own_schema() -> None:
    """FR-009 / F9: prove the artifact validates before it is declared written."""
    art = simple_artifact([("qa", "claude_code", "qa_passed")])
    rollup = roll_up([art])

    from coordinare.bench.harness_rollup import HarnessRollup

    assert HarnessRollup.model_validate_json(rollup.to_validated_json()).run_ids == ["run-1"]


def test_rollup_does_not_mutate_the_artifact() -> None:
    """Contract: the rollup is read-only over its input."""
    art = simple_artifact([("qa", "claude_code", "qa_passed")])
    before = art.to_validated_json()
    roll_up([art])

    assert art.to_validated_json() == before

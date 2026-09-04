"""Spec 161 — harness-comparison scoring view, ties, and ranking verdicts.

T028 (US3): the view discriminator is carried and blocks cross-view comparison (FR-012).
T030 (US3): the objective mirrors compute_scalar's shape (FR-010, FR-024).
T031 (US3): a scalar is withheld as None, never 0.0 (FR-013).
T031a (US3): the cost term's units (FR-024).
T032 (US3): the tie rule reads per-run scalars (FR-020).
T033 (US3): insufficient evidence (FR-021).
T034 (US3): no_viable_harness (FR-027).
T035 (US3): evidence citation, and advisory-only output (FR-022, FR-023).
"""

from __future__ import annotations

import pytest

from coordinare.bench.harness_rank import (
    DEFAULT_MIN_CONCLUSIVE,
    HarnessRankingReport,
    format_report,
    rank,
    rank_role,
    score_row,
)
from coordinare.bench.harness_rollup import RoleHarnessRow, roll_up
from coordinare.bench.score import ScoringView, Weights
from tests.unit.test_161_fixtures import simple_artifact


def _row(
    backend: str,
    credit: int,
    defect: int,
    *,
    role: str = "reviewer",
    seconds: float = 0.0,
    tokens: int | None = None,
    per_run: list[float] | None = None,
    runs: int = 1,
) -> RoleHarnessRow:
    return RoleHarnessRow(
        role=role,
        backend=backend,
        dispatches=credit + defect,
        credit=credit,
        harness_defect=defect,
        seconds_total=seconds,
        tokens_total=tokens,
        runs_contributing=runs,
        per_run_scalars=per_run if per_run is not None else [],
    )


# --- T028: the view discriminator -----------------------------------------------------


def test_harness_scores_carry_the_harness_view() -> None:
    """FR-012: every emitted score identifies the objective behind it."""
    s = score_row(_row("openclaw", 5, 0))
    assert s.view is ScoringView.HARNESS_COMPARISON


def test_existing_scores_default_to_the_config_view() -> None:
    """FR-011/FR-012: the pre-existing objective keeps its identity, so a config sweep's
    scalars are not silently reinterpreted as harness scores."""
    from datetime import UTC, datetime

    from coordinare.bench.score import ScoreObject

    s = ScoreObject(run_id="r", artifact_schema_version=1, scored_at=datetime.now(UTC))
    assert s.view is ScoringView.CONFIG_COMPARISON


def test_the_two_views_are_distinct_values() -> None:
    """They treat harness failure in opposite ways, so they must never compare equal."""
    assert ScoringView.CONFIG_COMPARISON != ScoringView.HARNESS_COMPARISON


# --- T030 / T031a: objective shape and units -----------------------------------------


def test_objective_is_credit_rate_when_penalties_are_free() -> None:
    """FR-024: with no time or token cost, the scalar is the credit term alone."""
    s = score_row(_row("openclaw", 3, 1))  # credit_rate = 0.75
    assert s.credit_rate == pytest.approx(0.75)
    assert s.scalar == pytest.approx(0.75)


def test_time_penalty_reduces_the_scalar() -> None:
    """FR-024: the time term is normalized by the time budget."""
    w = Weights(w_correctness=1.0, w_cost=0.0, w_time=0.1, time_budget_seconds=100.0)
    s = score_row(_row("openclaw", 4, 0, seconds=50.0), w)
    # 1.0 - 0.1*(50/100) = 0.95
    assert s.scalar == pytest.approx(0.95)


def test_a_slower_harness_scores_lower_all_else_equal() -> None:
    w = Weights(w_cost=0.0, w_time=0.1, time_budget_seconds=100.0)
    fast = score_row(_row("a", 4, 0, seconds=10.0), w)
    slow = score_row(_row("b", 4, 0, seconds=90.0), w)
    assert fast.scalar > slow.scalar


def test_defects_lower_the_scalar_which_is_the_whole_point() -> None:
    """FR-010: harness defects count AGAINST the harness, unlike the config view."""
    clean = score_row(_row("a", 4, 0))
    broken = score_row(_row("b", 2, 2))
    assert clean.scalar > broken.scalar


def test_cost_term_converts_tokens_to_currency_before_dividing() -> None:
    """FR-024 unit rule: USD/USD. Dividing a raw token count by a USD budget would be a
    unit error, and with realistic token counts it would swamp every other term."""
    w = Weights(w_correctness=1.0, w_cost=1.0, w_time=0.0, cost_budget_usd=1.0)
    # 1_000_000 tokens at $2/M = $2.00; 1.0 - 1.0*(2.0/1.0) = -1.0
    s = score_row(_row("a", 4, 0, tokens=1_000_000), w, cost_per_million_tokens=2.0)
    assert s.scalar == pytest.approx(-1.0)
    assert s.cost_component_missing is False


def test_missing_tokens_omit_the_cost_term_rather_than_assuming_free() -> None:
    """FR-024: unknown cost is recorded as unknown, never treated as zero."""
    w = Weights(w_correctness=1.0, w_cost=1.0, w_time=0.0, cost_budget_usd=1.0)
    s = score_row(_row("a", 4, 0, tokens=None), w, cost_per_million_tokens=2.0)

    assert s.cost_component_missing is True
    assert s.scalar == pytest.approx(1.0)  # credit term only, cost omitted


def test_a_partial_token_sum_omits_the_cost_term_rather_than_undercharging() -> None:
    """A partial sum understates cost, so penalizing on it would flatter a harness whose
    token reporting happens to be patchy. Decline the term and record it missing, the same
    treatment as no tokens at all."""
    w = Weights(w_correctness=1.0, w_cost=1.0, w_time=0.0, cost_budget_usd=1.0)
    row = _row("a", 4, 0, tokens=1_000_000)
    row.tokens_partial = True

    s = score_row(row, w, cost_per_million_tokens=2.0)

    assert s.cost_component_missing is True
    assert s.scalar == pytest.approx(1.0), "cost omitted, not computed from a lower bound"


def test_a_complete_token_sum_still_charges_cost() -> None:
    """The converse, so the partial guard cannot silently disable costing entirely."""
    w = Weights(w_correctness=1.0, w_cost=1.0, w_time=0.0, cost_budget_usd=1.0)
    row = _row("a", 4, 0, tokens=1_000_000)
    assert row.tokens_partial is False

    s = score_row(row, w, cost_per_million_tokens=2.0)

    assert s.cost_component_missing is False
    assert s.scalar == pytest.approx(-1.0)


def test_weights_are_embedded_in_the_score() -> None:
    """FR-024: scalars compare only across matching weights, so carry them."""
    w = Weights(w_time=0.5, time_budget_seconds=42.0)
    s = score_row(_row("a", 1, 0), w)
    assert s.weights.w_time == 0.5
    assert s.weights.time_budget_seconds == 42.0


# --- T031: absent is not zero ---------------------------------------------------------


def test_scalar_is_withheld_when_there_is_no_conclusive_evidence() -> None:
    """FR-013: None, not 0.0. A zero would rank as a measured, terrible result."""
    s = score_row(_row("opencode", 0, 0))  # environment/inconclusive only
    assert s.credit_rate is None
    assert s.scalar is None


def test_a_withheld_scalar_is_not_ranked_as_zero() -> None:
    rows = [_row("a", 5, 0, per_run=[1.0]), _row("b", 0, 0)]
    r = rank_role("reviewer", rows)
    assert "b" in r.insufficient_evidence
    assert [s.backend for s in r.ordered] == ["a"]


# --- T032: the tie rule reads per-run scalars ----------------------------------------


def test_candidates_within_the_noise_band_are_tied() -> None:
    """FR-020: |Δmean| <= stdev_a + stdev_b, over the per-run scalars."""
    rows = [
        _row("a", 6, 4, per_run=[0.4, 0.8], runs=2),  # mean 0.6, spread 0.28
        _row("b", 5, 5, per_run=[0.3, 0.7], runs=2),  # mean 0.5, spread 0.28
    ]
    r = rank_role("reviewer", rows)

    assert r.verdict == "tie"
    assert any({"a", "b"} == set(g) for g in r.ties)


def test_ties_are_reported_pairwise_with_no_duplicate_or_overlapping_entries() -> None:
    """The tie relation is NOT transitive, so ties are reported as PAIRS.

    Adversarial review flagged that the original grouping emitted overlapping groups for
    three mutually-tied candidates (`[[c,b,a],[b,a]]`), and suggested merging into
    connected components. That fix would be wrong: measured directly, x~y and y~z can hold
    while x~z does not, so a merged group would assert an indistinguishability the data
    denies. Pairs say exactly what was measured and nothing more.
    """
    rows = [
        _row("c", 6, 4, per_run=[0.75, 0.85], runs=2),
        _row("b", 5, 5, per_run=[0.4, 0.7], runs=2),
        _row("a", 4, 6, per_run=[0.3, 0.7], runs=2),
    ]
    r = rank_role("reviewer", rows)

    # Every entry is a pair, no entry repeats, and no unordered pair appears twice.
    assert all(len(g) == 2 for g in r.ties), f"expected pairs, got {r.ties}"
    keys = [frozenset(g) for g in r.ties]
    assert len(keys) == len(set(keys)), f"duplicate tie pairs: {r.ties}"


def test_a_non_transitive_tie_chain_does_not_merge_into_one_group() -> None:
    """x~y and y~z but NOT x~z: the output must never imply x ties z."""
    rows = [
        _row("z", 5, 5, per_run=[0.22, 0.22], runs=2),   # mean 0.22, stdev 0
        _row("y", 5, 5, per_run=[0.0, 0.32], runs=2),    # mean 0.16, wide
        _row("x", 5, 5, per_run=[0.10, 0.10], runs=2),   # mean 0.10, stdev 0
    ]
    r = rank_role("reviewer", rows)

    pairs = {frozenset(g) for g in r.ties}
    assert frozenset({"x", "z"}) not in pairs, (
        "x and z do not tie when measured directly; reporting them together would "
        f"fabricate an equivalence. ties={r.ties}"
    )


def test_clearly_separated_candidates_are_ranked_not_tied() -> None:
    rows = [
        _row("good", 10, 0, per_run=[1.0, 1.0], runs=2),  # mean 1.0, stdev 0
        _row("bad", 0, 10, per_run=[0.0, 0.0], runs=2),   # mean 0.0, stdev 0
    ]
    r = rank_role("reviewer", rows)

    assert r.verdict == "ranked"
    assert [s.backend for s in r.ordered] == ["good", "bad"]


def test_single_run_is_flagged_so_an_unmeasured_band_is_not_read_as_a_tie() -> None:
    """FR-020: with one run the deviation is 0, so only exactly equal scalars tie. The
    flag tells the reader the band was never measured."""
    rows = [
        _row("a", 8, 2, per_run=[0.8], runs=1),
        _row("b", 5, 5, per_run=[0.5], runs=1),
    ]
    r = rank_role("reviewer", rows)

    assert r.single_run is True
    assert r.verdict == "ranked"  # 0.8 vs 0.5 with zero spread is not a tie


def test_the_tie_rule_uses_per_run_scalars_not_the_pooled_row() -> None:
    """Guards the F1 defect analysis caught: if the rule read the pooled value there
    would be a single number, no deviation, and the band would silently vanish."""
    wide = [_row("a", 6, 4, per_run=[0.1, 1.0], runs=2),
            _row("b", 5, 5, per_run=[0.0, 1.0], runs=2)]
    narrow = [_row("a", 6, 4, per_run=[0.6, 0.6], runs=2),
              _row("b", 5, 5, per_run=[0.5, 0.5], runs=2)]

    # Same pooled means either way; only the per-run spread differs.
    assert rank_role("reviewer", wide).verdict == "tie"
    assert rank_role("reviewer", narrow).verdict == "ranked"


# --- T033: insufficient evidence ------------------------------------------------------


def test_a_pair_below_the_conclusive_floor_is_insufficient() -> None:
    """FR-021: a rate over one or two dispatches is not distinguishable from noise."""
    rows = [_row("a", 1, 1, per_run=[0.5]), _row("b", 5, 0, per_run=[1.0])]
    r = rank_role("reviewer", rows, min_conclusive=3)

    assert "a" in r.insufficient_evidence
    assert r.verdict == "insufficient_evidence"  # only one rankable candidate left


def test_a_single_candidate_cannot_be_best() -> None:
    """FR-021: there is nothing for it to be better than."""
    r = rank_role("reviewer", [_row("a", 9, 1, per_run=[0.9])])
    assert r.verdict == "insufficient_evidence"


def test_the_floor_is_configurable() -> None:
    rows = [_row("a", 1, 1, per_run=[0.5]), _row("b", 2, 0, per_run=[1.0])]
    r = rank_role("reviewer", rows, min_conclusive=2)
    assert r.insufficient_evidence == []
    assert r.min_conclusive == 2


def test_default_floor_is_three() -> None:
    assert DEFAULT_MIN_CONCLUSIVE == 3


# --- T034: no viable harness ----------------------------------------------------------


def test_all_candidates_failing_yields_no_viable_harness() -> None:
    """FR-027: never crown the least-bad option when nothing worked."""
    rows = [
        _row("a", 0, 5, per_run=[0.0], runs=1),
        _row("b", 0, 9, per_run=[0.0], runs=1),
    ]
    r = rank_role("reviewer", rows)

    assert r.verdict == "no_viable_harness"


def test_one_working_candidate_prevents_the_no_viable_verdict() -> None:
    rows = [_row("a", 0, 5, per_run=[0.0]), _row("b", 4, 1, per_run=[0.8])]
    r = rank_role("reviewer", rows)
    assert r.verdict in {"ranked", "tie"}
    assert r.ordered[0].backend == "b"


# --- T035: evidence citation and advisory-only ----------------------------------------


def test_every_score_cites_its_evidence_row() -> None:
    """FR-022: a recommendation without its counts is not checkable."""
    r = rank_role("reviewer", [_row("a", 5, 1, per_run=[0.83]), _row("b", 3, 3, per_run=[0.5])])
    for s in r.ordered:
        assert s.evidence.conclusive == s.evidence.credit + s.evidence.harness_defect
        assert s.evidence.backend == s.backend


def test_report_writes_only_a_file_and_says_it_is_advisory(tmp_path) -> None:
    """FR-023: nothing here may modify live deployment configuration."""
    rollup = roll_up([simple_artifact([
        ("reviewer", "openclaw", "approved"),
        ("reviewer", "openclaw", "approved"),
        ("reviewer", "openclaw", "changes_requested"),
        ("reviewer", "junie", "malformed_output"),
        ("reviewer", "junie", "malformed_output"),
        ("reviewer", "junie", "system_error"),
    ])])
    report = rank(rollup)
    out = report.write(tmp_path)

    assert out.name == "harness_rank.json"
    assert list(tmp_path.iterdir()) == [out], "only the report may be written"
    assert "advisory only" in format_report(report)


def test_report_round_trips(tmp_path) -> None:
    rollup = roll_up([simple_artifact([("reviewer", "openclaw", "approved")])])
    report = rank(rollup)
    assert HarnessRankingReport.model_validate_json(report.to_validated_json()).view is (
        ScoringView.HARNESS_COMPARISON
    )


def test_rank_covers_every_role_in_the_rollup() -> None:
    """FR-019/SC-005: no role silently absent."""
    rollup = roll_up([simple_artifact([
        ("reviewer", "openclaw", "approved"),
        ("security", "codex", "security_passed"),
        ("qa", "claude_code", "qa_passed"),
    ])])
    report = rank(rollup)
    assert {r.role for r in report.rankings} == {"reviewer", "security", "qa"}


def test_empty_rollup_reports_nothing_to_rank() -> None:
    report = HarnessRankingReport(generated_at=__import__("datetime").datetime.now(
        __import__("datetime").UTC))
    assert "nothing to rank" in format_report(report)

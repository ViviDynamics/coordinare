"""Spec 137 — the optimizer search loop, proven against synthetic objectives
with a planted optimum (the stub substrate is signal-free — 136 finding), plus
artifact invariants and failure isolation.

Contract: specs/137-board-bench-optimizer/contracts/optimizer-artifact.md
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from coordinare.bench.optimizer import (
    OPTIMIZER_SCHEMA_VERSION,
    BudgetSpent,
    EvalOutcome,
    EvaluationRecord,
    OptimizerArtifact,
    Recommendation,
    run_optimizer,
)
from coordinare.bench.space import load_space
from tests.unit.test_136_space import BASELINE, SPACE

# The planted optimum over the test space (3 dimensions, 2 choices each).
TARGET = {
    "symphonies.bench.persona_scope.ci_gate.enabled": True,
    "global_config.performers.implementer.mode": "single-premium",
    "global_config.max_concurrent_cards": 2,
}


def _loaded(tmp_path: Path):
    (tmp_path / "baseline.yaml").write_text(yaml.safe_dump(BASELINE))
    path = tmp_path / "space.yaml"
    path.write_text(yaml.safe_dump(SPACE))
    return load_space(path)


def _synthetic(target: dict[str, Any] = TARGET, fail_on: set[str] | None = None):
    """Score = fraction of target coordinates matched; optionally fail points."""

    async def evaluate(eval_id: int, overrides: dict[str, Any], config) -> EvalOutcome:
        if fail_on and any(str(v) in fail_on for v in overrides.values()):
            return EvalOutcome(failed=True, error="synthetic failure")
        matched = sum(1 for k, v in target.items() if overrides.get(k) == v)
        return EvalOutcome(
            mean_scalar=matched / len(target),
            mean_components={"correctness_rate": matched / len(target)},
        )

    return evaluate


async def _run(tmp_path, *, seed=1, max_evaluations=30, evaluate=None, **kw):
    loaded = _loaded(tmp_path)
    return await run_optimizer(
        loaded,
        evaluate or _synthetic(),
        seed=seed,
        max_evaluations=max_evaluations,
        evaluator_kind="synthetic",
        **kw,
    )


# SC-001 runs over the SHIPPED default space (2^9 = 512 grid combinations);
# the planted optimum is reachable by single-dimension improvements.
DEFAULT_SPACE_TARGET = {
    "symphonies.bench.persona_scope.ci_gate.enabled": True,
    "symphonies.bench.persona_scope.local_test_gate.enabled": True,
    "symphonies.bench.persona_scope.baseline_prevention_gate.enabled": True,
    "symphonies.bench.persona_scope.baseline_classification_gate.enabled": True,
    "symphonies.bench.persona_scope.inherited_repair_gate.enabled": True,
    "symphonies.bench.persona_scope.env_blocked_gate.enabled": True,
    "global_config.max_concurrent_cards": 2,
    "global_config.performers.implementer.mode": "single-premium",
    "global_config.performers.reviewer.mode": "single-premium",
}


class TestConvergence:
    @pytest.mark.parametrize("seed", [1, 2, 3])
    async def test_finds_planted_optimum_within_half_the_default_space(self, seed: int) -> None:
        # SC-001: shipped default space (512 combos), budget = half = 256.
        loaded = load_space(Path("benchmarks/spaces/default.yaml"))
        artifact = await run_optimizer(
            loaded,
            _synthetic(DEFAULT_SPACE_TARGET),
            seed=seed,
            max_evaluations=256,
            evaluator_kind="synthetic",
        )
        assert artifact.space_total_combinations == 512
        assert artifact.budget.charged_evaluations <= 256
        assert artifact.recommendation is not None
        assert artifact.recommendation.mean_scalar == 1.0
        rec = artifact.recommendation.overrides
        assert all(rec.get(k) == v for k, v in DEFAULT_SPACE_TARGET.items())
        # best-so-far is monotonically non-decreasing
        best = [r.best_so_far for r in artifact.trace if r.best_so_far is not None]
        assert best == sorted(best)

    @pytest.mark.parametrize("seed", [1, 2, 3])
    async def test_finds_planted_optimum_on_small_space(self, tmp_path: Path, seed: int) -> None:
        artifact = await _run(tmp_path, seed=seed, max_evaluations=8)
        assert artifact.recommendation is not None
        assert artifact.recommendation.mean_scalar == 1.0
        rec = artifact.recommendation.overrides
        assert all(rec.get(k) == v for k, v in TARGET.items())

    async def test_budget_cap_is_hard_and_stop_reason_says_budget(self, tmp_path: Path) -> None:
        artifact = await _run(tmp_path, max_evaluations=3)
        assert artifact.budget.charged_evaluations <= 3
        assert artifact.budget.stop_reason == "budget_evaluations"

    async def test_wall_clock_cap_stops_search(self, tmp_path: Path) -> None:
        ticks = iter(range(0, 10_000, 100))  # each clock() call advances 100s

        artifact = await _run(
            tmp_path, max_evaluations=100, max_seconds=150.0, clock=lambda: float(next(ticks)),
        )
        assert artifact.budget.stop_reason == "budget_wall_clock"
        assert artifact.budget.charged_evaluations < 100
        # the overshoot note is truthful: cap exceeded because in-flight completed
        assert any("wall-clock cap exceeded" in n for n in artifact.notes)

    async def test_budget_shortfall_on_comparison_set_is_stated(self, tmp_path: Path) -> None:
        # budget 1 = baseline only; the candidate is cut off and must be named
        artifact = await _run(tmp_path, max_evaluations=1)
        assert artifact.budget.charged_evaluations == 1
        labels = (
            [row.label for row in artifact.recommendation.comparison]
            if artifact.recommendation else []
        )
        assert "candidate:aggressive" not in labels
        assert any(
            "shortfall" in n and "candidate:aggressive" in n for n in artifact.notes
        ), artifact.notes

    async def test_space_exhaustion_stops_early_with_full_coverage(self, tmp_path: Path) -> None:
        artifact = await _run(tmp_path, max_evaluations=1000)
        assert artifact.budget.stop_reason == "space_exhausted"
        assert artifact.distinct_evaluated >= artifact.space_total_combinations
        assert artifact.space_total_combinations == 8

    async def test_seed_determinism(self, tmp_path: Path) -> None:
        a = await _run(tmp_path, seed=7)
        b = await _run(tmp_path, seed=7)
        assert [r.model_dump() for r in a.trace] == [r.model_dump() for r in b.trace]
        assert a.recommendation == b.recommendation

    async def test_different_seeds_may_differ_but_both_converge(self, tmp_path: Path) -> None:
        a = await _run(tmp_path, seed=1, max_evaluations=1000)
        b = await _run(tmp_path, seed=2, max_evaluations=1000)
        assert a.recommendation is not None and b.recommendation is not None
        assert a.recommendation.fingerprint == b.recommendation.fingerprint  # same optimum

    async def test_no_fingerprint_evaluated_twice_and_cache_hits_uncharged(self, tmp_path: Path) -> None:
        artifact = await _run(tmp_path, max_evaluations=1000)
        charged = [r for r in artifact.trace if not r.cache_hit]
        fingerprints = [r.fingerprint for r in charged]
        assert len(fingerprints) == len(set(fingerprints))
        assert len(artifact.trace) == artifact.budget.charged_evaluations + artifact.budget.cache_hits


class TestFailureAndTies:
    async def test_failed_evaluations_recorded_search_continues(self, tmp_path: Path) -> None:
        artifact = await _run(
            tmp_path, max_evaluations=1000, evaluate=_synthetic(fail_on={"single-premium"}),
        )
        failed = [r for r in artifact.trace if r.failed]
        assert failed and all("synthetic failure" in r.error for r in failed)
        assert artifact.recommendation is not None
        assert artifact.recommendation.overrides.get(
            "global_config.performers.implementer.mode",
        ) != "single-premium"

    async def test_all_failed_yields_no_recommendation(self, tmp_path: Path) -> None:
        async def always_fail(eval_id, overrides, config) -> EvalOutcome:
            return EvalOutcome(failed=True, error="boom")

        artifact = await _run(tmp_path, max_evaluations=5, evaluate=always_fail)
        assert artifact.recommendation is None
        assert any("no recommendation" in n.lower() for n in artifact.notes)

    async def test_tie_breaks_to_earliest_with_note(self, tmp_path: Path) -> None:
        async def constant(eval_id, overrides, config) -> EvalOutcome:
            return EvalOutcome(mean_scalar=0.5)

        artifact = await _run(tmp_path, max_evaluations=6, evaluate=constant)
        assert artifact.recommendation is not None
        first_charged = next(r for r in artifact.trace if not r.cache_hit and not r.failed)
        assert artifact.recommendation.eval_id == first_charged.eval_id
        assert artifact.recommendation.tie_note


class TestComparisonAndArtifact:
    async def test_baseline_and_candidates_in_comparison_with_dup_flag(self, tmp_path: Path) -> None:
        artifact = await _run(tmp_path, max_evaluations=1000)
        labels = [row.label for row in artifact.recommendation.comparison]
        assert "baseline" in labels
        assert "candidate:aggressive" in labels
        rec_fp = artifact.recommendation.fingerprint
        for row in artifact.recommendation.comparison:
            assert row.duplicate_of_recommendation == (row.fingerprint == rec_fp)

    async def test_artifact_round_trips_and_renders(self, tmp_path: Path) -> None:
        artifact = await _run(tmp_path, max_evaluations=8, session_dir=tmp_path / "session")
        path = artifact.write(tmp_path / "session")
        assert path == tmp_path / "session" / "optimizer.json"
        assert OptimizerArtifact.load(path) == artifact
        md = (tmp_path / "session" / "optimizer-report.md").read_text()
        assert "Recommendation" in md and "baseline" in md
        assert str(artifact.budget.charged_evaluations) in md
        assert artifact.schema_version == OPTIMIZER_SCHEMA_VERSION == 1

    def test_reconciliation_invariants_refuse_to_write(self, tmp_path: Path) -> None:
        artifact = OptimizerArtifact(
            space_name="s",
            seed=1,
            evaluator_kind="synthetic",
            repeats=1,
            repeats_source="default",
            space_total_combinations=8,
            distinct_evaluated=1,
            trace=[
                EvaluationRecord(
                    eval_id=1, provenance="baseline", overrides={}, fingerprint="f" * 16,
                    mean_scalar=0.5, best_so_far=0.5,
                ),
            ],
            recommendation=Recommendation(
                eval_id=1, overrides={}, fingerprint="f" * 16, mean_scalar=0.5,
            ),
            budget=BudgetSpent(
                max_evaluations=10, charged_evaluations=5, cache_hits=0,  # lies: trace has 1
                max_seconds=60.0, elapsed_seconds=1.0, stop_reason="space_exhausted",
            ),
        )
        with pytest.raises(ValueError, match="reconcile"):
            artifact.write(tmp_path)

    async def test_repeats_and_source_recorded(self, tmp_path: Path) -> None:
        artifact = await _run(
            tmp_path, max_evaluations=4, repeats=3, repeats_source="noise_report",
            noise_report_ref="runs/x/noise-report.json",
        )
        assert artifact.repeats == 3
        assert artifact.repeats_source == "noise_report"
        assert artifact.noise_report_ref == "runs/x/noise-report.json"

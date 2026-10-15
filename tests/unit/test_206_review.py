"""Spec 206 review: a legacy flat-rate cost is never a price, and a mixed set of repeats is
never averaged over only its priced part.

Each block pins one review finding. A numeric cost with no pricing marker is a flat-rate
figure written before spec 206, so it loads as unknown. Repeats with an unknown cost withhold
their mean while the cost term carries weight, and an all-unpriced optimizer run names the
reason instead of saying only "unrankable".
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from coordinare.bench.artifact import LEGACY_REASON, RunArtifact
from coordinare.bench.fixtures import tiny_fixture
from coordinare.bench.grader import score_run
from coordinare.bench.noise import aggregate
from coordinare.bench.optimizer import EvalOutcome, real_evaluator, run_optimizer
from coordinare.bench.score import (
    ComponentVector,
    ScoreObject,
    Weights,
    compute_scalar,
    cost_is_known,
    describe_scalar,
    repeat_mean,
    scalar_withheld_reason,
)
from coordinare.bench.sweep import AblationDelta, PointResult, compute_deltas, run_sweep
from coordinare.config import CoordinareConfiguration
from tests.unit.test_136_sweep import _loaded as _sweep_loaded
from tests.unit.test_137_optimizer import _loaded as _optimizer_loaded

REASON = "token split by kind not reported"
NOW = datetime(2026, 10, 14, tzinfo=UTC)
REPO = Path(__file__).resolve().parents[2]


def _score(cost: float | None, *, reason: str | None = None, w_cost: float = 0.1) -> ScoreObject:
    weights = Weights(w_cost=w_cost)
    components = ComponentVector(
        correctness_rate=1.0, cost_usd=cost, cost_priced=True,
        cost_unknown_reason=reason, wall_clock_seconds=60.0,
    )
    scalar, _ = compute_scalar(components, weights)
    return ScoreObject(run_id="r", artifact_schema_version=1, scored_at=NOW,
                       weights=weights, components=components, scalar=scalar)


def _legacy_run_dict(fixture_id: str) -> dict[str, object]:
    """A run.json written before spec 206: a numeric card and run cost, no pricing marker."""
    return {
        "schema_version": 1, "run_id": "old",
        "started_at": "2026-09-09T00:00:00Z", "finished_at": "2026-09-09T00:01:00Z",
        "wall_clock_seconds": 60.0,
        "config_fingerprint": {"hash": "h", "source_path": "p"},
        "cards": [{
            "card_id": "c1", "fixture_id": fixture_id, "final_state": "merged",
            "dispatches": [],
            "cost": {"tokens_processed": 1000, "cost_usd": 3.0, "cost_estimated": True},
        }],
        "totals": {"tokens_processed": 1000, "cost_usd": 3.0, "cost_estimated": True,
                   "cards_total": 1, "cards_merged": 1},
    }


# --- BLOCK: a legacy flat-rate cost is not a known price ---------------------------------


def test_legacy_run_rescored_at_a_weighted_cost_is_unknown_not_a_price() -> None:
    fixture = tiny_fixture()
    run = RunArtifact.model_validate(_legacy_run_dict(fixture.id))

    score = score_run(run, [fixture], weights=Weights(w_cost=0.1))

    assert run.totals.cost_usd is None
    assert score.components.cost_usd is None
    assert cost_is_known(score.components) is False
    assert score.components.cost_unknown_reason == LEGACY_REASON
    assert score.scalar is None  # the penalty cannot be applied to a flat-rate figure


def test_committed_161_evidence_loads_with_its_cost_unknown() -> None:
    """The 161 evidence was written with a flat rate and no basis. It must load as unknown:
    a cost of 0.0 there is not a measured free run."""
    evidence = REPO / "specs" / "161-board-bench-harness" / "evidence"
    runs = sorted(evidence.rglob("run.json"))
    scores = sorted(evidence.rglob("score.json"))
    assert runs and scores, "the evidence files this test pins must exist"

    for path in runs:
        loaded = RunArtifact.load(path)
        assert loaded.totals.cost_usd is None, path
        assert all(c.cost.cost_usd is None for c in loaded.cards), path

    for path in scores:
        loaded_score = ScoreObject.load(path)
        assert cost_is_known(loaded_score.components) is False, path
        assert loaded_score.components.cost_usd is None, path


# --- BLOCK, mixed repeats: never averaged over the priced part only -----------------------


def test_one_unpriced_repeat_withholds_the_mean_of_the_priced_ones() -> None:
    priced = _score(0.5)
    unpriced = _score(None, reason=REASON)

    value, reason = repeat_mean([priced, unpriced])

    assert value is None
    assert reason == REASON


def test_all_priced_repeats_still_average() -> None:
    value, reason = repeat_mean([_score(0.5), _score(0.5)])

    assert value is not None
    assert reason is None


def test_an_unpriced_repeat_does_not_withhold_when_cost_has_no_weight() -> None:
    value, reason = repeat_mean([_score(0.5, w_cost=0.0), _score(None, reason=REASON, w_cost=0.0)])

    assert value is not None
    assert reason is None


def test_scalar_withheld_reason_names_the_first_unpriced_repeat() -> None:
    assert scalar_withheld_reason([_score(0.5), _score(None, reason=REASON)]) == REASON
    assert scalar_withheld_reason([_score(0.5), _score(0.4)]) is None


def test_noise_report_withholds_the_scalar_stats_for_mixed_repeats() -> None:
    report = aggregate(
        [("h", _score(0.5), "1"), ("h", _score(None, reason=REASON), "2")],
        requested_repeats=2, failures=[],
    )

    assert report.scalar_stats is None
    assert report.scalar_withheld_reason == REASON


def test_describe_scalar_names_the_reason_for_a_withheld_scalar() -> None:
    assert describe_scalar(_score(None, reason=REASON)) == f"unrankable: {REASON}"
    assert describe_scalar(_score(0.5)).startswith("0.")


# --- optimizer: an all-unpriced run names the cost reason --------------------------------


@pytest.mark.asyncio
async def test_all_unpriced_optimizer_run_names_the_cost_reason(tmp_path: Path) -> None:
    loaded = _optimizer_loaded(tmp_path)

    async def unpriced(eval_id: int, overrides: dict[str, Any], config: CoordinareConfiguration) -> EvalOutcome:
        return EvalOutcome(repeats=1, mean_scalar=None, cost_unknown_reason=REASON,
                           mean_components={"correctness_rate": 1.0})

    artifact = await run_optimizer(loaded, unpriced, max_evaluations=5,
                                   evaluator_kind="synthetic", session_dir=tmp_path / "s")

    assert artifact.recommendation is None
    assert any("no point could be priced" in n and REASON in n for n in artifact.notes)


# --- sweep: deltas report an unpriced baseline ------------------------------------------


def test_compute_deltas_reports_an_unpriced_baseline_instead_of_nothing() -> None:
    baseline = PointResult(point_id="baseline", kind="baseline", mean_scalar=None,
                           cost_unknown_reason=REASON)
    point = PointResult(point_id="concurrency=2", kind="ablation", dimension="concurrency",
                        value=2, mean_scalar=0.9)

    deltas = compute_deltas(baseline, [point])

    assert len(deltas) == 1
    assert deltas[0].scalar_delta is None
    assert deltas[0].note == f"baseline unpriced: {REASON}"
    assert isinstance(deltas[0], AblationDelta)


# --- end to end at the default weights: every stub point is reported unpriced -------------


@pytest.mark.asyncio
async def test_default_weights_sweep_prices_every_stub_point_at_zero_and_ranks_it(tmp_path: Path) -> None:
    """Spec 242: a stub makes no model calls, so each point is priced at zero and ranks."""
    loaded = _sweep_loaded(tmp_path)

    artifact = await run_sweep(loaded, "ablation", tmp_path / "session")

    assert artifact.points
    assert all(p.mean_scalar is not None for p in artifact.points)
    assert all(p.cost_unknown_reason is None for p in artifact.points)


@pytest.mark.asyncio
async def test_default_weights_optimizer_recommends_a_stub_run_priced_at_zero(tmp_path: Path) -> None:
    """Spec 242: a stub evaluation is priced at zero, so the default-weight optimizer ranks and
    recommends instead of naming an unpriced cost as the reason it could not."""
    loaded = _optimizer_loaded(tmp_path)
    evaluate = real_evaluator(loaded, [tiny_fixture()], tmp_path / "eval")

    artifact = await run_optimizer(loaded, evaluate, max_evaluations=3,
                                   session_dir=tmp_path / "opt")

    assert artifact.recommendation is not None
    assert not any("no point could be priced" in n for n in artifact.notes)


@pytest.mark.asyncio
async def test_an_unpriced_sweep_drops_its_points_and_names_the_unpriced_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec 242 restores the end-to-end unknown path. A stub is now priced at zero, so the path
    is driven here by pricing every dispatch unknown: each point is dropped as unpriced, and the
    deltas say the baseline is unpriced."""
    from coordinare.bench import runner
    from coordinare.bench.pricing import TokenCost

    monkeypatch.setattr(
        runner, "price_dispatch",
        lambda *_args, **_kwargs: TokenCost(None, "transcripts disabled: test", None),
    )
    loaded = _sweep_loaded(tmp_path)

    artifact = await run_sweep(loaded, "ablation", tmp_path / "session")

    assert artifact.points
    assert artifact.coverage.dropped
    assert all(d.reason.startswith("unpriced: ") for d in artifact.coverage.dropped)
    assert artifact.deltas
    assert all(d.note.startswith("baseline unpriced") for d in artifact.deltas)

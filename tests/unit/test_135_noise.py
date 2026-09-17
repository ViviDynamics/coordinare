"""Spec 135 — noise aggregation: sample statistics, failed repeats, agreement.

Contract: specs/135-board-bench-scoring/contracts/noise-report.md
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from coordinare.bench.noise import (
    NOISE_SCHEMA_VERSION,
    NoiseError,
    NoiseReport,
    RepeatFailure,
    aggregate,
    component_stats,
)
from coordinare.bench.score import CardVerdict, ComponentVector, ScoreObject

NOW = datetime(2026, 8, 23, tzinfo=UTC)


def _score(
    correctness: float | None = 1.0,
    cost: float | None = None,
    scalar: float | None = 0.99,
    category: str = "PASS",
) -> ScoreObject:
    return ScoreObject(
        run_id="r",
        artifact_schema_version=1,
        scored_at=NOW,
        cards=[
            CardVerdict(
                card_id="PVTI_1",
                fixture_id="fx",
                category=category,  # type: ignore[arg-type]
                correct=category == "PASS",
            ),
        ],
        components=ComponentVector(
            correctness_rate=correctness, cost_usd=cost, wall_clock_seconds=60.0,
        ),
        scalar=scalar,
    )


class TestComponentStats:
    def test_sample_variance_uses_n_minus_one(self) -> None:
        s = component_stats([1.0, 2.0, 3.0])
        assert s is not None
        assert s.mean == pytest.approx(2.0)
        assert s.variance == pytest.approx(1.0)  # sample variance, not 2/3
        assert s.stdev == pytest.approx(1.0)
        assert (s.min, s.max, s.n) == (1.0, 3.0, 3)
        assert s.single_sample is False

    def test_single_sample_flags_and_zeroes_variance(self) -> None:
        s = component_stats([5.0])
        assert s is not None
        assert s.variance == 0.0 and s.stdev == 0.0 and s.single_sample is True

    def test_no_values_returns_none(self) -> None:
        assert component_stats([]) is None


class TestAggregate:
    def test_aggregates_components_scalar_and_agreement(self) -> None:
        runs = [("h", _score(scalar=0.9), "1/score.json"), ("h", _score(scalar=1.1), "2/score.json")]
        report = aggregate(runs, requested_repeats=2, failures=[])
        assert report.schema_version == NOISE_SCHEMA_VERSION
        assert report.effective_repeats == 2 and report.requested_repeats == 2
        assert report.scalar_stats is not None
        assert report.scalar_stats.mean == pytest.approx(1.0)
        assert report.component_stats["correctness_rate"].n == 2
        assert report.card_agreement == {"fx": 1.0}
        assert report.score_refs == ["1/score.json", "2/score.json"]

    def test_none_components_count_only_present_values(self) -> None:
        runs = [("h", _score(cost=0.5), "1"), ("h", _score(cost=None), "2")]
        report = aggregate(runs, requested_repeats=2, failures=[])
        assert report.component_stats["cost_usd"].n == 1
        assert "cost_usd" in report.component_stats

    def test_failed_repeats_reported_not_dropped(self) -> None:
        runs = [("h", _score(), "1")]
        failures = [RepeatFailure(index=2, error="boom")]
        report = aggregate(runs, requested_repeats=2, failures=failures)
        assert report.requested_repeats == 2 and report.effective_repeats == 1
        assert report.failures == failures
        assert report.scalar_stats is not None and report.scalar_stats.single_sample is True

    def test_config_fingerprint_mismatch_aborts(self) -> None:
        runs = [("h1", _score(), "1"), ("h2", _score(), "2")]
        with pytest.raises(NoiseError, match="fingerprint"):
            aggregate(runs, requested_repeats=2, failures=[])

    def test_card_agreement_is_modal_fraction(self) -> None:
        runs = [
            ("h", _score(category="PASS"), "1"),
            ("h", _score(category="PASS"), "2"),
            ("h", _score(category="FAIL_MODEL"), "3"),
        ]
        report = aggregate(runs, requested_repeats=3, failures=[])
        assert report.card_agreement["fx"] == pytest.approx(2 / 3)

    def test_report_round_trips_and_writes_markdown(self, tmp_path) -> None:
        report = aggregate([("h", _score(), "1")], requested_repeats=1, failures=[])
        path = report.write(tmp_path)
        assert path == tmp_path / "noise-report.json"
        assert NoiseReport.load(path).effective_repeats == 1
        md = (tmp_path / "noise-report.md").read_text()
        assert "correctness_rate" in md and "effective" in md.lower()

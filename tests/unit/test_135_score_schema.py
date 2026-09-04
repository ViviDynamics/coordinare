"""Spec 135 — score-object schema: round-trip, weights embedded, versioning.

Contract: specs/135-board-bench-scoring/contracts/score-object.md
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from coordinare.bench.score import (
    KNOWN_ARTIFACT_VERSIONS,
    SCORE_SCHEMA_VERSION,
    CardVerdict,
    ComponentVector,
    ScoreObject,
    Weights,
    compute_scalar,
)


def _score(**kw) -> ScoreObject:
    base = dict(
        run_id="20260823-000000",
        artifact_schema_version=1,
        scored_at=datetime(2026, 8, 23, tzinfo=UTC),
    )
    base.update(kw)
    return ScoreObject(**base)


class TestScoreSchema:
    def test_round_trips_through_its_own_schema(self) -> None:
        s = _score(
            cards=[CardVerdict(card_id="PVTI_1", fixture_id="fx", category="PASS", correct=True)],
            components=ComponentVector(correctness_rate=1.0, wall_clock_seconds=2.5),
            scalar=0.9995,
        )
        text = s.to_validated_json()
        again = ScoreObject.model_validate_json(text)
        assert again == s

    def test_write_places_score_json_next_to_run_json(self, tmp_path) -> None:
        (tmp_path / "run.json").write_text("{}")
        path = _score().write(tmp_path)
        assert path == tmp_path / "score.json"
        assert ScoreObject.load(path).run_id == "20260823-000000"

    def test_weights_are_embedded_with_defaults(self) -> None:
        s = _score()
        assert s.weights == Weights(
            w_correctness=1.0, w_cost=0.1, w_time=0.1, cost_budget_usd=1.0, time_budget_seconds=600.0
        )
        assert '"w_correctness"' in s.to_validated_json()

    def test_schema_version_is_current_and_artifact_versions_known(self) -> None:
        """The score schema version is pinned so a bump is always a deliberate act.

        Moved 1 → 2 by spec 161, which adds the `view` discriminator to ScoreObject.
        Adding a field changes the serialized document, so the version moves with it.
        The change is backward-compatible: `view` defaults to CONFIG_COMPARISON, so a
        v1 document loads and keeps exactly the meaning it always had (pinned by
        tests/unit/test_161_score_regression.py).

        KNOWN_ARTIFACT_VERSIONS is unchanged — the RUN-artifact schema did not move.
        """
        assert _score().schema_version == SCORE_SCHEMA_VERSION == 2
        assert frozenset({1}) == KNOWN_ARTIFACT_VERSIONS

    def test_rejects_bad_category(self) -> None:
        with pytest.raises(ValueError, match="category"):
            CardVerdict(card_id="c", category="MAYBE")  # type: ignore[arg-type]


class TestScalar:
    def test_formula_matches_clarifications(self) -> None:
        comps = ComponentVector(correctness_rate=1.0, cost_usd=0.5, wall_clock_seconds=60.0)
        scalar, cost_missing = compute_scalar(comps, Weights())
        # 1.0*1.0 - 0.1*(0.5/1.0) - 0.1*(60/600) = 1.0 - 0.05 - 0.01
        assert scalar == pytest.approx(0.94)
        assert cost_missing is False

    def test_unknown_cost_omits_term_and_flags(self) -> None:
        comps = ComponentVector(correctness_rate=1.0, cost_usd=None, wall_clock_seconds=60.0)
        scalar, cost_missing = compute_scalar(comps, Weights())
        assert scalar == pytest.approx(0.99)  # no cost penalty, never treated as zero-cost silently
        assert cost_missing is True

    def test_zero_gradeable_cards_yields_no_scalar(self) -> None:
        scalar, _ = compute_scalar(ComponentVector(correctness_rate=None), Weights())
        assert scalar is None

    @pytest.mark.parametrize("field", ["cost_budget_usd", "time_budget_seconds"])
    def test_zero_budget_rejected_at_construction_not_mid_scoring(self, field: str) -> None:
        with pytest.raises(ValueError, match="greater than 0"):
            Weights(**{field: 0.0})

    def test_negative_weight_rejected(self) -> None:
        with pytest.raises(ValueError, match="greater than or equal to 0"):
            Weights(w_cost=-1.0)

"""Spec 161 — FR-011: the harness view is ADDITIVE. The config view is unchanged.

T029 (US3): config-comparison scalar and component VALUES are numerically unchanged for a
fixed-harness sweep. Asserted on values, deliberately NOT on serialized bytes: verified
during analysis that adding any field changes ScoreObject's JSON document, so a
byte-identity assertion would be unsatisfiable rather than protective.

T029a (US3): the additive-change policy — SCORE_SCHEMA_VERSION bumped, the view
discriminator defaults to CONFIG_COMPARISON, and a document written without a view still
loads and reads as config-comparison.

Why this file exists: spec-135 FR-005 deliberately quarantines harness failure out of
correctness so config optimization is never steered by infrastructure noise. Spec 161
introduces the OPPOSITE treatment for harness comparison. If the two ever collapse into
one objective, every historical config scalar silently changes meaning. These tests are
the guard.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from coordinare.bench.artifact import (
    CardOutcome,
    CIResult,
    ConfigFingerprint,
    Merge,
    PersonaDispatch,
    RunArtifact,
)
from coordinare.bench.fixtures import Fixture
from coordinare.bench.grader import score_run
from coordinare.bench.score import (
    SCORE_SCHEMA_VERSION,
    ComponentVector,
    ScoreObject,
    ScoringView,
    Weights,
    compute_scalar,
)

NOW = datetime(2026, 9, 4, tzinfo=UTC)


def _fixture(fx_id: str) -> Fixture:
    """Mirrors tests/unit/test_135_grader.py's helper, including base_files."""
    return Fixture(
        id=fx_id, title="t", body="b", base_files={"a.py": ""},
        ground_truth="t", expected_final_state="merged",
    )


def _passing_card(card_id: str, fx_id: str) -> CardOutcome:
    """A card the grader will score CORRECT: merged AND gated by green CI.

    Both are required by grader._deterministic for an expected-merge fixture — merging
    without recorded CI evidence is not proof against the acceptance tests.
    """
    return CardOutcome(
        card_id=card_id, fixture_id=fx_id, final_state="merged",
        dispatches=[PersonaDispatch(stage="implementing", role="implementer",
                                    backend="codex", terminal_marker="pr_opened")],
        ci_results=[CIResult(head_sha="abc", conclusion="success")],
        merge=Merge(merged=True),
    )


# --- T029: the objective's VALUES are unchanged ---------------------------------------


def test_compute_scalar_formula_is_unchanged() -> None:
    """The documented config-comparison objective, pinned by hand-computed arithmetic.

    correctness - w_cost*(cost/cost_budget) - w_time*(time/time_budget)
      = 1.0*0.8 - 0.1*(0.5/1.0) - 0.1*(300/600)
      = 0.8 - 0.05 - 0.05 = 0.70
    """
    components = ComponentVector(
        correctness_rate=0.8, cost_usd=0.5, wall_clock_seconds=300.0,
    )
    scalar, cost_missing = compute_scalar(components, Weights())

    assert scalar == pytest.approx(0.70)
    assert cost_missing is False


def test_unknown_cost_still_omits_the_cost_term() -> None:
    """spec-135 behaviour: unknown cost is never treated as free."""
    components = ComponentVector(
        correctness_rate=1.0, cost_usd=None, wall_clock_seconds=600.0,
    )
    scalar, cost_missing = compute_scalar(components, Weights())

    # 1.0 - (cost omitted) - 0.1*(600/600) = 0.9
    assert scalar == pytest.approx(0.9)
    assert cost_missing is True


def test_zero_gradeable_cards_still_has_no_scalar() -> None:
    """spec-135 behaviour: unrankable, not zero."""
    scalar, _ = compute_scalar(ComponentVector(correctness_rate=None), Weights())
    assert scalar is None


def test_harness_failure_is_still_quarantined_from_correctness() -> None:
    """spec-135 FR-005, the invariant spec 161 must NOT disturb.

    A card that failed as harness/infrastructure is excluded from the correctness
    denominator and surfaced separately. If spec 161 had changed the shared scorer instead
    of adding a view, this would flip and every historical scalar would change meaning.
    """
    fixtures = [_fixture("fx-ok"), _fixture("fx-broken")]
    artifact = RunArtifact(
        run_id="r1", started_at=NOW, finished_at=NOW, wall_clock_seconds=1.0,
        config_fingerprint=ConfigFingerprint(hash="h", source_path="c.yaml"),
        cards=[
            _passing_card("c1", "fx-ok"),
            # final_state "error" with no dispatches == the card-level harness failure
            CardOutcome(card_id="c2", fixture_id="fx-broken", final_state="error",
                        dispatches=[]),
        ],
    )

    score = score_run(artifact, fixtures)

    categories = {v.card_id: v.category for v in score.cards}
    assert categories["c2"] == "FAIL_HARNESS"
    assert score.components.harness_failure_rate == pytest.approx(0.5)
    # The harness-failed card is NOT in the correctness denominator: 1 of 1 gradeable.
    assert score.components.correctness_rate == pytest.approx(1.0)


def test_the_config_view_is_what_the_existing_grader_emits() -> None:
    """FR-011/FR-012: a fixed-harness sweep's score is still a config-comparison score."""
    fixtures = [_fixture("fx")]
    artifact = RunArtifact(
        run_id="r", started_at=NOW, finished_at=NOW, wall_clock_seconds=1.0,
        config_fingerprint=ConfigFingerprint(hash="h", source_path="c.yaml"),
        cards=[_passing_card("c1", "fx")],
    )

    assert score_run(artifact, fixtures).view is ScoringView.CONFIG_COMPARISON


# --- T029a: the additive-change policy ------------------------------------------------


def test_score_schema_version_was_bumped() -> None:
    """Adding a field changes the document, so the version must move with it."""
    assert SCORE_SCHEMA_VERSION >= 2


def test_the_view_defaults_to_config_comparison() -> None:
    s = ScoreObject(run_id="r", artifact_schema_version=1, scored_at=NOW)
    assert s.view is ScoringView.CONFIG_COMPARISON


def test_a_document_without_a_view_still_loads_as_config_comparison() -> None:
    """Backward compatibility: every score written before spec 161 keeps its meaning."""
    legacy = (
        '{"schema_version": 1, "run_id": "old-run", "artifact_schema_version": 1, '
        '"scored_at": "2026-01-01T00:00:00Z", "scalar": 0.5}'
    )
    loaded = ScoreObject.model_validate_json(legacy)

    assert loaded.view is ScoringView.CONFIG_COMPARISON
    assert loaded.scalar == 0.5


def test_a_legacy_documents_scalar_is_not_reinterpreted() -> None:
    """The value guarantee: loading an old document must not recompute or adjust it."""
    legacy = (
        '{"schema_version": 1, "run_id": "old", "artifact_schema_version": 1, '
        '"scored_at": "2026-01-01T00:00:00Z", "scalar": 0.7, '
        '"components": {"correctness_rate": 0.8, "wall_clock_seconds": 300.0, '
        '"cost_usd": 0.5}}'
    )
    loaded = ScoreObject.model_validate_json(legacy)

    assert loaded.scalar == pytest.approx(0.7)
    assert loaded.components.correctness_rate == pytest.approx(0.8)


def test_the_two_views_are_never_silently_interchangeable() -> None:
    """FR-012: comparing across views is a category error, so they must differ."""
    from coordinare.bench.harness_rank import score_row
    from coordinare.bench.harness_rollup import RoleHarnessRow

    harness = score_row(RoleHarnessRow(role="reviewer", backend="openclaw",
                                       dispatches=4, credit=4))
    config = ScoreObject(run_id="r", artifact_schema_version=1, scored_at=NOW)

    assert harness.view is not config.view

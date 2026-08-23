"""Spec 135 — whole-lifecycle grader: deterministic truth table + 077 agree-to-PASS.

T005 (US1): deterministic grading against planted ground truth.
T006 (US2): agree-to-PASS composition, deterministic-only labeling, judge-error fallback.
T016: FR-010 backward compatibility of the Fixture extension.
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
from coordinare.bench.grader import GradingError, score_run
from coordinare.bench.score import Weights

NOW = datetime(2026, 8, 23, tzinfo=UTC)


def _fixture(fx_id: str = "fx", expected: str = "merged", ground_truth: str = "truth") -> Fixture:
    return Fixture(
        id=fx_id,
        title="t",
        body="b",
        base_files={"a.py": ""},
        ground_truth=ground_truth,
        expected_final_state=expected,  # type: ignore[arg-type]
    )


def _card(
    fx_id: str = "fx",
    final_state: str = "merged",
    merged: bool = True,
    ci: str | None = "success",
    dispatched: bool = True,
) -> CardOutcome:
    return CardOutcome(
        card_id="PVTI_1",
        fixture_id=fx_id,
        final_state=final_state,  # type: ignore[arg-type]
        dispatches=[PersonaDispatch(stage="implementing", role="implementer")] if dispatched else [],
        ci_results=[CIResult(head_sha="abc", conclusion=ci)] if ci else [],  # type: ignore[arg-type]
        merge=Merge(merged=merged),
    )


def _artifact(*cards: CardOutcome, schema_version: int = 1) -> RunArtifact:
    art = RunArtifact(
        schema_version=schema_version,
        run_id="20260823-000000",
        started_at=NOW,
        finished_at=NOW,
        wall_clock_seconds=60.0,
        config_fingerprint=ConfigFingerprint(hash="h", source_path=""),
        cards=list(cards),
    )
    art.totals = art.compute_totals()
    return art


class TestDeterministicTruthTable:
    def test_expected_merge_with_green_ci_passes(self) -> None:
        score = score_run(_artifact(_card()), [_fixture()])
        (v,) = score.cards
        assert v.category == "PASS" and v.correct and v.deterministic_ok
        assert score.components.correctness_rate == 1.0

    def test_merged_but_ci_red_fails_model(self) -> None:
        score = score_run(_artifact(_card(ci="failure")), [_fixture()])
        assert score.cards[0].category == "FAIL_MODEL"

    def test_merged_with_no_ci_evidence_fails_model(self) -> None:
        score = score_run(_artifact(_card(ci=None)), [_fixture()])
        (v,) = score.cards
        assert v.category == "FAIL_MODEL"
        assert "no CI evidence" in v.deterministic_detail

    def test_expected_blocked_but_merged_fails_model(self) -> None:
        score = score_run(_artifact(_card()), [_fixture(expected="blocked")])
        (v,) = score.cards
        assert v.category == "FAIL_MODEL"
        assert v.expected_final_state == "blocked" and v.observed_final_state == "merged"

    def test_expected_blocked_and_held_passes(self) -> None:
        card = _card(final_state="blocked", merged=False, ci="failure")
        score = score_run(_artifact(card), [_fixture(expected="blocked")])
        assert score.cards[0].category == "PASS"

    def test_expected_blocked_but_abandoned_is_not_a_proven_block(self) -> None:
        card = _card(final_state="abandoned", merged=False, ci=None)
        score = score_run(_artifact(card), [_fixture(expected="blocked")])
        assert score.cards[0].category == "FAIL_MODEL"

    def test_final_state_error_is_harness_failure(self) -> None:
        score = score_run(_artifact(_card(final_state="error", merged=False)), [_fixture()])
        (v,) = score.cards
        assert v.category == "FAIL_HARNESS"
        assert score.components.correctness_rate is None  # zero gradeable cards
        assert score.components.harness_failure_rate == 1.0
        assert score.scalar is None

    def test_zero_dispatches_is_harness_failure(self) -> None:
        card = _card(final_state="abandoned", merged=False, dispatched=False, ci=None)
        score = score_run(_artifact(card), [_fixture()])
        assert score.cards[0].category == "FAIL_HARNESS"

    def test_harness_failures_excluded_from_correctness_denominator(self) -> None:
        ok = _card()
        broken = CardOutcome(
            card_id="PVTI_2", fixture_id="fx2", final_state="error", merge=Merge(merged=False)
        )
        score = score_run(_artifact(ok, broken), [_fixture(), _fixture("fx2")])
        assert score.components.correctness_rate == 1.0
        assert score.components.harness_failure_rate == 0.5

    def test_empty_ground_truth_fails_loudly(self) -> None:
        with pytest.raises(GradingError, match="ground_truth"):
            score_run(_artifact(_card()), [_fixture(ground_truth="")])

    def test_unknown_fixture_id_fails_loudly(self) -> None:
        with pytest.raises(GradingError, match="fixture"):
            score_run(_artifact(_card(fx_id="ghost")), [_fixture()])

    def test_unknown_artifact_schema_version_fails_loudly(self) -> None:
        with pytest.raises(GradingError, match="schema"):
            score_run(_artifact(_card(), schema_version=99), [_fixture()])

    def test_uses_last_ci_result_when_no_head_match(self) -> None:
        card = _card()
        card.ci_results = [
            CIResult(head_sha="old", conclusion="failure"),
            CIResult(head_sha="new", conclusion="success"),
        ]
        score = score_run(_artifact(card), [_fixture()])
        assert score.cards[0].category == "PASS"


class TestAgreeToPass:
    def test_judge_disagreement_blocks_pass_and_records_both(self) -> None:
        judge = lambda rubric, det, evidence: (False, 2, "wrong behaviour")  # noqa: E731
        score = score_run(_artifact(_card()), [_fixture()], judge=judge, judge_model="m")
        (v,) = score.cards
        assert v.category == "FAIL_MODEL"
        assert v.deterministic_ok is True and v.judge_correct is False
        assert v.judge_reason == "wrong behaviour"
        assert score.judged is True and score.deterministic_only is False
        assert score.judge_model == "m"

    def test_judge_agreement_passes(self) -> None:
        judge = lambda rubric, det, evidence: (True, 5, "ok")  # noqa: E731
        score = score_run(_artifact(_card()), [_fixture()], judge=judge)
        assert score.cards[0].category == "PASS"
        assert score.cards[0].judge_quality == 5

    def test_judge_disabled_is_labeled_deterministic_only(self) -> None:
        score = score_run(_artifact(_card()), [_fixture()])
        assert score.judged is False and score.deterministic_only is True
        assert score.cards[0].judge_correct is None

    def test_judge_error_falls_back_per_card_never_flips(self) -> None:
        judge = lambda rubric, det, evidence: (None, None, "judge_error: boom")  # noqa: E731
        score = score_run(_artifact(_card()), [_fixture()], judge=judge)
        (v,) = score.cards
        assert v.category == "PASS"  # deterministic verdict stands
        assert v.judge_correct is None and "judge_error" in v.judge_reason

    def test_judge_never_upgrades_a_deterministic_failure(self) -> None:
        judge = lambda rubric, det, evidence: (True, 5, "looks great")  # noqa: E731
        score = score_run(_artifact(_card(ci="failure")), [_fixture()], judge=judge)
        assert score.cards[0].category == "FAIL_MODEL"

    def test_judge_not_called_for_harness_failures(self) -> None:
        calls: list[str] = []

        def judge(rubric: str, det: str, evidence: str):
            calls.append(rubric)
            return True, 5, "ok"

        score_run(_artifact(_card(final_state="error", merged=False)), [_fixture()], judge=judge)
        assert calls == []

    def test_same_artifact_twice_reproduces_score(self) -> None:
        artifact = _artifact(_card())
        a = score_run(artifact, [_fixture()])
        b = score_run(artifact, [_fixture()])
        assert a.model_dump(exclude={"scored_at"}) == b.model_dump(exclude={"scored_at"})

    def test_weights_are_carried_into_the_score(self) -> None:
        w = Weights(w_correctness=2.0, time_budget_seconds=120.0)
        score = score_run(_artifact(_card()), [_fixture()], weights=w)
        assert score.weights == w
        # 2.0*1.0 - 0.1*(60/120) = 1.95 (cost unknown -> omitted)
        assert score.scalar == pytest.approx(1.95)
        assert score.cost_component_missing is True


class TestFixtureBackwardCompat:
    def test_fixture_without_expected_final_state_defaults_to_merged(self) -> None:
        fx = Fixture(id="old", title="t", body="b", base_files={})
        assert fx.expected_final_state == "merged"

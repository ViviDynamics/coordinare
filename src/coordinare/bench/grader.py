"""Spec 135 — the whole-lifecycle grader.

Lifts spec-077's agree-to-PASS model (``scripts/persona_bench.py``) from per-role
to whole-card: deterministic checks read the run artifact's recorded facts against
the fixture's planted ground truth, the optional LLM judge rules on behavioural
correctness, and a card is PASS only when the deterministic contract holds AND the
judge does not disagree. Harness failures (the card never really ran) are split
out as FAIL_HARNESS so downstream optimization is never steered by infrastructure
noise (FR-005).

Deterministic signals (research.md R1):
* expected ``merged``  — final_state is merged AND the gating CI on the merged
  head concluded success (the fake's CI runs the fixture's real acceptance
  pytest, so green CI *is* "the planted acceptance tests pass").
* expected ``blocked`` — the card was affirmatively held (final_state blocked,
  no merge); an abandoned run is not a proven block.
* harness failure     — final_state ``error`` or zero recorded dispatches.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from coordinare.bench.score import (
    KNOWN_ARTIFACT_VERSIONS,
    CardVerdict,
    ComponentVector,
    ScoreObject,
    Weights,
    compute_scalar,
)

if TYPE_CHECKING:
    from coordinare.bench.artifact import CardOutcome, RunArtifact
    from coordinare.bench.fixtures import Fixture
    from coordinare.bench.judge import JudgeFn


class GradingError(ValueError):
    """A run that cannot be graded fails loudly — never a partial score (FR-001)."""


def _deterministic(outcome: CardOutcome, fixture: Fixture) -> tuple[bool, str]:
    """The deterministic contract for one card. Returns (ok, human-readable detail)."""
    expected = fixture.expected_final_state
    if expected == "blocked":
        # PASS requires the pipeline to have affirmatively HELD the card — an
        # "abandoned" run (budget exhausted mid-flight) proves nothing about a
        # correct block and does not count (spec US1 scenario 3).
        ok = not outcome.merge.merged and outcome.final_state == "blocked"
        detail = (
            f"expected an affirmative hold; observed final_state={outcome.final_state} "
            f"merged={outcome.merge.merged}"
        )
        return ok, detail

    # expected == "merged": merge must have happened AND the gating CI was green.
    # The artifact does not record the merged head's sha, so use the last CI
    # result (the one that gated the merge) — research.md R1.
    ci = outcome.ci_results[-1] if outcome.ci_results else None
    if not outcome.merge.merged or outcome.final_state != "merged":
        return False, f"expected merge; observed final_state={outcome.final_state} merged={outcome.merge.merged}"
    if ci is None:
        return False, "merged with no CI evidence recorded — not proven against acceptance tests"
    ok = ci.conclusion == "success"
    return ok, f"merged; gating CI ({ci.head_sha or 'unknown sha'}) concluded {ci.conclusion}"


def _grade_card(outcome: CardOutcome, fixture: Fixture, judge: JudgeFn | None) -> CardVerdict:
    if not fixture.ground_truth:
        msg = f"fixture {fixture.id!r} has empty ground_truth — card {outcome.card_id} is ungradeable"
        raise GradingError(msg)

    harness_failed = outcome.final_state == "error" or not outcome.dispatches
    if harness_failed:
        return CardVerdict(
            card_id=outcome.card_id,
            fixture_id=fixture.id,
            expected_final_state=fixture.expected_final_state,
            observed_final_state=outcome.final_state,
            category="FAIL_HARNESS",
            deterministic_detail=(
                f"harness failure: final_state={outcome.final_state}, "
                f"dispatches={len(outcome.dispatches)}"
            ),
        )

    det_ok, det_detail = _deterministic(outcome, fixture)
    judge_correct: bool | None = None
    judge_quality: int | None = None
    judge_reason = ""
    if judge is not None:
        evidence = outcome.model_dump_json()
        judge_correct, judge_quality, judge_reason = judge(fixture.ground_truth, det_detail, evidence)

    # Agree-to-PASS (077 categorize): the judge can veto, never rescue; a judge
    # error (None) leaves the deterministic verdict standing for this card.
    correct = det_ok and judge_correct is not False
    return CardVerdict(
        card_id=outcome.card_id,
        fixture_id=fixture.id,
        expected_final_state=fixture.expected_final_state,
        observed_final_state=outcome.final_state,
        category="PASS" if correct else "FAIL_MODEL",
        correct=correct,
        deterministic_ok=det_ok,
        deterministic_detail=det_detail,
        judge_correct=judge_correct,
        judge_quality=judge_quality,
        judge_reason=judge_reason,
    )


def score_run(
    artifact: RunArtifact,
    fixtures: list[Fixture],
    *,
    weights: Weights | None = None,
    judge: JudgeFn | None = None,
    judge_model: str | None = None,
    run_ref: str = "run.json",
) -> ScoreObject:
    """Grade one spec-134 run artifact against its fixtures' planted ground truth."""
    if artifact.schema_version not in KNOWN_ARTIFACT_VERSIONS:
        msg = (
            f"unknown run-artifact schema version {artifact.schema_version} "
            f"(known: {sorted(KNOWN_ARTIFACT_VERSIONS)}) — refusing to score"
        )
        raise GradingError(msg)
    weights = weights or Weights()
    by_id = {fx.id: fx for fx in fixtures}

    verdicts: list[CardVerdict] = []
    for outcome in artifact.cards:
        fixture = by_id.get(outcome.fixture_id or "")
        if fixture is None:
            msg = f"card {outcome.card_id} references fixture {outcome.fixture_id!r} not in the manifest"
            raise GradingError(msg)
        verdicts.append(_grade_card(outcome, fixture, judge))

    total = len(verdicts)
    harness = sum(1 for v in verdicts if v.category == "FAIL_HARNESS")
    gradeable = total - harness
    components = ComponentVector(
        correctness_rate=(sum(1 for v in verdicts if v.correct) / gradeable) if gradeable else None,
        harness_failure_rate=(harness / total) if total else 0.0,
        tokens_processed=artifact.totals.tokens_processed,
        cost_usd=artifact.totals.cost_usd,
        cost_estimated=artifact.totals.cost_estimated,
        wall_clock_seconds=artifact.wall_clock_seconds,
    )
    scalar, cost_missing = compute_scalar(components, weights)
    return ScoreObject(
        run_id=artifact.run_id,
        run_ref=run_ref,
        artifact_schema_version=artifact.schema_version,
        scored_at=datetime.now(UTC),
        judged=judge is not None,
        judge_model=judge_model if judge is not None else None,
        deterministic_only=judge is None,
        weights=weights,
        cards=verdicts,
        components=components,
        scalar=scalar,
        cost_component_missing=cost_missing,
    )

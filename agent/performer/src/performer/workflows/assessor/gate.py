"""Assessor gate step (spec 166 FR-006 to FR-009): applies bounded clarification rules.

Pure functions that apply the gate rules to the model's assessment:
- Cap questions to at most two (FR-006)
- Drop questions that match answered clarifications (FR-007)
- Force ready after two answered rounds (FR-008)
- Drop questions when ready wins (spec edge case)
- Set criteria source based on whether the card has criteria (FR-005)

The gate returns the final Assessment and a GateRecord documenting what changed.
"""
from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import structlog

from performer.workflows.assessor.models import (
    ModelAssessment,
    Assessment,
    ClarificationRound,
    GateRecord,
)
from performer.workflows._text import matches_answered

logger = structlog.get_logger(__name__)

if TYPE_CHECKING:
    from performer.workflows.assessor.intake import Intake


class GateError(Exception):
    """Error applying a gate rule."""
    pass


def cap_questions(questions: list[str], limit: int = 2) -> tuple[list[str], list[str]]:
    """Keep at most *limit* questions (FR-006).

    Args:
        questions: Questions from the model.
        limit: Maximum questions to keep. Default 2.

    Returns:
        (kept, dropped) tuples.
    """
    kept = questions[:limit]
    dropped = questions[limit:]
    return kept, dropped


def drop_answered(
    questions: list[str],
    answered: list[ClarificationRound | dict],
    threshold: float = 0.6,
) -> tuple[list[str], list[str], list[str]]:
    """Drop questions matching answered clarifications (FR-007).

    Args:
        questions: Questions from the model.
        answered: Answered clarifications from the intake (ClarificationRound or dict).
        threshold: Token overlap threshold for matching. Default 0.6 (60%).

    Returns:
        (kept, dropped, assumptions_added) tuples.
    """
    kept = []
    dropped = []
    assumptions = []

    for q in questions:
        matched = False
        for round_ in answered:
            if isinstance(round_, dict):
                answer_text = round_.get("answer", "").strip() if round_.get("answer") else ""
                question_text = round_.get("question", "")
            else:
                answer_text = round_.answer.strip() if round_.answer else ""
                question_text = round_.question

            if not answer_text:
                continue
            if matches_answered(q, question_text, threshold=threshold):
                dropped.append(q)
                assumptions.append(f"already answered: {question_text} -> {answer_text}")
                matched = True
                break
        if not matched:
            kept.append(q)

    return kept, dropped, assumptions


def force_ready(
    ready: bool,
    questions: list[str],
    answered_rounds: int,
    limit: int = 2,
    verdict: str = "work",
) -> tuple[bool, list[str], list[str]]:
    """Force ready after *limit* answered rounds (FR-008).

    410: the force applies to a work verdict only. A not_work or needs_split
    verdict is the model declining to build the card; answered rounds must not
    override that into a built card with invented assumptions.

    Args:
        ready: Current ready status.
        questions: Questions from the model.
        answered_rounds: Count of answered clarification rounds.
        limit: Answered rounds after which to force ready. Default 2.
        verdict: The assessment verdict ("work", "not_work", "needs_split").

    Returns:
        (ready, questions, assumptions_added) tuple.
    """
    assumptions = []

    if verdict != "work":
        # The card is not headed for the lifecycle; questions and readiness
        # stand exactly as the model left them.
        return ready, questions, assumptions

    if answered_rounds >= limit:
        # Two rounds of human answers are the budget: decide, do not ask again.
        ready = True
        for q in questions:
            assumptions.append(f"assumed: {q}")
        questions = []
    elif not ready and not questions:
        # Nothing usable is left to ask (every question was capped or already
        # answered): a not-ready assessment with no question would block the
        # card for nothing, so it is ready by construction.
        ready = True
        assumptions.append("assumed: no open question remained after the gate; proceeding on the card as written")

    return ready, questions, assumptions


def ready_wins(ready: bool, questions: list[str]) -> tuple[list[str], list[str]]:
    """When ready=True, drop questions to assumptions.

    Args:
        ready: Readiness status.
        questions: Questions from the model.

    Returns:
        (questions, assumptions_added) tuple.
    """
    assumptions = []

    if ready:
        for q in questions:
            assumptions.append(f"not asked: {q}")
        questions = []

    return questions, assumptions


def criteria_source(
    card_criteria: list | None,
    drafted: list,
    *,
    ready: bool = True,
) -> tuple[list, str]:
    """Set criteria_source based on card (FR-005).

    Args:
        card_criteria: Criteria from the card acceptance_criteria.
        drafted: Criteria drafted by the model.
        ready: The gate's readiness decision. A not-ready assessment has no
            settled outcome to write criteria for, so an empty draft is fine
            (a live round on an ambiguous card returned none, correctly).

    Returns:
        (criteria, criteria_source) tuple.
        A ready assessment whose card lists no criteria and whose model
        drafted none degrades to an empty list (410): the architect's
        blueprint permits zero criteria, so refusing the whole assessment
        with a GateError on exactly the docs, config, dep-bump and one-line
        cards was a performer error in disguise.
    """
    if any(str(c).strip() for c in (card_criteria or [])):
        return [], "card"

    if ready and not drafted:
        logger.warning(
            "assessor.criteria_degraded",
            msg="ready assessment drafted no criteria and the card listed none; continuing with an empty set",
        )

    return list(drafted), "assessor"


def _compute_assessment_hash(assessment: ModelAssessment) -> str:
    """Compute SHA-256 hash of assessment fields for deduplication."""
    data = (
        assessment.goal +
        assessment.expected_behavior +
        "\n".join(assessment.out_of_scope) +
        "\n".join(c.model_dump_json() for c in assessment.criteria)
    )
    return hashlib.sha256(data.encode()).hexdigest()


def run_gate(model_assessment: ModelAssessment, intake: "Intake") -> tuple[Assessment, GateRecord]:
    """Apply all gate rules in order to produce the final assessment.

    Order: ready_wins, drop_answered, cap_questions, force_ready, criteria_source.
    Dedupe runs before the cap so an already-answered first question does not
    consume one of the two slots a new question needed.

    Args:
        model_assessment: The model's assessment output.
        intake: The intake context including clarifications and card info.

    Returns:
        (Assessment, GateRecord) tuple.
    """
    questions = list(model_assessment.questions)
    assumptions = list(model_assessment.assumptions)
    ready = model_assessment.ready
    verdict = model_assessment.verdict

    # 1. ready_wins: if already ready, move questions to assumptions
    questions_after_ready_wins, ready_wins_assumptions = ready_wins(ready, questions)
    questions = questions_after_ready_wins
    assumptions.extend(ready_wins_assumptions)

    # 2. drop_answered: remove questions matching answered clarifications
    answered_rounds = intake.answered_rounds
    answered_clarifications = intake.clarifications
    questions_after_drop, questions_dropped_as_answered, drop_answered_assumptions = drop_answered(
        questions, answered_clarifications
    )
    questions = questions_after_drop
    assumptions.extend(drop_answered_assumptions)

    # 3. cap_questions: keep at most 2 of what survived the dedupe
    questions_kept, questions_dropped_by_cap = cap_questions(questions)
    questions = questions_kept

    # 4. force_ready: ready after 2 answered rounds, or when nothing usable is left to ask.
    # 410: never applied to a not_work / needs_split verdict.
    ready, questions, force_ready_assumptions = force_ready(ready, questions, answered_rounds, limit=2, verdict=verdict)
    assumptions.extend(force_ready_assumptions)

    # 5. criteria_source: determine if criteria come from card or assessor
    crit, source = criteria_source(
        intake.criteria or [],
        model_assessment.criteria,
        ready=ready,
    )

    # Truncate assumptions to max 10
    assumptions = assumptions[:10]

    # Compute assessment hash
    temp_assessment = ModelAssessment(
        ready=ready,
        goal=model_assessment.goal,
        expected_behavior=model_assessment.expected_behavior,
        out_of_scope=model_assessment.out_of_scope,
        questions=questions,
        assumptions=assumptions,
        criteria=crit,
        verdict=verdict,
    )
    hash_value = _compute_assessment_hash(temp_assessment)

    assessment = Assessment(
        ready=ready,
        goal=model_assessment.goal,
        expected_behavior=model_assessment.expected_behavior,
        out_of_scope=model_assessment.out_of_scope,
        questions=questions,
        assumptions=assumptions,
        criteria=crit,
        verdict=verdict,
        criteria_source=source,
        clarifications=intake.clarifications,
        assessment_hash=hash_value,
    )

    # Extract answers from clarifications (dicts or ClarificationRound objects)
    answers_matched = []
    for c in answered_clarifications:
        if isinstance(c, dict):
            answer = c.get("answer", "").strip() if c.get("answer") else ""
        else:
            answer = c.answer.strip() if c.answer else ""
        if answer:
            answers_matched.append(answer)

    record = GateRecord(
        questions_kept=len(questions),
        questions_dropped_by_cap=questions_dropped_by_cap,
        questions_dropped_as_answered=questions_dropped_as_answered,
        answers_matched=answers_matched,
        questions_turned_to_assumptions=force_ready_assumptions,
        round_count=answered_rounds,
    )

    return assessment, record

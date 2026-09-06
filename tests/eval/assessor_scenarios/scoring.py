"""Qualitative scoring for one assessor run against a fixture's expectations.

The signals are the ones that matter to the feature: readiness, question count,
criteria source, no re-asking answered questions, write-free check, and the
assessment hand-off to architecting. Not a CI gate in live mode (Constitution II);
the stubbed mode is exact and does run in CI.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from performer.workflows._text import matches_answered

from coordinare.graph.nodes.dispatch_performer import (
    inject_assessment,
    reset_assessment_for_assessor,
)
from tests.eval.assessor_scenarios.fixtures import Fixture


@dataclass
class Score:
    """Score for one assessor run."""

    fixture: str
    checks: dict[str, bool] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(self.checks.values())


def score_run(fixture: Fixture, report: dict, *, live: bool = False) -> Score:
    """Score an assessor run against fixture expectations.

    Checks:
    - ready: whether the assessment ready flag matches expectations
    - question_count: number of questions within expected range
    - criteria_source: whether criteria come from card or assessor
    - criteria_count: number of criteria within expected range (for assessor-sourced)
    - no_reask: no question in report matches an answered clarification
    - write_free: report confirms no commands ran and no files written
    - assumptions_carry_forced_question: (answered fixture only) the new question appears in assumptions as "assumed:"
    - handoff: assessment is injected into architecting dispatch and cleared on assessor re-dispatch

    Args:
        fixture: The fixture being scored.
        report: The PerformerResponse report dict.
        live: If True, relax exact question count for ambiguous (1-2 stays) and criteria count.

    Returns:
        Score with checks and notes.
    """
    assessment = report.get("assessment", {})
    exp = fixture.expect
    s = Score(fixture=fixture.name)

    # Check ready
    is_ready = assessment.get("ready", False)
    s.checks["ready"] = is_ready == exp.ready
    if not s.checks["ready"]:
        s.notes.append(f"ready: got {is_ready}, expected {exp.ready}")

    # Check question count
    questions = assessment.get("questions", [])
    n_questions = len(questions)
    s.checks["question_count"] = exp.min_questions <= n_questions <= exp.max_questions
    if not s.checks["question_count"]:
        if live and fixture.name == "ambiguous" and exp.min_questions <= n_questions <= exp.max_questions:
            s.checks["question_count"] = True
        else:
            s.notes.append(f"question_count: got {n_questions}, expected {exp.min_questions}-{exp.max_questions}")

    # Check criteria_source
    source = assessment.get("criteria_source")
    s.checks["criteria_source"] = source == exp.criteria_source
    if not s.checks["criteria_source"]:
        s.notes.append(f"criteria_source: got {source}, expected {exp.criteria_source}")

    # Check criteria count
    criteria = assessment.get("criteria", [])
    n_criteria = len(criteria)
    s.checks["criteria_count"] = exp.min_criteria <= n_criteria <= exp.max_criteria
    if not s.checks["criteria_count"]:
        if live:
            s.checks["criteria_count"] = True
        else:
            s.notes.append(f"criteria_count: got {n_criteria}, expected {exp.min_criteria}-{exp.max_criteria}")

    # Check no_reask: no question in the report matches an answered clarification
    answered_clarifications = fixture.clarifications
    asked_questions = assessment.get("questions", [])
    reasks = []
    for asked in asked_questions:
        for clarif in answered_clarifications:
            if matches_answered(asked, clarif.get("question", ""), threshold=0.6):
                reasks.append(asked)
    s.checks["no_reask"] = len(reasks) == 0
    if not s.checks["no_reask"]:
        s.notes.append(f"no_reask: questions re-ask answered clarifications: {reasks}")

    # Check write_free: no commands ran, no screenshot_capture, no dom_reader
    wf = report.get("write_free_check", {})
    write_free_passed = bool(wf.get("passed"))
    has_screenshot = bool(wf.get("has_screenshot_capture"))
    has_dom = bool(wf.get("has_dom_reader"))
    s.checks["write_free"] = write_free_passed and not has_screenshot and not has_dom
    if not s.checks["write_free"]:
        s.notes.append(
            f"write_free: passed={write_free_passed}, commands_run={wf.get('commands_run', 0)}, "
            f"has_screenshot_capture={has_screenshot}, has_dom_reader={has_dom}"
        )

    # Check assumptions_carry_forced_question (answered fixture only): every
    # question the gate turned into an assumption (FR-008) is in assumptions
    # with the "assumed:" prefix. The stub always asks one more question; a
    # live model may already have decided for itself, so the check keys off
    # what the gate recorded, not off a fixed sentence.
    if fixture.name == "answered":
        assumptions = assessment.get("assumptions", [])
        turned = (report.get("gate_record") or {}).get("questions_turned_to_assumptions") or []
        s.checks["assumptions_carry_forced_question"] = all(t in assumptions for t in turned) and (
            live or bool(turned)
        )
        if not s.checks["assumptions_carry_forced_question"]:
            s.notes.append(f"assumptions_carry_forced_question: gate turned {turned}, assumptions {assumptions}")

    # Check handoff: assessment is injected into architecting dispatch only
    # Build a mock state and check inject_assessment
    mock_state = {
        "assessment": assessment,
        "card_id": "c1",
    }
    card_context_arch = {}
    inject_assessment(card_context_arch, mock_state, performer_stage="architecting")
    s.checks["handoff_inject"] = "assessment" in card_context_arch
    if not s.checks["handoff_inject"]:
        s.notes.append("handoff: assessment not injected into architecting dispatch")

    # Check that assessment is NOT injected into other stages
    card_context_other = {}
    inject_assessment(card_context_other, mock_state, performer_stage="implementing")
    s.checks["handoff_not_other"] = "assessment" not in card_context_other
    if not s.checks["handoff_not_other"]:
        s.notes.append("handoff: assessment incorrectly injected into non-architecting stage")

    # Check that reset_assessment_for_assessor clears it
    state_for_reset = {"assessment": assessment.copy()}
    reset_assessment_for_assessor(state_for_reset, "assessing")
    s.checks["handoff_reset"] = state_for_reset.get("assessment") is None
    if not s.checks["handoff_reset"]:
        s.notes.append("handoff_reset: assessment not cleared on assessor re-dispatch")

    return s

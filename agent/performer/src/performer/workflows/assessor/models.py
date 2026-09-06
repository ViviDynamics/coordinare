"""Assessment model: the structured product reading of one card.

Spec 166 data-model.md: every field is bounded and required. Extra fields are
forbidden. The model output is validated, optionally reprompted once, and then
run through pure gate functions that apply the clarification-loop rules.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from typing_extensions import Annotated

from performer.workflows.architect.models import Criterion


class _Bounded(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ClarificationRound(_Bounded):
    """One question-answer pair from the card's clarification history.

    Carried from the dispatch Score; the assessor workflow reads and passes through.
    """

    question: str
    answer: str


class GateRecord(_Bounded):
    """Documents what the gate changed in the model's answer.

    Travels in the report for eval and logging; coordinare does not consume it.
    """

    questions_kept: int
    questions_dropped_by_cap: list[str]
    questions_dropped_as_answered: list[str]
    answers_matched: list[str]
    questions_turned_to_assumptions: list[str]
    round_count: int


class ModelAssessment(_Bounded):
    """The shape the model returns before gate processing.

    Bounded per spec 166 data-model.md with extra="forbid", but deliberately
    looser than the final Assessment on two points the GATE decides, so a
    model that over-asks costs a trim and not a reprompt (FR-006, FR-008):
    up to six questions come in (the gate keeps two), and a not-ready answer
    with no question is accepted (the gate makes it ready with a recorded
    assumption). Code adds criteria_source, clarifications and
    assessment_hash during gate processing.
    """

    ready: bool
    goal: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    expected_behavior: Annotated[str, StringConstraints(max_length=1000)]
    out_of_scope: Annotated[list[Annotated[str, StringConstraints(max_length=300)]], Field(max_length=5)]
    questions: Annotated[list[Annotated[str, StringConstraints(max_length=300)]], Field(max_length=6)]
    assumptions: Annotated[list[Annotated[str, StringConstraints(max_length=300)]], Field(max_length=10)]
    criteria: Annotated[list[Criterion], Field(max_length=8)]


class Assessment(_Bounded):
    """The structured product reading of one card.

    Every field is required and bounded per spec 166 data-model.md.
    Enforced at the boundary: extra="forbid" rejects unknown fields.
    A not-ready assessment must have at least one question.

    This is the final assessment after gate processing, with criteria_source,
    clarifications, and assessment_hash added by code.
    """

    ready: bool
    goal: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    expected_behavior: Annotated[str, StringConstraints(max_length=1000)]
    out_of_scope: Annotated[list[Annotated[str, StringConstraints(max_length=300)]], Field(max_length=5)]
    questions: Annotated[list[Annotated[str, StringConstraints(max_length=300)]], Field(max_length=2)]
    assumptions: Annotated[list[Annotated[str, StringConstraints(max_length=300)]], Field(max_length=10)]
    criteria: Annotated[list[Criterion], Field(max_length=8)]
    criteria_source: Literal["card", "assessor"]
    clarifications: list[ClarificationRound]
    assessment_hash: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]

    @model_validator(mode="after")
    def _not_ready_needs_question(self) -> Assessment:
        """A not-ready assessment must have at least one question."""
        if not self.ready and not self.questions:
            raise ValueError("not-ready assessment must have at least one question")
        return self


__all__ = ["ModelAssessment", "Assessment", "ClarificationRound", "GateRecord", "Criterion"]


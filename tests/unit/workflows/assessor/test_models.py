"""T012/T013 - Assessment models (spec 166 data-model.md).

Every list and string bound from data-model.md is enforced at the boundary.
Extra fields are forbidden. Required fields are required. Validators enforce:
- Not ready assessment must have at least one question.
- Ready assessment may have zero questions.
- Criteria must have at least one item when criteria_source is "assessor".
"""
from __future__ import annotations

import pytest
from performer.workflows.architect.models import Criterion
from performer.workflows.assessor.models import Assessment, ClarificationRound, GateRecord
from pydantic import ValidationError


def _assessment(**over) -> dict:
    """Build a minimal valid assessment."""
    base = {
        "ready": True,
        "goal": "Do something useful",
        "expected_behavior": "The user sees a result",
        "out_of_scope": [],
        "questions": [],
        "assumptions": [],
        "criteria": [],
        "criteria_source": "card",
        "clarifications": [],
        "assessment_hash": "a" * 64,
    }
    base.update(over)
    return base


class TestAssessmentBounds:
    """Test every bound from data-model.md."""

    def test_minimal_valid_assessment(self):
        """Minimal assessment validates."""
        Assessment.model_validate(_assessment())

    def test_ready_is_required(self):
        """ready is a required boolean."""
        data = _assessment()
        del data["ready"]
        with pytest.raises(ValidationError):
            Assessment.model_validate(data)

    def test_goal_is_required_and_bounded(self):
        """goal is required, 1-500 chars."""
        Assessment.model_validate(_assessment(goal="x"))
        Assessment.model_validate(_assessment(goal="x" * 500))
        with pytest.raises(ValidationError):
            Assessment.model_validate(_assessment(goal=""))
        with pytest.raises(ValidationError):
            Assessment.model_validate(_assessment(goal="x" * 501))

    def test_expected_behavior_bounded(self):
        """expected_behavior is 0-1000 chars."""
        Assessment.model_validate(_assessment(expected_behavior=""))
        Assessment.model_validate(_assessment(expected_behavior="x" * 1000))
        with pytest.raises(ValidationError):
            Assessment.model_validate(_assessment(expected_behavior="x" * 1001))

    def test_out_of_scope_bounded(self):
        """out_of_scope is 0-5 items, each <=300 chars."""
        Assessment.model_validate(_assessment(out_of_scope=[]))
        Assessment.model_validate(_assessment(out_of_scope=["x" * 300] * 5))
        with pytest.raises(ValidationError):
            Assessment.model_validate(_assessment(out_of_scope=["x" * 300] * 6))
        with pytest.raises(ValidationError):
            Assessment.model_validate(_assessment(out_of_scope=["x" * 301]))

    def test_questions_bounded(self):
        """questions is 0-2 items, each <=300 chars."""
        Assessment.model_validate(_assessment(questions=[]))
        Assessment.model_validate(_assessment(questions=["q"] * 2))
        with pytest.raises(ValidationError):
            Assessment.model_validate(_assessment(questions=["q"] * 3))
        with pytest.raises(ValidationError):
            Assessment.model_validate(_assessment(questions=["x" * 301]))

    def test_assumptions_bounded(self):
        """assumptions is 0-10 items, each <=300 chars."""
        Assessment.model_validate(_assessment(assumptions=[]))
        Assessment.model_validate(_assessment(assumptions=["a"] * 10))
        with pytest.raises(ValidationError):
            Assessment.model_validate(_assessment(assumptions=["a"] * 11))
        with pytest.raises(ValidationError):
            Assessment.model_validate(_assessment(assumptions=["x" * 301]))

    def test_criteria_bounded(self):
        """criteria is 0-8 items."""
        Assessment.model_validate(_assessment(criteria=[]))
        crit = {"surface": "s", "action": "a", "expected": "e", "kind": "functional"}
        Assessment.model_validate(_assessment(criteria=[crit] * 8))
        with pytest.raises(ValidationError):
            Assessment.model_validate(_assessment(criteria=[crit] * 9))

    def test_criteria_source_must_be_card_or_assessor(self):
        """criteria_source is a required literal."""
        Assessment.model_validate(_assessment(criteria_source="card"))
        Assessment.model_validate(_assessment(criteria_source="assessor"))
        with pytest.raises(ValidationError):
            Assessment.model_validate(_assessment(criteria_source="other"))

    def test_clarifications_are_carried(self):
        """clarifications is 0-N ClarificationRound items."""
        Assessment.model_validate(_assessment(clarifications=[]))
        Assessment.model_validate(_assessment(clarifications=[
            {"question": "q", "answer": "a"},
            {"question": "q2", "answer": "a2"},
        ]))

    def test_assessment_hash_is_required_and_64_hex(self):
        """assessment_hash is a required 64-char hex string."""
        Assessment.model_validate(_assessment(assessment_hash="0" * 64))
        with pytest.raises(ValidationError):
            Assessment.model_validate(_assessment(assessment_hash="g" * 64))  # Not hex
        with pytest.raises(ValidationError):
            Assessment.model_validate(_assessment(assessment_hash="0" * 63))  # Too short

    def test_extra_fields_forbidden(self):
        """Unknown fields are rejected."""
        with pytest.raises(ValidationError):
            Assessment.model_validate({**_assessment(), "extra_field": "value"})

    def test_dependencies_field_forbidden(self):
        """dependencies field is explicitly rejected (spec 166 FR-010)."""
        with pytest.raises(ValidationError):
            Assessment.model_validate({**_assessment(), "dependencies": []})


class TestAssessmentValidators:
    """Test model validators."""

    def test_not_ready_requires_at_least_one_question(self):
        """A not-ready assessment must have at least one question."""
        Assessment.model_validate(_assessment(ready=False, questions=["q1"]))
        with pytest.raises(ValidationError):
            Assessment.model_validate(_assessment(ready=False, questions=[]))

    def test_ready_assessment_may_have_zero_questions(self):
        """A ready assessment can have zero questions."""
        Assessment.model_validate(_assessment(ready=True, questions=[]))


class TestClarificationRound:
    """ClarificationRound is a simple question-answer pair."""

    def test_minimal_clarification_round(self):
        """question and answer are required strings."""
        ClarificationRound.model_validate({"question": "q", "answer": "a"})

    def test_extra_fields_forbidden(self):
        """Unknown fields are rejected."""
        with pytest.raises(ValidationError):
            ClarificationRound.model_validate({
                "question": "q",
                "answer": "a",
                "extra": "field",
            })


class TestGateRecord:
    """GateRecord documents what the gate changed."""

    def test_minimal_gate_record(self):
        """All fields are required."""
        GateRecord.model_validate({
            "questions_kept": 1,
            "questions_dropped_by_cap": [],
            "questions_dropped_as_answered": [],
            "answers_matched": [],
            "questions_turned_to_assumptions": [],
            "round_count": 0,
        })

    def test_questions_kept_is_int(self):
        """questions_kept must be an integer."""
        with pytest.raises(ValidationError):
            GateRecord.model_validate({
                "questions_kept": "one",
                "questions_dropped_by_cap": [],
                "questions_dropped_as_answered": [],
                "answers_matched": [],
                "questions_turned_to_assumptions": [],
                "round_count": 0,
            })

    def test_round_count_tracks_answered_rounds(self):
        """round_count is the number of answered clarification rounds."""
        GateRecord.model_validate({
            "questions_kept": 0,
            "questions_dropped_by_cap": [],
            "questions_dropped_as_answered": [],
            "answers_matched": [],
            "questions_turned_to_assumptions": [],
            "round_count": 2,
        })

    def test_extra_fields_forbidden(self):
        """Unknown fields are rejected."""
        with pytest.raises(ValidationError):
            GateRecord.model_validate({
                "questions_kept": 1,
                "questions_dropped_by_cap": [],
                "questions_dropped_as_answered": [],
                "answers_matched": [],
                "questions_turned_to_assumptions": [],
                "round_count": 0,
                "extra": "field",
            })


class TestCriterionReuse:
    """Criterion is reused from architect models, not redefined."""

    def test_criterion_from_architect_models(self):
        """Criterion is imported from architect.models."""
        from performer.workflows.architect.models import Criterion as ArchCriterion

        crit = Criterion(surface="/", action="do", expected="see", kind="functional")
        assert isinstance(crit, ArchCriterion)

    def test_criterion_validates_bounds(self):
        """Criterion bounds are enforced (surface <=120, action <=200, expected <=300)."""
        Criterion.model_validate({
            "surface": "x" * 120,
            "action": "y" * 200,
            "expected": "z" * 300,
            "kind": "visual",
        })
        with pytest.raises(ValidationError):
            Criterion.model_validate({
                "surface": "x" * 121,
                "action": "y",
                "expected": "z",
                "kind": "functional",
            })


class TestAssessmentJsonSchema:
    """The rendered schema matches the contract fixture."""

    def test_schema_renders_for_prompt(self):
        """Assessment can render a JSON schema for the model prompt."""
        schema = Assessment.model_json_schema()
        assert isinstance(schema, dict)
        assert "properties" in schema or "$defs" in schema

    def test_schema_includes_required_fields(self):
        """Schema defines all required fields."""
        schema = Assessment.model_json_schema()
        required = schema.get("required", [])
        assert "ready" in required
        assert "goal" in required
        assert "criteria_source" in required


def _model_assessment(**over) -> dict:
    """Build a minimal valid model assessment (model's output shape)."""
    base = {
        "ready": True,
        "goal": "Do something useful",
        "expected_behavior": "The user sees a result",
        "out_of_scope": [],
        "questions": [],
        "assumptions": [],
        "criteria": [],
    }
    base.update(over)
    return base


class TestModelAssessment:
    """ModelAssessment is the shape the model returns before gate processing.

    It differs from Assessment by not having criteria_source, clarifications,
    or assessment_hash. These are added by code during the gate phase.
    """

    def test_minimal_valid_model_assessment(self):
        """Minimal model assessment validates."""
        from performer.workflows.assessor.models import ModelAssessment
        ModelAssessment.model_validate(_model_assessment())

    def test_ready_is_required(self):
        """ready is a required boolean."""
        from performer.workflows.assessor.models import ModelAssessment
        data = _model_assessment()
        del data["ready"]
        with pytest.raises(ValidationError):
            ModelAssessment.model_validate(data)

    def test_goal_is_required_and_bounded(self):
        """goal is required, 1-500 chars."""
        from performer.workflows.assessor.models import ModelAssessment
        ModelAssessment.model_validate(_model_assessment(goal="x"))
        ModelAssessment.model_validate(_model_assessment(goal="x" * 500))
        with pytest.raises(ValidationError):
            ModelAssessment.model_validate(_model_assessment(goal=""))
        with pytest.raises(ValidationError):
            ModelAssessment.model_validate(_model_assessment(goal="x" * 501))

    def test_expected_behavior_bounded(self):
        """expected_behavior is 0-1000 chars."""
        from performer.workflows.assessor.models import ModelAssessment
        ModelAssessment.model_validate(_model_assessment(expected_behavior=""))
        ModelAssessment.model_validate(_model_assessment(expected_behavior="x" * 1000))
        with pytest.raises(ValidationError):
            ModelAssessment.model_validate(_model_assessment(expected_behavior="x" * 1001))

    def test_out_of_scope_bounded(self):
        """out_of_scope is 0-5 items, each <=300 chars."""
        from performer.workflows.assessor.models import ModelAssessment
        ModelAssessment.model_validate(_model_assessment(out_of_scope=[]))
        ModelAssessment.model_validate(_model_assessment(out_of_scope=["x" * 300] * 5))
        with pytest.raises(ValidationError):
            ModelAssessment.model_validate(_model_assessment(out_of_scope=["x" * 300] * 6))
        with pytest.raises(ValidationError):
            ModelAssessment.model_validate(_model_assessment(out_of_scope=["x" * 301]))

    def test_questions_bounded_loosely_so_the_gate_caps(self):
        """The model may over-ask (up to 6); the gate keeps two (FR-006).
        Seven is a schema violation and a reprompt."""
        from performer.workflows.assessor.models import ModelAssessment
        ModelAssessment.model_validate(_model_assessment(questions=[]))
        ModelAssessment.model_validate(_model_assessment(questions=["q"] * 6))
        with pytest.raises(ValidationError):
            ModelAssessment.model_validate(_model_assessment(questions=["q"] * 7))
        with pytest.raises(ValidationError):
            ModelAssessment.model_validate(_model_assessment(questions=["x" * 301]))

    def test_assumptions_bounded(self):
        """assumptions is 0-10 items, each <=300 chars."""
        from performer.workflows.assessor.models import ModelAssessment
        ModelAssessment.model_validate(_model_assessment(assumptions=[]))
        ModelAssessment.model_validate(_model_assessment(assumptions=["a"] * 10))
        with pytest.raises(ValidationError):
            ModelAssessment.model_validate(_model_assessment(assumptions=["a"] * 11))
        with pytest.raises(ValidationError):
            ModelAssessment.model_validate(_model_assessment(assumptions=["x" * 301]))

    def test_criteria_bounded(self):
        """criteria is 0-8 items."""
        from performer.workflows.assessor.models import ModelAssessment
        ModelAssessment.model_validate(_model_assessment(criteria=[]))
        crit = {"surface": "s", "action": "a", "expected": "e", "kind": "functional"}
        ModelAssessment.model_validate(_model_assessment(criteria=[crit] * 8))
        with pytest.raises(ValidationError):
            ModelAssessment.model_validate(_model_assessment(criteria=[crit] * 9))

    def test_no_criteria_source_field(self):
        """criteria_source is NOT a field in ModelAssessment."""
        from performer.workflows.assessor.models import ModelAssessment
        with pytest.raises(ValidationError):
            ModelAssessment.model_validate({**_model_assessment(), "criteria_source": "card"})

    def test_no_clarifications_field(self):
        """clarifications is NOT a field in ModelAssessment."""
        from performer.workflows.assessor.models import ModelAssessment
        with pytest.raises(ValidationError):
            ModelAssessment.model_validate({**_model_assessment(), "clarifications": []})

    def test_no_assessment_hash_field(self):
        """assessment_hash is NOT a field in ModelAssessment."""
        from performer.workflows.assessor.models import ModelAssessment
        with pytest.raises(ValidationError):
            ModelAssessment.model_validate({**_model_assessment(), "assessment_hash": "0" * 64})

    def test_extra_fields_forbidden(self):
        """Unknown fields are rejected."""
        from performer.workflows.assessor.models import ModelAssessment
        with pytest.raises(ValidationError):
            ModelAssessment.model_validate({**_model_assessment(), "extra_field": "value"})

    def test_dependencies_field_forbidden(self):
        """dependencies field is explicitly rejected (spec 166 FR-010)."""
        from performer.workflows.assessor.models import ModelAssessment
        with pytest.raises(ValidationError):
            ModelAssessment.model_validate({**_model_assessment(), "dependencies": []})

    def test_not_ready_without_a_question_is_accepted_for_the_gate_to_decide(self):
        """FR-008: the gate turns a not-ready answer with nothing to ask into
        ready with a recorded assumption; the schema must not reprompt for it."""
        from performer.workflows.assessor.models import ModelAssessment
        ModelAssessment.model_validate(_model_assessment(ready=False, questions=["q1"]))
        ModelAssessment.model_validate(_model_assessment(ready=False, questions=[]))



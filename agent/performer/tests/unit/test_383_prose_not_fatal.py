"""383: a card must not be lost because the model wrote a long sentence.

Measured on the website symphony, 2026-09-12: eight "String should have at most
500 characters" violations in one night, three of them escalating through
BACKEND_FORMAT_ERROR and the bounded retries to a blocked card. The offending
field was TestObservation.summary -- prose that nothing branches on.

The distinction this pins: a field that is READ may be clamped to its bound;
a field that is EXECUTED or MATCHED may not, because truncating it changes what
it means.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from performer.workflows.prose import Prose


def test_prose_longer_than_the_bound_is_clamped_not_rejected():
    from pydantic import BaseModel

    class M(BaseModel):
        summary: Prose(20) = ""

    m = M(summary="x" * 500)
    assert len(m.summary) == 20, "over-long prose was not clamped"


def test_prose_within_the_bound_is_untouched():
    from pydantic import BaseModel

    class M(BaseModel):
        summary: Prose(100) = ""

    m = M(summary="a readable sentence")
    assert m.summary == "a readable sentence"


def test_prose_still_bounds_an_unbounded_response():
    """The cap exists to stop a runaway response eating context. Clamping keeps
    that guarantee; it only stops the bound being fatal."""
    from pydantic import BaseModel

    class M(BaseModel):
        summary: Prose(50) = ""

    assert len(M(summary="y" * 100_000).summary) == 50


def test_prose_tolerates_a_non_string():
    from pydantic import BaseModel

    class M(BaseModel):
        summary: Prose(20) = ""

    with pytest.raises(ValidationError):
        M(summary={"not": "a string"})


# --- the fields that actually failed ----------------------------------------

def test_a_long_observation_summary_no_longer_fails_validation():
    """The exact failure: 'String should have at most 500 characters' on
    TestObservation.summary, which blocked card ...q-9Tg after three retries."""
    from performer.workflows.implementer.observe import TestObservation

    obs = TestObservation(outcome="assertion_failure", summary="s" * 5000)
    assert len(obs.summary) == 500
    assert obs.outcome == "assertion_failure", "the decision field survived intact"


def test_a_long_red_judgement_reason_no_longer_fails_validation():
    from performer.workflows.implementer.observe import RedJudgement

    j = RedJudgement(is_expected_red=True, reason="r" * 5000)
    assert len(j.reason) == 500
    assert j.is_expected_red is True


def test_a_long_environment_problem_no_longer_fails_validation():
    from performer.workflows.implementer.observe import TestObservation

    obs = TestObservation(outcome="could_not_run", environment_problem="e" * 5000)
    assert len(obs.environment_problem) == 300


def test_the_decision_fields_are_still_strict():
    """Clamping prose must not soften the values the lane actually branches on."""
    from performer.workflows.implementer.observe import RedJudgement, TestObservation

    with pytest.raises(ValidationError):
        TestObservation(outcome="not_a_real_outcome")
    with pytest.raises(ValidationError):
        RedJudgement(is_expected_red=True, reason="ok", next_action="invent_something")


#: Fields whose value is RUN. A truncated command is a different command, so
#: every one of these must still reject an over-long value rather than clamp.
#: Enumerated because the rule has two instances and the first cut of this test
#: pinned only one: switching start_command to Prose(300) passed all twelve
#: tests, which is the "pins one example while the rule is violated elsewhere"
#: defect this codebase keeps producing.
EXECUTED_COMMAND_FIELDS = ("test_command", "start_command")


@pytest.mark.parametrize("field", EXECUTED_COMMAND_FIELDS)
def test_an_executed_command_is_not_clamped(field):
    """A truncated command is a DIFFERENT command, so an over-long one must
    fail loudly rather than quietly run something the model did not ask for."""
    from performer.workflows.project_shape import ProjectShape

    values = {"project_name": "x", "summary": "y", "test_command": "", "start_command": ""}
    values[field] = "c" * 5000
    with pytest.raises(ValidationError, match="at most"):
        ProjectShape(**values)


def test_every_executed_command_field_is_actually_declared():
    """Guards the list above: a new executed command added to ProjectShape and
    not listed here would be unprotected, and no test would say so."""
    from performer.workflows.project_shape import ProjectShape

    declared = {n for n in ProjectShape.model_fields if n.endswith("_command")}
    assert declared == set(EXECUTED_COMMAND_FIELDS), (
        f"ProjectShape command fields changed: {declared}. Every field whose value is RUN "
        "must be listed in EXECUTED_COMMAND_FIELDS so the no-clamping rule covers it."
    )


def test_the_persona_states_the_budget():
    """The reprompt had nothing to correct against: the model was never told a
    limit existed, so it produced the same over-long summary twice. Eight
    violations, four escalations, in one night."""
    from performer.workflows.implementer.observe import observe_persona

    persona = observe_persona()
    assert "500" in persona, "the summary budget is not stated to the model"


def test_an_empty_reason_is_also_not_fatal():
    """RedJudgement.reason previously carried min_length=1. Dropping it is
    deliberate and is the same argument as the upper bound: an explanation that
    is missing is no more worth losing a card over than one that is long. The
    verdict lives in is_expected_red, which stays strict.

    Pinned so that restoring min_length is a visible decision rather than a
    quiet re-introduction of the failure this issue is about.
    """
    from performer.workflows.implementer.observe import RedJudgement

    j = RedJudgement(is_expected_red=False, reason="")
    assert j.reason == ""
    assert j.is_expected_red is False


def test_the_bound_is_still_advertised_to_the_model():
    """The clamp alone would satisfy every behavioural test while quietly
    removing the bound from the JSON schema the model is shown.

    schema_instruction() renders model_json_schema(), so StringConstraints is
    what puts maxLength in front of the model. Dropping it leaves the model
    with no stated budget -- which is the condition that produced this issue:
    eight violations, four escalations, because nothing ever told it a limit
    existed. Mutation P2 (removing StringConstraints) passed all eleven
    behavioural tests.
    """
    from performer.workflows.implementer.observe import RedJudgement, TestObservation

    obs = TestObservation.model_json_schema()["properties"]
    assert obs["summary"].get("maxLength") == 500
    assert obs["environment_problem"].get("maxLength") == 300
    assert RedJudgement.model_json_schema()["properties"]["reason"].get("maxLength") == 500

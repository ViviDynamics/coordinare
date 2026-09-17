"""166 end-to-end independent test: the whole workflow against a stubbed model.

No writes, budget respected, valid assessment, no command ran, four step
events in order, and the report carries what coordinare needs to lift.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from performer.workflows.assessor import STEPS, AssessorWorkflow
from performer.workflows.assessor.models import Assessment
from performer.workflows.base import WorkflowMetrics
from performer.workflows.budget import ModelReply
from performer.workflows.toolkit import Toolkit

_CLEAR_ASSESSMENT = {
    "ready": True,
    "goal": "Enable timesheets in the payroll system",
    "expected_behavior": "Users can submit weekly timesheets and admins can approve them",
    "out_of_scope": ["Real-time notifications", "Mobile app"],
    "questions": [],
    "assumptions": [],
    "criteria": [],
}

_AMBIGUOUS_ASSESSMENT = {
    "ready": False,
    "goal": "Improve services page",
    "expected_behavior": "Page looks better",
    "out_of_scope": [],
    "questions": ["Which audience is this page targeting?"],
    "assumptions": [],
    "criteria": [],
}


def _stub_toolkit(model_response: dict):
    """A Toolkit whose model returns the given response, with no command runner."""

    async def model_call(persona, content, max_tokens):
        return ModelReply(content=json.dumps(model_response), finish_reason="stop")

    events = []
    tk = Toolkit(
        metrics=WorkflowMetrics(),
        model_call=model_call,
        command_runner=None,
        screenshot_capture=None,
        dom_reader=None,
        event_sink=events.append,
        call_limit=12,
    )
    return tk, events


def _score(**over):
    base = {
        "title": "Timesheet workflows",
        "description": "Add timesheet submission with lifecycle and approval",
        "acceptance_criteria": ["Users submit", "Admins approve"],
        "clarifications": [],
        "issue_number": 42,
        "workflow_env": {},
    }
    base.update(over)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_clear_card_ready_assessment(tmp_path):
    """US1: a well-written card reads as clear and ready with no questions."""
    tk, events = _stub_toolkit(_CLEAR_ASSESSMENT)
    result = await AssessorWorkflow().run(SimpleNamespace(path=tmp_path), _score(), tk)

    # No command ran
    assert result.metrics.commands_run == 0
    # Assessment is valid and ready
    Assessment.model_validate(result.report["assessment"])
    assert result.report["assessment"]["ready"] is True
    assert result.report["assessment"]["questions"] == []
    assert result.report["assessment"]["criteria_source"] == "card"
    assert result.report["assessment"]["criteria"] == []
    # Four steps in order
    assert [e.text for e in events] == [f"assessor.{s}" for s in STEPS]
    assert set(tk.metrics.step_durations_ms) == set(STEPS)
    assert tk.metrics.model_calls == 1


@pytest.mark.asyncio
async def test_ambiguous_card_blocked_with_questions(tmp_path):
    """US2: an ambiguous card asks the human and blocks."""
    tk, _ = _stub_toolkit(_AMBIGUOUS_ASSESSMENT)
    result = await AssessorWorkflow().run(SimpleNamespace(path=tmp_path), _score(), tk)

    Assessment.model_validate(result.report["assessment"])
    assert result.report["assessment"]["ready"] is False
    assert len(result.report["assessment"]["questions"]) == 1
    assert "audience" in result.report["assessment"]["questions"][0].lower()


@pytest.mark.asyncio
async def test_card_without_criteria_gets_draft(tmp_path):
    """US3: a card with no criteria gets a draft from the assessor."""
    model_response = {
        "ready": True,
        "goal": "Support bulk user imports",
        "expected_behavior": "CSV imports work for large files",
        "out_of_scope": [],
        "questions": [],
        "assumptions": [],
        "criteria": [
            {"surface": "/admin/users", "action": "upload CSV", "expected": "users imported", "kind": "functional"},
            {"surface": "/admin/users", "action": "large file (10k rows)", "expected": "completes in 30s", "kind": "visual"},
        ],
    }
    tk, _ = _stub_toolkit(model_response)
    result = await AssessorWorkflow().run(SimpleNamespace(path=tmp_path), _score(acceptance_criteria=[]), tk)

    assert result.report["assessment"]["criteria_source"] == "assessor"
    assert len(result.report["assessment"]["criteria"]) == 2


@pytest.mark.asyncio
async def test_card_with_criteria_discards_model_draft(tmp_path):
    """When the card has criteria, the model's draft is discarded."""
    model_response = {
        "ready": True,
        "goal": "Fix typo",
        "expected_behavior": "Reads correctly",
        "out_of_scope": [],
        "questions": [],
        "assumptions": [],
        "criteria": [
            {"surface": "/contact", "action": "see text", "expected": "Contact us", "kind": "visual"},
        ],
    }
    tk, _ = _stub_toolkit(model_response)
    result = await AssessorWorkflow().run(SimpleNamespace(path=tmp_path), _score(), tk)

    assert result.report["assessment"]["criteria_source"] == "card"
    assert result.report["assessment"]["criteria"] == []


@pytest.mark.asyncio
async def test_assessor_touches_only_allowed_toolkit_names(tmp_path):
    """FR-003: a spy on the toolkit proves the workflow reaches only for
    call_model, metrics, emit, events, and call_limit. No commit, push, write,
    or run_command. (Note: _command_runner, _screenshot_capture, and _dom_reader
    are checked for the write-free proof, not invoked.)"""
    tk, _ = _stub_toolkit(_CLEAR_ASSESSMENT)
    touched: list[str] = []

    class Spy:
        def __getattr__(self, name):
            touched.append(name)
            return getattr(tk, name)

    await AssessorWorkflow().run(SimpleNamespace(path=tmp_path), _score(), Spy())

    assert touched, "the spy saw nothing; the workflow is not using the toolkit it was given"
    # _command_runner, _screenshot_capture, _dom_reader are checked for write-free proof, not invoked
    assert set(touched) <= {"metrics", "call_model", "events", "emit", "call_limit", "_command_runner", "_screenshot_capture", "_dom_reader"}, touched
    assert not any(any(w in n for w in ("commit", "push", "write", "delete", "run_command")) for n in touched)


@pytest.mark.asyncio
async def test_every_step_is_timed_and_logged(tmp_path, monkeypatch):
    """FR-018: step durations in the metrics, and structured events on the module logger."""
    from performer.workflows import assessor as assessor_mod

    from tests.unit.workflows._fakelog import FakeLog

    fake = FakeLog()
    monkeypatch.setattr(assessor_mod, "log", fake)
    tk, _ = _stub_toolkit(_CLEAR_ASSESSMENT)
    result = await AssessorWorkflow().run(SimpleNamespace(path=tmp_path), _score(), tk)

    durations = tk.metrics.step_durations_ms
    assert list(durations) == list(STEPS), "each step records a duration, in order"
    assert result.report["workflow_metrics"]["step_durations_ms"] == durations
    events = {e["event"]: e for e in fake.entries}
    assert "assessor.assessed" in events
    assert events["assessor.assessed"]["ready"] is True


@pytest.mark.asyncio
async def test_no_path_attribute_on_stand():
    """Stand with no path attribute should not raise AttributeError."""
    tk, _ = _stub_toolkit(_CLEAR_ASSESSMENT)
    stand = SimpleNamespace()
    result = await AssessorWorkflow().run(stand, _score(), tk)

    Assessment.model_validate(result.report["assessment"])
    assert result.report["assessment"]["ready"] is True


@pytest.mark.asyncio
async def test_three_round_clarification_loop(tmp_path):
    """SC-004: a card blocks at most twice, then the third question goes to assumptions.

    Round 1: model asks Q1 -> blocked with 1 question
    Round 2: Q1 answered, model asks Q2 -> blocked with 1 new question
    Round 3: Q1 and Q2 answered, model asks Q3 -> ready, Q3 in assumptions as "assumed: ..."
    """
    # Round 1: no clarifications yet, model asks one question
    round1_response = {
        "ready": False,
        "goal": "Make the services page better",
        "expected_behavior": "Users find services easily",
        "out_of_scope": [],
        "questions": ["Will this require database schema changes?"],
        "assumptions": [],
        "criteria": [],
    }

    # Round 2: Q1 answered, model asks Q2
    round2_response = {
        "ready": False,
        "goal": "Make the services page better",
        "expected_behavior": "Users find services easily",
        "out_of_scope": [],
        "questions": ["Which design system should we follow?"],  # Distinct Q2
        "assumptions": [],
        "criteria": [],
    }

    # Round 3: Q1 and Q2 answered, model asks Q3
    # After 2 answered rounds, gate forces ready, Q3 goes to assumptions
    round3_response = {
        "ready": False,
        "goal": "Make the services page better",
        "expected_behavior": "Users find services easily",
        "out_of_scope": [],
        "questions": ["Should we add analytics tracking?"],  # Distinct Q3
        "assumptions": [],
        "criteria": [],
    }

    # Simulate round 1: no clarifications
    tk, _ = _stub_toolkit(round1_response)
    result = await AssessorWorkflow().run(
        SimpleNamespace(path=tmp_path),
        _score(clarifications=[]),
        tk,
    )
    assert result.report["assessment"]["ready"] is False
    assert len(result.report["assessment"]["questions"]) == 1
    assert "database" in result.report["assessment"]["questions"][0].lower()

    # Simulate round 2: first question answered
    tk, _ = _stub_toolkit(round2_response)
    result = await AssessorWorkflow().run(
        SimpleNamespace(path=tmp_path),
        _score(clarifications=[
            {"question": "Will this require database schema changes?", "answer": "Yes, a new table for settings"},
        ]),
        tk,
    )
    assert result.report["assessment"]["ready"] is False
    assert len(result.report["assessment"]["questions"]) == 1
    assert "design" in result.report["assessment"]["questions"][0].lower()

    # Simulate round 3: both prior questions answered
    tk, _ = _stub_toolkit(round3_response)
    result = await AssessorWorkflow().run(
        SimpleNamespace(path=tmp_path),
        _score(clarifications=[
            {"question": "Will this require database schema changes?", "answer": "Yes, a new table for settings"},
            {"question": "Which design system should we follow?", "answer": "Use Material Design"},
        ]),
        tk,
    )
    # After 2 answered rounds, gate forces ready
    assert result.report["assessment"]["ready"] is True
    # The third question should NOT be in questions
    assert len(result.report["assessment"]["questions"]) == 0
    # The third question should be in assumptions, phrased as "assumed: ..."
    assumptions = result.report["assessment"]["assumptions"]
    assert any("analytics" in a.lower() and "assumed" in a.lower() for a in assumptions), \
        f"analytics not in assumptions as 'assumed: ...': {assumptions}"

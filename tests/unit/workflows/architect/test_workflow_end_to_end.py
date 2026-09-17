"""165 US1 independent test: the whole workflow against a stubbed model.

No writes, budget respected, valid blueprint, size derived by code, five step
events in order, and the report carries what coordinare needs to lift.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from performer.workflows.architect import STEPS, ArchitectWorkflow
from performer.workflows.architect.models import Blueprint
from performer.workflows.architect.report import ArchitectWroteToTree
from performer.workflows.base import SchemaViolation, WorkflowMetrics
from performer.workflows.budget import ModelReply
from performer.workflows.toolkit import Toolkit

_SCHEMA_BP = {
    "summary": "Add timesheet submissions with a lifecycle and an approval endpoint.",
    "milestones": [
        {"goal": "migration + model", "scope": ["db/migrate", "app/models/timesheet_submission.rb"], "done_when": "model specs pass"},
        {"goal": "state machine", "scope": ["app/models"], "done_when": "transitions covered"},
        {"goal": "approval endpoint", "scope": ["app/controllers", "config/routes.rb"], "done_when": "request specs pass"},
    ],
    "modules": [{"path": "app/models/", "note": "new model"}, {"path": "app/controllers/", "note": "approvals"}],
    "data_model": {"changes": [{"kind": "table", "name": "timesheet_submissions", "note": "period, status, submitted_at"}]},
    "interfaces": [{"name": "POST /timesheets/:id/approve", "kind": "endpoint", "contract": "admin only; 200 with status approved"}],
    "risks": ["locking rules interact with time entries"],
    "criteria": [
        {"surface": "/timesheets", "action": "submit the week", "expected": "status becomes submitted", "kind": "functional"},
        {"surface": "/admin/timesheets", "action": "approve", "expected": "status approved, entries locked", "kind": "functional"},
    ],
    "docs": [{"topic": "Timesheet lifecycle", "location": "docs/wiki/time-tracking.md", "say": "states and who moves them"}],
}
_TRIVIAL_BP = {
    "summary": "Fix the typo on the contact page heading.",
    "milestones": [{"goal": "fix copy", "scope": ["app/views/contact/index.html.erb"], "done_when": "heading reads Contact us"}],
    "modules": [{"path": "app/views/contact/", "note": "copy only"}],
    "data_model": {"changes": []}, "interfaces": [], "risks": [],
    "criteria": [{"surface": "/contact", "action": "open", "expected": "heading reads Contact us", "kind": "visual"}],
    "docs": [],
}


def _stub_toolkit(blueprint: dict, proposed: list[str], tree_status: str = ""):
    """A Toolkit whose model answers the survey then the blueprint, and whose
    command runner records every command and reports a (configurable) tree."""
    ran: list[str] = []
    answers = iter([
        json.dumps({"commands": [{"command": c, "reason": "orient"} for c in proposed]}),
        json.dumps(blueprint),
    ])

    async def model_call(persona, content, max_tokens):
        return ModelReply(content=next(answers), finish_reason="stop")

    async def runner(cmd, cwd, timeout_s):
        ran.append(cmd)
        if cmd == "git status --porcelain":
            return 0, tree_status
        return 0, f"output of {cmd}"

    events = []
    tk = Toolkit(metrics=WorkflowMetrics(), model_call=model_call, command_runner=runner,
                 screenshot_capture=None, dom_reader=None, event_sink=events.append, call_limit=12)
    return tk, ran, events


def _score(**over):
    base = {"title": "Schema S4", "description": "timesheet submissions", "acceptance_criteria": ["submit", "approve"],
                "clarifications": [], "issue_number": 163, "workflow_env": {}}
    base.update(over)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_large_card_end_to_end(tmp_path):
    tk, ran, events = _stub_toolkit(_SCHEMA_BP, ["ls app/models", "bundle install", "git log --oneline -5"])
    result = await ArchitectWorkflow().run(SimpleNamespace(path=tmp_path), _score(), tk)

    # nothing but read-only commands and the write-free check ran
    assert ran == ["ls app/models", "git log --oneline -5", "git status --porcelain"]
    # the refused command is recorded
    assert result.report["write_free_check"]["refused_commands"] == 1
    # blueprint validated, size derived by code
    Blueprint.model_validate({k: v for k, v in result.report["blueprint"].items() if k not in ("size", "blueprint_hash", "created_at")})
    assert result.report["size"] == "large" and result.report["blueprint"]["size"] == "large"
    # five steps, in order, as progress events
    assert [e.text for e in events] == [f"architect.{s}" for s in STEPS]
    assert set(tk.metrics.step_durations_ms) == set(STEPS)
    assert tk.metrics.model_calls == 2
    assert result.findings == []


@pytest.mark.asyncio
async def test_trivial_card_is_small_with_no_docs(tmp_path):
    tk, _ran, _ = _stub_toolkit(_TRIVIAL_BP, ["cat app/views/contact/index.html.erb"])
    result = await ArchitectWorkflow().run(SimpleNamespace(path=tmp_path), _score(title="typo"), tk)
    assert result.report["size"] == "small"
    assert result.report["blueprint"]["docs"] == []


@pytest.mark.asyncio
async def test_the_architect_never_writes(tmp_path):
    """No commit, push or file write primitive is ever reached: the toolkit has
    none, and the executed git status is the proof recorded in the report."""
    tk, ran, _ = _stub_toolkit(_TRIVIAL_BP, ["sed -i 's/a/b/' f", "cat f > g", "ls"])
    result = await ArchitectWorkflow().run(SimpleNamespace(path=tmp_path), _score(), tk)
    assert ran == ["ls", "git status --porcelain"]
    assert result.report["write_free_check"]["passed"] is True
    assert result.report["write_free_check"]["refused_commands"] == 2


@pytest.mark.asyncio
async def test_a_dirty_tree_after_the_run_is_an_error_not_a_plan(tmp_path):
    tk, _, _ = _stub_toolkit(_TRIVIAL_BP, ["ls"], tree_status="?? db/migrate/new.rb\n")
    with pytest.raises(ArchitectWroteToTree, match=r"db/migrate/new\.rb"):
        await ArchitectWorkflow().run(SimpleNamespace(path=tmp_path), _score(), tk)


@pytest.mark.asyncio
async def test_a_hollow_blueprint_raises_naming_the_field(tmp_path):
    hollow = {**_TRIVIAL_BP, "milestones": []}
    ran: list[str] = []
    answers = iter([json.dumps({"commands": []}), json.dumps(hollow), json.dumps(hollow)])

    async def model_call(persona, content, max_tokens):
        return ModelReply(content=next(answers), finish_reason="stop")

    async def runner(cmd, cwd, timeout_s):
        ran.append(cmd)
        return 0, ""

    tk = Toolkit(metrics=WorkflowMetrics(), model_call=model_call, command_runner=runner, call_limit=12)
    with pytest.raises(SchemaViolation, match="milestones"):
        await ArchitectWorkflow().run(SimpleNamespace(path=tmp_path), _score(), tk)


@pytest.mark.asyncio
async def test_survey_budget_from_workflow_env(tmp_path):
    tk, ran, _ = _stub_toolkit(_TRIVIAL_BP, [f"ls d{i}" for i in range(10)])
    await ArchitectWorkflow().run(SimpleNamespace(path=tmp_path), _score(workflow_env={"ARCHITECT_SURVEY_MAX_COMMANDS": "3"}), tk)
    assert ran[:-1] == ["ls d0", "ls d1", "ls d2"]


# --- review of #266 ------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_architect_touches_only_read_primitives_on_the_toolkit(tmp_path):
    """T015 as written: a spy on the toolkit proves the workflow reaches for
    call_model, run_command and the metrics only. Any write primitive a
    future Toolkit grows (commit_file, push_branch, write_file) would show up
    here by name."""
    tk, _, _ = _stub_toolkit(_TRIVIAL_BP, ["ls"])
    touched: list[str] = []

    class Spy:
        def __getattr__(self, name):
            touched.append(name)
            return getattr(tk, name)

    await ArchitectWorkflow().run(SimpleNamespace(path=tmp_path), _score(), Spy())
    assert touched, "the spy saw nothing; the workflow is not using the toolkit it was given"
    assert set(touched) <= {"metrics", "call_model", "run_command", "events", "emit", "call_limit"}, touched
    assert not any(any(w in n for w in ("commit", "push", "write", "delete")) for n in touched)


@pytest.mark.asyncio
async def test_every_step_is_timed_and_the_steps_are_logged(tmp_path, monkeypatch):
    """FR-018: step durations in the metrics 164 already reports, and the
    architect's own structured events on the module logger."""
    from performer.workflows import architect as arch_mod

    from tests.unit.workflows._fakelog import FakeLog

    fake = FakeLog()
    monkeypatch.setattr(arch_mod, "log", fake)
    tk, _, _ = _stub_toolkit(_TRIVIAL_BP, ["ls", "cat f > g"])
    result = await ArchitectWorkflow().run(SimpleNamespace(path=tmp_path), _score(), tk)

    durations = tk.metrics.step_durations_ms
    assert list(durations) == list(STEPS), "each step records a duration, in order"
    assert result.report["workflow_metrics"]["step_durations_ms"] == durations
    events = {e["event"]: e for e in fake.entries}
    assert events["architect.survey_done"]["commands"] == 2
    assert events["architect.survey_done"]["refused"] == 1
    assert events["architect.blueprint"]["size"] == "small"

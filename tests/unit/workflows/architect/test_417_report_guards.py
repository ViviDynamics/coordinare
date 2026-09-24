"""417: the write-free proof compares against the pre-run tree state (pre-existing
dirt is not the architect's write), Module.path / Milestone.scope are validated
against the tree (exist, or declared new), and the blueprint plan is size-capped
and degeneracy-checked before the report is emitted (the remaining half of 396)."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from performer.workflows.architect.models import (
    Blueprint,
    Criterion,
    DataModel,
    DataModelChange,
    DocTopic,
    Interface,
    Milestone,
    Module,
)
from performer.workflows.architect.report import (
    DegeneratePlan,
    enforce_plan_guards,
    invalid_blueprint_paths,
    tree_state,
    write_free_check,
)


def _toolkit(outputs: list[str]):
    calls = iter(outputs)

    async def run_command(command, cwd, timeout_s):
        try:
            return SimpleNamespace(exit_code=0, output_excerpt=next(calls))
        except StopIteration:
            return SimpleNamespace(exit_code=0, output_excerpt="")
    return SimpleNamespace(run_command=run_command)


@pytest.mark.asyncio
async def test_pre_existing_dirt_does_not_fail_the_write_free_check():
    toolkit = _toolkit(["?? docs/old.md"])
    check = await write_free_check(toolkit, Path("/ws"), baseline=["docs/old.md"])
    assert check["passed"] is True
    assert check["pre_existing"] == ["docs/old.md"]
    assert check["dirty_paths"] == []


@pytest.mark.asyncio
async def test_new_dirt_fails_even_with_a_baseline():
    toolkit = _toolkit(["?? docs/old.md\n?? plan.md"])
    check = await write_free_check(toolkit, Path("/ws"), baseline=["docs/old.md"])
    assert check["passed"] is False
    assert check["dirty_paths"] == ["plan.md"]


@pytest.mark.asyncio
async def test_without_a_baseline_any_dirt_fails_as_before():
    toolkit = _toolkit(["?? docs/old.md"])
    check = await write_free_check(toolkit, Path("/ws"))
    assert check["passed"] is False


@pytest.mark.asyncio
async def test_tree_state_snapshots_the_pre_run_paths():
    toolkit = _toolkit([" M a.py\n?? b.py"])
    snapshot = await tree_state(toolkit, Path("/ws"))
    assert snapshot == ["a.py", "b.py"]


def _blueprint(modules: list[Module], milestones: list[Milestone] | None = None) -> Blueprint:
    return Blueprint(
        summary="s",
        milestones=milestones or [Milestone(goal="g", scope=["src/app.py"], done_when="d")],
        modules=modules,
        data_model=DataModel(changes=[]),
        interfaces=[],
        risks=[],
        criteria=[Criterion(surface="s", action="a", expected="e", kind="functional")],
        docs=[],
    )


def test_blueprint_paths_that_exist_validate(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x", encoding="utf-8")
    bp = _blueprint([Module(path="src/app.py", note="n")])
    assert invalid_blueprint_paths(bp, tmp_path) == []


def test_declared_new_paths_validate(tmp_path):
    bp = _blueprint(
        [Module(path="new:src/thing.py", note="n")],
        [Milestone(goal="g", scope=["new:src/thing.py", "src/app.py"], done_when="d")],
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x", encoding="utf-8")
    assert invalid_blueprint_paths(bp, tmp_path) == []


def test_hallucinated_paths_are_reported(tmp_path):
    bp = _blueprint(
        [Module(path="src/ghost.py", note="n")],
        [Milestone(goal="g", scope=["src/nope.py"], done_when="d")],
    )
    invalid = invalid_blueprint_paths(bp, tmp_path)
    assert sorted(invalid) == ["src/ghost.py", "src/nope.py"]


def _degenerate_blueprint() -> Blueprint:
    scope = ["src/rep"] * 8
    milestones = [
        Milestone(goal=f"goal {i}", scope=scope, done_when=f"done {i}") for i in range(7)
    ]
    return Blueprint(
        summary="degenerate",
        milestones=milestones,
        modules=[Module(path="src/rep", note="n") for _ in range(12)],
        data_model=DataModel(changes=[]),
        interfaces=[],
        risks=[],
        criteria=[Criterion(surface="s", action="a", expected="e", kind="functional")],
        docs=[],
    )


def test_degenerate_blueprint_plan_is_refused():
    with pytest.raises(DegeneratePlan):
        enforce_plan_guards(_degenerate_blueprint())


def test_worst_case_blueprint_plan_is_refused():
    bp = Blueprint(
        summary="x" * 600,
        milestones=[Milestone(goal="g" * 200, scope=["a" * 160] * 8, done_when="d" * 300) for _ in range(7)],
        modules=[Module(path="p", note="n" * 200) for _ in range(12)],
        data_model=DataModel(changes=[DataModelChange(kind="table", name="t", note="n" * 200)] * 12),
        interfaces=[Interface(name="i", kind="class", contract="c" * 300) for _ in range(12)],
        risks=["r" * 300] * 8,
        criteria=[Criterion(surface="s" * 120, action="a" * 200, expected="e" * 300, kind="functional")] * 12,
        docs=[DocTopic(topic="t", location="l", say="y" * 400) for _ in range(8)],
    )
    with pytest.raises(DegeneratePlan):
        enforce_plan_guards(bp)


def test_the_size_rule_engages_when_the_cap_is_tight():
    bp = _blueprint([Module(path="src/app.py", note="a real note")])
    with pytest.raises(DegeneratePlan):
        enforce_plan_guards(bp, size_cap=10)


def test_a_normal_blueprint_passes_the_plan_guards(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x", encoding="utf-8")
    bp = _blueprint([Module(path="src/app.py", note="a real note")])
    enforce_plan_guards(bp)
    assert invalid_blueprint_paths(bp, tmp_path) == []

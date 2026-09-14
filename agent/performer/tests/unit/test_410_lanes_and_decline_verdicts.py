"""410: the milestone shape admits chore, docs, config and dependency work.

Four fixtures drive the acceptance criteria: a migration-only card, a docs-only
card, a dependency bump and a repository with one pre-existing red test — each
reaching a terminal success. The same file covers the lane vocabulary, the docs
scope rule, the assessor's not_work / needs_split verdicts, and the architect
schema's zero-criteria blueprint and extended kinds.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from performer.workflows.implementer import driver
from performer.workflows.implementer.cycle import scope_violations
from performer.workflows.implementer.models import Baseline as BaselineModel
from performer.workflows.implementer.models import MilestonePlan
from performer.workflows.implementer.observe import TestObservation
from performer.workflows.implementer.plan import LaneNotForImplementer, build_plan, select_lane


# --- the lane vocabulary ------------------------------------------------------

def test_select_lane_maps_the_new_work_kinds():
    assert select_lane("docs", None) == ("docs", "brief")
    assert select_lane("config", None) == ("config", "brief")
    assert select_lane("dependency", None) == ("dependency", "brief")


def test_research_still_ends_at_the_architect():
    with pytest.raises(LaneNotForImplementer):
        select_lane("research", None)


def test_no_brief_lane_comes_from_labels():
    score = SimpleNamespace(labels=["documentation"], acceptance_criteria=[], issue_number=1)
    plans = build_plan(score, None)
    assert plans[0].lane == "docs"
    assert plans[0].lane_source == "labels"


def test_no_brief_without_matching_labels_stays_feature():
    score = SimpleNamespace(labels=["bug", "question"], acceptance_criteria=[], issue_number=1)
    plans = build_plan(score, None)
    assert plans[0].lane == "feature"


# --- the docs scope rule ------------------------------------------------------

def test_docs_paths_are_in_scope_on_the_docs_lane():
    assert scope_violations("implement", {"docs/SETUP.md": "added"}, "", allow_docs=True) == []


def test_docs_paths_stay_out_of_scope_without_the_docs_lane():
    violations = scope_violations("implement", {"docs/SETUP.md": "added"}, "")
    assert violations and violations[0]["kind"] == "reverted_doc"


def test_the_docs_lane_still_writes_no_tests():
    violations = scope_violations("implement", {"tests/test_a.py": "added"}, "", allow_docs=True)
    assert violations and violations[0]["kind"].startswith("reverted_")


# --- the four fixtures, at the driver seam ------------------------------------

def _baseline(fail_count=0, pass_count=5, fail_names=None):
    return BaselineModel(
        test_names=None, test_names_failed=list(fail_names or []),
        pass_count=pass_count, fail_count=fail_count,
        stack="", detected_from="test",
    )


def _summary_ctx(failed=0, passed=1):
    async def run_command(cmd, *, cwd=None, timeout_s=300, plan_check_id=""):
        return SimpleNamespace(exit_code=1 if failed else 0, output_excerpt="out")

    async def call_model(*, persona, schema, content, budget):
        return TestObservation(
            outcome="assertion_failure" if failed else "all_passed",
            failed=failed if isinstance(failed, list) else [f"t{i}" for i in range(failed)],
            passed=["ok"] * passed if not isinstance(passed, list) else passed,
        )

    return SimpleNamespace(run_command=run_command, call_model=call_model, agent_turn=AsyncMock())


def _milestone(lane="chore"):
    return MilestonePlan(
        index=0, goal="do the thing", scope=".", done_when="it is done", lane=lane, lane_source="brief",
    )


async def _run_lane(tmp_path, monkeypatch, lane, changed, *, failed=0, baseline=None):
    """Drive one milestone through the real _change/_tests_lane control flow,
    with the git seams faked and the tests observation faked to `failed`."""
    commits = []

    async def run_agent_turn(payload, timeout_s=0):
        return {
            "exit_state": "done", "output_tail": "ok", "wall_ms": 1,
            "harness_commits": [], "changed_paths": sorted(changed),
        }

    async def squash(workspace, sha):
        return 0

    async def head_sha(workspace):
        return "base123"

    async def changed_paths_since(workspace, sha):
        return dict(changed)

    async def commit_paths(workspace, paths, message):
        commits.append((tuple(paths), message))
        return "sha"

    monkeypatch.setattr(driver.git, "squash_turn_commits", squash)
    monkeypatch.setattr(driver.git, "head_sha", head_sha)
    monkeypatch.setattr(driver.git, "changed_paths_since", changed_paths_since)
    monkeypatch.setattr(driver.git, "commit_paths", commit_paths)

    toolkit = _summary_ctx(failed=failed)
    ctx = driver.RunContext(
        toolkit=SimpleNamespace(run_agent_turn=run_agent_turn, run_command=toolkit.run_command, call_model=toolkit.call_model, agent_turn=AsyncMock()),
        stand=SimpleNamespace(path=str(tmp_path)),
        score=SimpleNamespace(owner_repo=("org", "repo"), effective_github_token="t", branch_name="b"),
        budgets=SimpleNamespace(impl_attempts=3, turn_timeout_s=10, test_timeout_s=10),
        runner_kind="", test_command="pytest",
        baseline=baseline or _baseline(),
    )
    record = driver.PerMilestoneRecord(index=0, goal="do the thing", done_when="it is done", implementation_successful=False)
    milestone = _milestone(lane)
    if lane == "docs":
        await driver._change(ctx, milestone, record, allow_docs=True)
    elif lane == "tests":
        await driver._tests_lane(ctx, milestone, record)
    else:
        await driver._change(ctx, milestone, record)
    return commits, record, ctx


@pytest.mark.asyncio
async def test_migration_only_card_reaches_success(tmp_path, monkeypatch):
    """A schema migration has no failing call site, so it can never ride red/green."""
    commits, record, _ = await _run_lane(
        tmp_path, monkeypatch, "chore",
        {"db/migrate/001_add_users.rb": "added"}, failed=0,
    )
    assert record.implementation_successful is not None
    assert commits, "the milestone must commit"


@pytest.mark.asyncio
async def test_docs_only_card_reaches_success(tmp_path, monkeypatch):
    """A docs card's documentation paths are in scope and the card commits."""
    commits, record, ctx = await _run_lane(
        tmp_path, monkeypatch, "docs",
        {"docs/SETUP.md": "added"}, failed=0,
    )
    assert commits, "a docs-only card must commit, not MilestoneFailed on a doc revert"
    assert ctx.scope_reverts == []


@pytest.mark.asyncio
async def test_dependency_bump_reaches_success(tmp_path, monkeypatch):
    commits, _, _ = await _run_lane(
        tmp_path, monkeypatch, "dependency",
        {"pyproject.toml": "modified", "uv.lock": "modified"}, failed=0,
    )
    assert commits


@pytest.mark.asyncio
async def test_pre_existing_red_does_not_starve_a_refactor(tmp_path, monkeypatch):
    """One already-failing test anywhere used to fail every chore and refactor
    milestone after three attempts: the lanes demanded absolute green."""
    baseline = _baseline(fail_count=1, pass_count=5, fail_names=["t0"])
    commits, _, _ = await _run_lane(
        tmp_path, monkeypatch, "chore",
        {"src/a.py": "modified"}, failed=1, baseline=baseline,
    )
    assert commits, "a failure the card did not cause must not fail the milestone"


@pytest.mark.asyncio
async def test_a_failure_the_card_caused_still_fails_the_change(tmp_path, monkeypatch):
    baseline = _baseline(fail_count=1, pass_count=5, fail_names=["t0"])
    with pytest.raises(driver.MilestoneFailed):
        await _run_lane(tmp_path, monkeypatch, "chore", {"src/a.py": "modified"}, failed=2, baseline=baseline)


@pytest.mark.asyncio
async def test_tests_lane_commits_with_pre_existing_red(tmp_path, monkeypatch):
    """The tests lane verdict is the baseline comparison too: new tests pass
    against the existing code, and a pre-existing red says nothing about them."""
    baseline = _baseline(fail_count=1, pass_count=5, fail_names=["t0"])
    commits, _, _ = await _run_lane(
        tmp_path, monkeypatch, "tests",
        {"tests/test_new.py": "added"}, failed=1, baseline=baseline,
    )
    assert commits


# --- the assessor's verdicts ---------------------------------------------------

def _model_assessment(**overrides):
    from performer.workflows.assessor.models import ModelAssessment

    fields = {
        "ready": True, "goal": "Ship the thing", "expected_behavior": "it works",
        "out_of_scope": [], "questions": [], "assumptions": [], "criteria": [],
    }
    fields.update(overrides)
    return ModelAssessment(**fields)


def test_verdict_defaults_to_work():
    assert _model_assessment().verdict == "work"


def test_not_work_verdict_needs_no_question_even_when_not_ready():
    from performer.workflows.assessor.models import Assessment

    Assessment(
        ready=False, goal="g", expected_behavior="e", out_of_scope=[],
        questions=[], assumptions=[], criteria=[],
        verdict="not_work", criteria_source="assessor",
        clarifications=[], assessment_hash="a" * 64,
    )


def test_work_verdict_still_needs_a_question_when_not_ready():
    from performer.workflows.assessor.models import Assessment

    with pytest.raises(ValueError):
        Assessment(
            ready=False, goal="g", expected_behavior="e", out_of_scope=[],
            questions=[], assumptions=[], criteria=[],
            verdict="work", criteria_source="assessor",
            clarifications=[], assessment_hash="a" * 64,
        )


def test_force_ready_never_overrides_a_non_work_verdict():
    from performer.workflows.assessor.gate import force_ready

    ready, questions, assumptions = force_ready(False, ["how?"], answered_rounds=5, verdict="not_work")
    assert ready is False and questions == ["how?"] and assumptions == []


def test_force_ready_still_applies_to_work():
    from performer.workflows.assessor.gate import force_ready

    ready, _, assumptions = force_ready(False, ["how?"], answered_rounds=2, verdict="work")
    assert ready is True and assumptions == ["assumed: how?"]


def test_ready_with_no_criteria_degrades_instead_of_raising():
    from performer.workflows.assessor.gate import criteria_source

    criteria, source = criteria_source([], [], ready=True)
    assert criteria == [] and source == "assessor"


def test_card_criteria_still_win():
    from performer.workflows.assessor.gate import criteria_source

    criteria, source = criteria_source(["it works"], [], ready=True)
    assert source == "card"


def test_run_gate_carries_the_verdict():
    from performer.workflows.assessor.gate import run_gate

    intake = SimpleNamespace(answered_rounds=0, clarifications=[], criteria=[])
    assessment, _record = run_gate(_model_assessment(verdict="needs_split", ready=False, questions=[]), intake)
    assert assessment.verdict == "needs_split"


# --- the architect schema -------------------------------------------------------

def test_zero_criteria_blueprint_is_valid():
    from performer.workflows.architect.models import Blueprint, DataModel, Milestone, Module

    bp = Blueprint(
        summary="bump the dependency", milestones=[Milestone(goal="bump", scope=["pyproject.toml"], done_when="version moved")],
        modules=[Module(path="pyproject.toml", note="lockfile")], data_model=DataModel(changes=[]),
        interfaces=[], risks=[], criteria=[], docs=[],
    )
    assert bp.criteria == []


def test_size_of_weighs_modules_and_criteria():
    from performer.workflows.architect.models import Blueprint, Criterion, DataModel, Milestone, Module
    from performer.workflows.architect.size import size_of

    def _bp(n_modules, n_criteria):
        return Blueprint(
            summary="s", milestones=[Milestone(goal="g", scope=["."], done_when="d")],
            modules=[Module(path=f"m{i}", note="n") for i in range(n_modules)],
            data_model=DataModel(changes=[]), interfaces=[], risks=[],
            criteria=[
                Criterion(surface=f"s{i}", action="a", expected="e", kind="file_exists")
                for i in range(n_criteria)
            ],
            docs=[],
        )
    assert size_of(_bp(3, 0)) == "small"
    assert size_of(_bp(12, 0)) == "large"
    assert size_of(_bp(1, 2)) == "small"
    assert size_of(_bp(3, 4)) == "large"


def test_extended_kind_vocabularies():
    from performer.workflows.architect.models import Criterion, DataModelChange, Interface

    Criterion(surface="docs/SETUP.md", action="written", expected="the page exists", kind="file_exists")
    Criterion(surface="pytest", action="runs", expected="exit 0", kind="command_exits")
    DataModelChange(kind="protobuf", name="User", note="n")
    Interface(name="UserQuery", kind="graphql", contract="c")
    Interface(name="Button", kind="component", contract="c")
    Interface(name="Bucket", kind="terraform", contract="c")


# --- the brief prose -------------------------------------------------------------

def test_docs_brief_names_the_work_as_documentation():
    from performer.backends._card_docs import _implementation_brief_lines

    brief = {"work_kind": "docs", "milestones": [{"goal": "write SETUP.md", "scope": ["docs/"], "done_when": "page exists"}]}
    text = "\n".join(_implementation_brief_lines(brief, single_turn=False))
    assert "documentation IS the work" in text
    assert "You write code and tests only" not in text


def test_code_brief_keeps_the_prohibition():
    from performer.backends._card_docs import _implementation_brief_lines

    brief = {"work_kind": "feature", "milestones": [{"goal": "g", "scope": ["."], "done_when": "d"}]}
    text = "\n".join(_implementation_brief_lines(brief, single_turn=False))
    assert "You write code and tests only" in text

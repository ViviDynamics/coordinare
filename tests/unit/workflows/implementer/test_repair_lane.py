"""Spec 169 FR-013: the implementer selects the repair lane when review findings
are present: one milestone per file group, no red step, the findings carried
verbatim into a CHANGE-style turn, milestone tests after the turn, then the
usual quality, push, PR and CI path."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from performer.workflows.implementer.personas import render
from performer.workflows.implementer.plan import build_plan, repair_plan, review_findings

from tests.unit.workflows.implementer.test_workflow_end_to_end import (
    Edges,
    _repo,
    _run,
    _score,
    _write,
)


def _finding(path, line, **over):
    base = {"path": path, "line": line, "category": "logic_error", "problem": f"problem at {path}:{line}", "why_blocking": "wrong", "evidence": "x = 1", "origin": "model"}
    base.update(over)
    return base


def _review(findings):
    return {"changed_files": [], "diff_truncated": False, "verdict": "changes_requested", "covered_files": [], "findings": findings}


def test_repair_plan_groups_findings_by_file_in_order():
    score = SimpleNamespace(review_findings=_review([_finding("src/b.py", 3), _finding("src/a.py", 1), _finding("src/b.py", 9, category="style"), {"path": "", "line": 0, "category": "unaddressed_feedback", "problem": "p", "why_blocking": "w", "evidence": "", "origin": "rule"}]))
    plans = repair_plan(score)
    assert [(p.index, p.scope, p.lane, p.lane_source) for p in plans] == [(0, "src/b.py", "repair", "review"), (1, "src/a.py", "repair", "review")]
    assert "2 review finding(s) in src/b.py" in plans[0].goal and "logic_error, style" in plans[0].done_when
    assert len(review_findings(score)) == 4, "the body-anchored finding is kept for the first turn but is not a group"


def test_build_plan_prefers_the_repair_lane_over_the_brief():
    brief = {"work_kind": "feature", "milestones": [{"goal": "m0", "scope": "src/m0.py", "done_when": "d"}]}
    score = SimpleNamespace(review_findings=_review([_finding("src/m0.py", 2)]), workflow_env={}, acceptance_criteria=["x"], issue_number=7)
    plans = build_plan(score, brief)
    assert [p.lane for p in plans] == ["repair"]
    without = build_plan(SimpleNamespace(review_findings=None, workflow_env={}, acceptance_criteria=["x"], issue_number=7), brief)
    assert [p.lane for p in without] == ["feature"]
    empty = build_plan(SimpleNamespace(review_findings=_review([]), workflow_env={}, acceptance_criteria=["x"], issue_number=7), brief)
    assert [p.lane for p in empty] == ["feature"], "an empty findings list is not a repair"


def test_repair_review_persona_carries_the_findings_verbatim():
    text = render("REPAIR_REVIEW", path="src/a.py", findings="- [logic_error] src/a.py:3: off by one")
    assert "src/a.py" in text and "off by one" in text and "Do not edit documentation" in text


@pytest.mark.asyncio
async def test_repair_lane_runs_one_turn_per_group_with_no_red_step_then_hands_off(tmp_path):
    repo, _ = _repo(tmp_path)
    _write(repo, "src/m0.py", "M0 = False\n")
    _write(repo, "src/m1.py", "M1 = False\n")
    from tests.unit.workflows.implementer.test_workflow_end_to_end import _sh
    _sh(["git", "add", "-A"], repo)
    _sh(["git", "-c", "user.email=e@x", "-c", "user.name=t", "commit", "-qm", "seed"], repo)  # CI runners have no git identity

    def turn(repo_path, brief):
        assert "problem at" in brief["persona"], "the findings ride in the persona verbatim"
        path = "src/m0.py" if brief["milestone_index"] == 0 else "src/m1.py"
        _write(repo_path, path, path.split("/")[-1][:-3].upper() + " = True\n")

    from tests.unit.workflows.implementer.test_workflow_end_to_end import Harness
    score = _score(milestones=[{"goal": "ignored", "scope": "src/x.py", "done_when": "d"}], work_kind="feature")
    body = {"path": "", "line": 0, "category": "unaddressed_feedback", "problem": "problem at the pull request body", "why_blocking": "w", "evidence": "", "origin": "rule"}
    score.review_findings = _review([_finding("src/m0.py", 1), _finding("src/m1.py", 1), _finding("src/m0.py", 1, category="style"), body])
    edges = Edges()
    harness = Harness(repo, {"REPAIR_REVIEW": turn})
    report, _toolkit = await _run(repo, score, harness, edges)
    run = report["implementer_run"]
    assert run["status"] == "pr_opened", run
    seen = [(b["persona_kind"], b["milestone_index"], b["kind"]) for b in harness.briefs]
    assert seen == [("REPAIR_REVIEW", 0, "implement"), ("REPAIR_REVIEW", 1, "implement")], seen
    assert run["milestones_planned"] == 2 and run["milestones_completed"] == 2
    from tests.unit.workflows.implementer.test_workflow_end_to_end import _log
    log = _log(repo)
    assert any(line.startswith("fix(#7): Address 2 review finding(s) in src/m0.py") for line in log), log
    assert any(line.startswith("fix(#7): Address 1 review finding(s) in src/m1.py") for line in log), log
    assert edges.pushes >= 1 and edges.pr_opened == 1
    assert "pull request body" in harness.briefs[0]["persona"] and "pull request body" not in harness.briefs[1]["persona"]
    assert [(a["command"], a["passed"]) for a in run["quality_attempts"]] == [("lint", True), ("lint", True)], "the quality set once per repair turn, no redundant final pass"


def test_body_anchored_findings_ride_with_the_first_group_and_traversal_paths_form_none():
    body = {"path": "", "line": 0, "category": "unaddressed_feedback", "problem": "answer the changelog comment", "why_blocking": "w", "evidence": "", "origin": "rule"}
    evil = _finding("../../etc/passwd", 1)
    absolute = _finding("/etc/hosts", 1)
    score = SimpleNamespace(review_findings=_review([body, evil, absolute, _finding("src/a.py", 1)]))
    plans = repair_plan(score)
    assert [p.scope for p in plans] == ["src/a.py"]
    assert len(review_findings(score)) == 4, "the body-anchored finding is kept for the first turn's persona"
    from performer.workflows.implementer.driver import _scope_list
    from performer.workflows.implementer.models import MilestonePlan

    plan = MilestonePlan(index=0, goal="g", scope="docs/README,special.md", done_when="d", lane="repair", lane_source="review")
    assert _scope_list(plan) == ["docs/README,special.md"], "a repair scope is one path, never split on commas"

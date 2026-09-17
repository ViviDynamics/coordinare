"""169 end-to-end scenarios against a stubbed model and a fake GitHub poster:
clean, findings, hallucinated anchor (re-anchor call), prior feedback, truncated
diff (coverage pass), documentation by the implementer, post failure (hold).
The tree stays clean (executed write-free check), one review is posted per run,
and the report carries what coordinare lifts."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from performer.workflows.base import WorkflowMetrics
from performer.workflows.budget import ModelReply
from performer.workflows.reviewer import STATES, ReviewerWorkflow
from performer.workflows.reviewer.models import ReviewRecord
from performer.workflows.toolkit import Toolkit

from tests.unit.workflows.reviewer._fixtures import DIFF, TRUNCATED_DIFF

DIV_FINDING = {"path": "src/calc.py", "line": 6, "category": "logic_error", "problem": "division by zero is unguarded", "why_blocking": "div(1, 0) raises ZeroDivisionError", "evidence": "return a / b"}
SURVEY = {"commands": [{"command": "git log --oneline -3", "reason": "recent history"}]}
OPEN_EXTRA = {"commands": [{"command": "sed -n '1,40p' src/extra.py", "reason": "read the cut file"}]}


class FakeGitHub:
    def __init__(self, fail=False):
        self.reviews = []
        self.fail = fail

    async def post(self, owner, repo, number, *, event, body, comments, token):
        if self.fail:
            raise RuntimeError("502 Bad Gateway")
        self.reviews.append({"number": number, "event": event, "body": body, "comments": comments})
        return {"html_url": f"https://github.com/{owner}/{repo}/pull/{number}#pullrequestreview-{len(self.reviews)}"}


def _toolkit(replies: list[dict]):
    """The model answers in order: survey proposal(s) first, then findings, then re-anchor."""
    state = {"i": 0, "commands": []}

    async def model_call(persona, content, max_tokens):
        reply = replies[min(state["i"], len(replies) - 1)]
        state["i"] += 1
        return ModelReply(content=json.dumps(reply), finish_reason="stop")

    async def runner(cmd, cwd, timeout_s):
        state["commands"].append(cmd)
        if cmd.startswith("git status"):
            return 0, ""
        if "src/extra.py" in cmd:
            return 0, "import os\nX = 1\nY = X + 1\n"
        return 0, "abc123 add div\n"

    events = []
    tk = Toolkit(metrics=WorkflowMetrics(), model_call=model_call, command_runner=runner, event_sink=events.append, call_limit=12)
    return tk, events, state


def _score(**over):
    base = dict(pr_diff=DIFF, relay_feedback=[], implementation_brief={}, title="Add div", description="Divide numbers",
                pr_url="https://github.com/o/r/pull/7", owner_repo=("o", "r"), effective_github_token="tok", backend="codex", model="m", workflow_env={})
    base.update(over)
    return SimpleNamespace(**base)


async def _run(replies, score, gh=None):
    gh = gh or FakeGitHub()
    tk, events, state = _toolkit(replies)
    result = await ReviewerWorkflow(poster=gh.post).run(SimpleNamespace(path=Path("/tmp/x")), score, tk)
    record = ReviewRecord.model_validate(result.report["review"])
    return result, record, gh, events, state


@pytest.mark.asyncio
async def test_clean_diff_is_approved_with_one_comment_review():
    result, record, gh, events, state = await _run([SURVEY, {"findings": [], "dispositions": []}], _score())
    assert record.verdict == "approved" and record.findings == []
    assert [r["event"] for r in gh.reviews] == ["COMMENT"] and gh.reviews[0]["comments"] == []
    assert record.posted_review_url.endswith("review-1")
    assert record.covered_files == ["src/calc.py", "tests/test_calc.py"] and record.coverage_pass_ran is False
    assert result.report["write_free_check"]["passed"] is True and state["commands"][-1].startswith("git status --porcelain")
    steps = [e.text for e in events if e.text.startswith("reviewer.")]
    assert steps == ["reviewer.intake", "reviewer.survey", "reviewer.findings", "reviewer.gate", "reviewer.post", "reviewer.report"]
    assert set(result.metrics.step_durations_ms) >= {"intake", "survey", "findings", "gate", "post", "report"}
    assert result.report["workflow_metrics"]["model_calls"] == 2
    assert set(STATES) >= set(s.split(".", 1)[1] for s in steps)


@pytest.mark.asyncio
async def test_an_anchored_finding_requests_changes_inline():
    result, record, gh, _, _ = await _run([SURVEY, {"findings": [DIV_FINDING], "dispositions": []}], _score())
    assert record.verdict == "changes_requested"
    assert [f.model_dump() for f in record.findings] == [{**DIV_FINDING, "origin": "model", "introduced_by": ""}]
    assert gh.reviews[0]["event"] == "REQUEST_CHANGES"
    assert [(c["path"], c["line"]) for c in gh.reviews[0]["comments"]] == [("src/calc.py", 6)]
    assert result.findings[0]["category"] == "logic_error"
    assert result.report["review"]["findings"][0]["evidence"] == "return a / b"


@pytest.mark.asyncio
async def test_a_hallucinated_anchor_is_dropped_then_reanchored_once():
    bad = {**DIV_FINDING, "line": 99, "evidence": "if b == 0: raise"}
    _result, record, _gh, _, state = await _run([SURVEY, {"findings": [bad], "dispositions": []}, {"findings": [DIV_FINDING], "dispositions": []}], _score())
    assert [f.line for f in record.findings_dropped] == [99]
    assert [f.line for f in record.findings_after_anchor_recheck] == [6]
    assert [f.line for f in record.findings] == [6] and record.verdict == "changes_requested"
    assert state["i"] == 3, "survey, findings, one re-anchor call"


@pytest.mark.asyncio
async def test_a_reanchor_that_still_fails_leaves_a_clean_approval():
    bad = {**DIV_FINDING, "evidence": "made up"}
    _, record, gh, _, state = await _run([SURVEY, {"findings": [bad], "dispositions": []}, {"findings": [bad], "dispositions": []}], _score())
    assert record.findings == [] and record.verdict == "approved" and len(record.findings_dropped) == 1
    assert gh.reviews[0]["event"] == "COMMENT" and state["i"] == 3


@pytest.mark.asyncio
async def test_prior_comments_need_dispositions_or_become_findings():
    relay = [{"id": 501, "path": "src/calc.py", "line": 6, "body": "Guard b == 0", "author_login": "jason"},
             {"body": "Add a changelog entry", "author_login": "jason"}]
    replies = [SURVEY, {"findings": [], "dispositions": [{"prior_comment_id": "501", "status": "fixed"}]}]
    _, record, gh, _, _ = await _run(replies, _score(relay_feedback=relay))
    assert [d.prior_comment_id for d in record.dispositions] == ["501"]
    assert [(f.category, f.path, f.line, f.origin) for f in record.findings] == [("unaddressed_feedback", "", 0, "rule")]
    assert record.verdict == "changes_requested"
    review = gh.reviews[0]
    assert review["event"] == "REQUEST_CHANGES" and review["comments"] == [], "a body-anchored finding is not an inline comment"
    assert "c2" in review["body"] and "changelog" in review["body"] and "Prior comments addressed: 501" in review["body"]


@pytest.mark.asyncio
async def test_a_truncated_diff_runs_the_coverage_pass_and_still_holds_on_a_bare_note():
    # 412 round 12: the bare truncation note accounts for nothing, so the
    # unopenable phantom holds the review even after the coverage pass opens
    # every visible file -- approval needs a note that names the tail.
    _, record, gh, events, state = await _run([SURVEY, OPEN_EXTRA, {"findings": [], "dispositions": []}], _score(pr_diff=TRUNCATED_DIFF))
    assert record.diff_truncated and record.coverage_pass_ran
    assert "src/extra.py" in record.covered_files
    assert record.unread_files == ["<unnamed files beyond the truncated diff>"]
    assert record.verdict == "env_blocked" and gh.reviews == []
    assert "reviewer.coverage" in [e.text for e in events]
    assert any("src/extra.py" in c for c in state["commands"])


@pytest.mark.asyncio
async def test_unread_files_after_the_coverage_pass_hold_instead_of_approving():
    _, record, gh, _, _ = await _run([SURVEY, SURVEY, {"findings": [], "dispositions": []}], _score(pr_diff=TRUNCATED_DIFF))
    assert record.verdict == "env_blocked" and record.unread_files == ["src/extra.py", "<unnamed files beyond the truncated diff>"]
    assert gh.reviews == [], "no review is posted on a hold"


@pytest.mark.asyncio
async def test_a_surveyed_file_anchors_a_finding_outside_the_hunks_in_the_body():
    deep = {"path": "src/extra.py", "line": 30, "category": "logic_error", "problem": "Y is unused", "why_blocking": "dead code", "evidence": "Y = X + 1"}
    _, record, gh, _, _ = await _run([SURVEY, OPEN_EXTRA, {"findings": [deep], "dispositions": []}], _score(pr_diff=TRUNCATED_DIFF))
    assert [f.path for f in record.findings] == ["src/extra.py"]
    review = gh.reviews[0]
    assert review["event"] == "REQUEST_CHANGES" and review["comments"] == [] and "`src/extra.py:30`" in review["body"]


@pytest.mark.asyncio
async def test_documentation_edits_under_a_brief_are_a_finding():
    docs_diff = DIFF + "diff --git a/docs/usage.md b/docs/usage.md\n--- a/docs/usage.md\n+++ b/docs/usage.md\n@@ -1,2 +1,3 @@\n # Usage\n+Call div.\n"
    brief = {"work_kind": "feature", "milestones": [{"goal": "add div"}]}
    _, record, gh, _, _ = await _run([SURVEY, {"findings": [], "dispositions": []}], _score(pr_diff=docs_diff, implementation_brief=brief))
    assert [(f.category, f.path, f.line) for f in record.findings] == [("documentation_by_implementer", "docs/usage.md", 1)]
    assert gh.reviews[0]["event"] == "REQUEST_CHANGES" and [(c["path"], c["line"]) for c in gh.reviews[0]["comments"]] == [("docs/usage.md", 1)]
    _, without, _, _, _ = await _run([SURVEY, {"findings": [], "dispositions": []}], _score(pr_diff=docs_diff))
    assert without.verdict == "approved", "no brief, no documentation rule"


@pytest.mark.asyncio
async def test_a_failed_post_is_an_environment_hold_with_the_error_recorded():
    _, record, _gh, _, _ = await _run([SURVEY, {"findings": [DIV_FINDING], "dispositions": []}], _score(), gh=FakeGitHub(fail=True))
    assert record.verdict == "env_blocked" and "502" in record.post_error
    assert len(record.findings) == 1, "the findings stay on the record for the operator; coordinare lifts nothing on a hold"


@pytest.mark.asyncio
async def test_a_dirty_tree_fails_the_round():
    from performer.workflows.reviewer.report import ReviewerWroteToTree

    calls = {"n": 0}

    async def model(persona, content, max_tokens):
        calls["n"] += 1
        return ModelReply(content=json.dumps(SURVEY if calls["n"] == 1 else {"findings": [], "dispositions": []}), finish_reason="stop")

    async def runner(cmd, cwd, timeout_s):
        return (0, " M src/calc.py\n") if cmd.startswith("git status") else (0, "ok")

    tk = Toolkit(metrics=WorkflowMetrics(), model_call=model, command_runner=runner, call_limit=12)
    with pytest.raises(ReviewerWroteToTree):
        await ReviewerWorkflow(poster=FakeGitHub().post).run(SimpleNamespace(path=Path("/tmp/x")), _score(), tk)


@pytest.mark.asyncio
async def test_the_categories_are_a_workflow_parameter():
    own = ("injection", "secrets")
    sec = {"path": "src/calc.py", "line": 6, "category": "secrets", "problem": "p", "why_blocking": "w", "evidence": "return a / b"}
    gh = FakeGitHub()
    tk, _, _ = _toolkit([SURVEY, {"findings": [sec], "dispositions": []}])
    result = await ReviewerWorkflow(categories=own, poster=gh.post).run(SimpleNamespace(path=Path("/tmp/x")), _score(), tk)
    assert result.report["review"]["findings"][0]["category"] == "secrets"


@pytest.mark.asyncio
async def test_every_step_is_timed_and_logged(monkeypatch):
    """FR-016: step durations in the metrics, and structured events on the module logger."""
    from performer.workflows import reviewer as reviewer_mod

    from tests.unit.workflows._fakelog import FakeLog

    fake = FakeLog()
    monkeypatch.setattr(reviewer_mod, "log", fake)
    result, _record, _gh, _, _ = await _run([SURVEY, {"findings": [DIV_FINDING], "dispositions": []}], _score())
    durations = result.metrics.step_durations_ms
    assert list(durations) == ["intake", "survey", "findings", "gate", "post", "report"], "each step records a duration, in order"
    assert result.report["workflow_metrics"]["step_durations_ms"] == durations
    assert result.report["workflow_metrics"]["model_calls"] == 2
    events = {e["event"]: e for e in fake.entries}
    assert events["reviewer.intake"]["files"] == 2
    assert events["reviewer.gate"]["kept"] == 1 and events["reviewer.gate"]["verdict"] == "changes_requested"


@pytest.mark.asyncio
async def test_a_surveyed_unchanged_caller_is_reported_in_review_body():
    caller = {"path": "src/extra.py", "introduced_by": "src/calc.py", "line": 3,
              "category": "logic_error", "problem": "Caller assumes the old result",
              "why_blocking": "New result breaks this unchanged caller", "evidence": "Y = X + 1"}
    _, record, gh, _, _ = await _run([OPEN_EXTRA, {"findings": [caller], "dispositions": []}], _score())
    assert record.verdict == "changes_requested"
    assert [(f.path, f.introduced_by) for f in record.findings] == [("src/extra.py", "src/calc.py")]
    assert gh.reviews[0]["comments"] == []
    assert "`src/extra.py:3`" in gh.reviews[0]["body"]

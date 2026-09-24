"""414: the CI gate and the closer gate must not report success on nothing.

Covers the zero-check-runs false green, commit statuses alongside check runs,
check-run pagination, the cancelled-run supersede rule, outcome-based
no-progress, hook-failed commits, and the closer's resolve-before-post order
plus the quote-authority rule.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from structlog.testing import capture_logs

from performer.workflows.base import WorkflowMetrics
from performer.workflows.budget import ModelReply
from performer.workflows.implementer.ci import CINoCI, CIPending, classify_check_runs, run_ci_phase
from performer.workflows.implementer.cycle import no_progress_check
from performer.workflows.toolkit import Toolkit


def _ctx(runs, statuses=None, budgets=None):
    return SimpleNamespace(
        get_check_runs=AsyncMock(return_value=runs),
        get_commit_statuses=AsyncMock(return_value=statuses) if statuses is not None else None,
        github_api_calls=0,
        budgets=budgets or SimpleNamespace(ci_wait_s=1, ci_repairs=3, ci_no_checks_floor_s=0),
        sleep=AsyncMock(),
        poll_interval_s=0,
        push=AsyncMock(),
        toolkit=SimpleNamespace(agent_turn=AsyncMock()),
    )


class _Clock:
    """Fake monotonic clock advanced by the poller's sleeps."""

    def __init__(self) -> None:
        self.t = 0.0

    def monotonic(self) -> float:
        return self.t


def _patched(ctx, monkeypatch):
    import performer.workflows.implementer.ci as ci

    clock = _Clock()

    async def sleep(_s: float) -> None:
        clock.t += 10.0

    ctx.sleep = sleep
    monkeypatch.setattr(ci, "time", clock)
    return clock


# --- the zero-checks false green -------------------------------------------------


def test_zero_check_runs_classifies_no_checks_not_pass():
    verdict, failed, pending = classify_check_runs([])
    assert verdict == "no_checks"
    assert failed == []
    assert pending == []


def test_none_check_runs_classifies_no_checks_not_pass():
    assert classify_check_runs(None)[0] == "no_checks"


@pytest.mark.asyncio
async def test_run_ci_phase_reports_no_ci_after_the_floor(monkeypatch):
    """No CI configured: after the floor wait elapses, CINoCI — never a green handoff."""
    ctx = _ctx([], budgets=SimpleNamespace(ci_wait_s=300, ci_repairs=3, ci_no_checks_floor_s=5))
    _patched(ctx, monkeypatch)
    with pytest.raises(CINoCI):
        await run_ci_phase(ctx, head_sha="abc", quality=[], rerun_green=AsyncMock())
    assert ctx.get_check_runs.await_count == 2


@pytest.mark.asyncio
async def test_zero_runs_pending_past_wait_budget_is_still_no_ci(monkeypatch):
    """Nothing is pending when there are no checks at all: the wait budget running out is no_ci, not CIPending."""
    ctx = _ctx([], budgets=SimpleNamespace(ci_wait_s=5, ci_repairs=3, ci_no_checks_floor_s=60))
    _patched(ctx, monkeypatch)
    with pytest.raises(CINoCI):
        await run_ci_phase(ctx, head_sha="abc", quality=[], rerun_green=AsyncMock())


@pytest.mark.asyncio
async def test_pending_checks_past_wait_budget_still_raises_ci_pending(monkeypatch):
    runs = [{"name": "test", "status": "pending", "conclusion": None}]
    ctx = _ctx(runs, budgets=SimpleNamespace(ci_wait_s=5, ci_repairs=3, ci_no_checks_floor_s=60))
    _patched(ctx, monkeypatch)
    with pytest.raises(CIPending):
        await run_ci_phase(ctx, head_sha="abc", quality=[], rerun_green=AsyncMock())


@pytest.mark.asyncio
async def test_status_api_outage_with_no_runs_is_an_env_hold(monkeypatch):
    """With zero check runs a failed status fetch is indistinguishable from no CI — hold instead of no_ci."""
    from performer.infrastructure import CIInfrastructureBlocked

    ctx = _ctx([], statuses=None, budgets=SimpleNamespace(ci_wait_s=300, ci_repairs=3, ci_no_checks_floor_s=5))
    ctx.get_commit_statuses = AsyncMock(side_effect=RuntimeError("500 server error"))
    _patched(ctx, monkeypatch)
    with pytest.raises(CIInfrastructureBlocked):
        await run_ci_phase(ctx, head_sha="abc", quality=[], rerun_green=AsyncMock())


# --- commit statuses count as checks ---------------------------------------------


def test_status_failure_is_a_failure():
    verdict, failed, _pending = classify_check_runs([], statuses=[{"state": "failure", "context": "ci/jenkins"}])
    assert verdict == "fail"
    assert [r["name"] for r in failed] == ["ci/jenkins"]


def test_status_error_is_a_failure_too():
    verdict, failed, _pending = classify_check_runs([], statuses=[{"state": "error", "context": "ci/travis"}])
    assert verdict == "fail"
    assert [r["name"] for r in failed] == ["ci/travis"]


def test_status_pending_is_pending():
    assert classify_check_runs([], statuses=[{"state": "pending", "context": "ci/circle"}])[0] == "pending"


def test_status_success_with_no_runs_is_pass():
    assert classify_check_runs([], statuses=[{"state": "success", "context": "ci/circle"}])[0] == "pass"


def test_check_run_failure_outranks_status_success():
    verdict, failed, _ = classify_check_runs(
        [{"name": "build", "status": "completed", "conclusion": "failure"}],
        statuses=[{"state": "success", "context": "ci/circle"}],
    )
    assert verdict == "fail"
    assert [r["name"] for r in failed] == ["build"]


# --- cancelled runs are superseded by newer runs of the same name ----------------


def test_cancelled_run_superseded_by_newer_pending_run_is_pending():
    runs = [
        {"name": "test", "status": "completed", "conclusion": "cancelled", "started_at": "2026-09-01T10:00:00Z"},
        {"name": "test", "status": "pending", "conclusion": None, "started_at": "2026-09-01T11:00:00Z"},
    ]
    assert classify_check_runs(runs)[0] == "pending"


def test_cancelled_run_superseded_by_newer_success_run_is_pass():
    runs = [
        {"name": "test", "status": "completed", "conclusion": "cancelled", "started_at": "2026-09-01T10:00:00Z"},
        {"name": "test", "status": "completed", "conclusion": "success", "started_at": "2026-09-01T11:00:00Z"},
    ]
    assert classify_check_runs(runs)[0] == "pass"


def test_cancelled_run_without_a_newer_run_still_fails():
    runs = [{"name": "test", "status": "completed", "conclusion": "cancelled", "started_at": "2026-09-01T10:00:00Z"}]
    assert classify_check_runs(runs)[0] == "fail"


def test_queued_run_without_started_at_still_supersedes_an_older_cancelled_run():
    """started_at is nullable for queued runs: fall back to created_at so the
    newer queued run wins the supersede instead of the cancelled one."""
    runs = [
        {"name": "test", "status": "completed", "conclusion": "cancelled",
         "started_at": "2026-09-01T10:00:00Z", "created_at": "2026-09-01T09:59:00Z"},
        {"name": "test", "status": "queued", "conclusion": None,
         "started_at": None, "created_at": "2026-09-01T11:00:00Z"},
    ]
    assert classify_check_runs(runs)[0] == "pending"


# --- no progress compares outcomes, not names ------------------------------------


def test_signature_distinguishes_runs_that_differ_only_in_output_text():
    from performer.workflows.implementer.ci import _signature

    same_ground = {"name": "build", "output": {"title": "T", "summary": "S", "text": "first failure detail"}}
    other_text = {"name": "build", "output": {"title": "T", "summary": "S", "text": "different failure detail"}}
    assert _signature(same_ground) != _signature(other_text)


def test_no_progress_false_when_the_ground_changed():
    prior = ["build: TypeError: None is not a function"]
    current = ["build: ImportError: no module named left_pad"]
    assert no_progress_check(prior, current) is False


def test_no_progress_true_when_names_and_ground_match():
    assert no_progress_check(["build: TypeError: boom"], ["build: TypeError: boom"]) is True


def test_no_progress_false_when_a_check_goes_green():
    assert no_progress_check(["build: TypeError: boom", "lint: E501"], ["lint: E501"]) is False


# --- pagination ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_check_runs_follows_pagination():
    import respx

    from performer.github import get_check_runs

    root = "https://api.github.com/repos/org/repo"
    first = {"total_count": 102, "check_runs": [
        {"id": i, "name": f"job{i}", "status": "completed", "conclusion": "success"} for i in range(100)]}
    second = {"total_count": 102, "check_runs": [
        {"id": 100 + i, "name": f"job{100 + i}", "status": "completed", "conclusion": "success"} for i in range(2)]}
    with respx.mock:
        respx.get(f"{root}/commits/abc/check-runs", params={"page": "1", "per_page": "100"}).respond(200, json=first)
        respx.get(f"{root}/commits/abc/check-runs", params={"page": "2", "per_page": "100"}).respond(200, json=second)
        runs = await get_check_runs("org", "repo", "abc", "tok")
    assert [r["name"] for r in runs] == [f"job{i}" for i in range(102)]


@pytest.mark.asyncio
async def test_get_commit_statuses_follows_pagination():
    """The combined /status view caps at 30 contexts; the history endpoint is paginated."""
    import respx

    from performer.github import get_commit_statuses

    root = "https://api.github.com/repos/org/repo"
    first = [{"state": "success", "context": f"ci/{i}", "created_at": "2026-09-01T10:00:00Z"} for i in range(100)]
    second = [{"state": "failure", "context": "ci/fail-late", "created_at": "2026-09-01T11:00:00Z"}]
    with respx.mock:
        respx.get(f"{root}/statuses/abc", params={"page": "1", "per_page": "100"}).respond(200, json=first)
        respx.get(f"{root}/statuses/abc", params={"page": "2", "per_page": "100"}).respond(200, json=second)
        statuses = await get_commit_statuses("org", "repo", "abc", "tok")
    assert len(statuses) == 101
    assert statuses[-1]["context"] == "ci/fail-late"


# --- commit hooks ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_commit_under_failing_hook_retries_no_verify(tmp_path):
    """A hook-failed commit is bounded: retry with --no-verify and record the hook output."""
    import subprocess

    from performer.workflows.implementer.commits import commit_paths

    def git(*args):
        return subprocess.run(["git", *args], cwd=tmp_path, capture_output=True, text=True)

    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    hook = tmp_path / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho 'hook says no' >&2\nexit 1\n")
    hook.chmod(0o755)
    (tmp_path / "f.txt").write_text("hello\n")
    git("add", "f.txt")

    with capture_logs() as logs:
        sha = await commit_paths(tmp_path, ["f.txt"], "wip: salvage")
    assert sha is not None
    assert git("log", "-1", "--pretty=%s").stdout.strip() == "wip: salvage"
    assert any("hook says no" in str(entry.get("stderr", "")) for entry in logs)


# --- closer: resolve before post -------------------------------------------------


def _thread(tid, *comments, **over):
    from performer.workflows.closer.models import Thread

    base = {"id": tid, "path": "src/app.py", "line": 10, "resolved": False, "outdated": False, "comments": list(comments)}
    base.update(over)
    return Thread.model_validate(base)


ASK = {"author": "reviewer", "body": "This needs a guard for the empty case.", "created_at": "2026-09-07T10:00:00Z", "author_association": "NONE"}
REPLY = {"author": "implementer", "body": "Added the guard in commit abc123.", "created_at": "2026-09-07T11:00:00Z", "author_association": "NONE"}
CONFIRM = {"author": "reviewer", "body": "Confirmed, works now. Verified.", "created_at": "2026-09-07T12:00:00Z", "author_association": "NONE"}
CONFIRM2 = {"author": "reviewer", "body": "Second one verified as well.", "created_at": "2026-09-07T12:30:00Z", "author_association": "NONE"}


def _toolkit(judgements: list[dict]) -> Toolkit:
    async def model_call(persona, content, max_tokens):
        return ModelReply(content=json.dumps({"judgements": judgements}), finish_reason="stop")

    return Toolkit(metrics=WorkflowMetrics(), model_call=model_call, command_runner=None, call_limit=8)


def _run_env(threads, review_context=None):
    gh = _FakeGH(threads, review_context or {})
    score = SimpleNamespace(path="/tmp/x", pr_url="https://github.com/o/r/pull/7", owner_repo=("o", "r"),
                            effective_github_token="tok", backend="codex", model="m", workflow_env={}, head_sha="abc1234")
    return gh, score


class _FakeGH:
    def __init__(self, threads, review_context) -> None:
        self.threads, self.review_context = threads, review_context
        self.resolved: list[str] = []
        self.reviews: list[dict] = []

    async def fetcher(self, score, budgets):
        return list(self.threads), 1, dict(self.review_context)

    async def resolver(self, score, ids: list[str]):
        self.resolved.extend(ids)
        return ids, []

    async def poster(self, owner, repo, number, *, event, body, comments, token):
        self.reviews.append({"event": event, "body": body, "comments": comments, "number": number})
        return {"html_url": f"https://github.com/{owner}/{repo}/pull/{number}#review-{len(self.reviews)}"}


@pytest.mark.asyncio
async def test_partial_resolve_posts_changes_requested_with_the_remaining_list():
    """One of two resolutions fails: the posted review says CHANGES REQUESTED and lists the thread still open."""
    from performer.workflows.closer import CloserWorkflow

    t1 = _thread("t1", ASK, REPLY, CONFIRM)
    t2 = _thread("t2", ASK, REPLY, CONFIRM2)
    gh, score = _run_env([t1, t2], review_context={"pr_author": "implementer", "review_decision": "REVIEW_REQUIRED", "changes_requested_humans": []})
    judgements = [
        {"thread_id": "t1", "addressed": True, "quote": "Confirmed, works now", "reason": ""},
        {"thread_id": "t2", "addressed": True, "quote": "Second one verified as well", "reason": ""},
    ]
    result = await CloserWorkflow(fetcher=gh.fetcher, resolver=_fail_second(gh), poster=gh.poster).run(
        SimpleNamespace(path="/tmp/x"), score, _toolkit(judgements))
    record = result.report["closing"]
    assert gh.resolved == ["t1"], "only the thread that actually resolved is recorded"
    assert record["verdict"] == "env_blocked"
    posted = gh.reviews[0]["body"]
    assert "CHANGES REQUESTED" in posted
    assert "This needs a guard for the empty case" in posted


def _fail_second(gh):
    async def resolver(score, ids):
        ok = [i for i in ids if i != "t2"]
        failures = [{"id": i, "error": "could not resolve"} for i in ids if i == "t2"]
        gh.resolved.extend(ok)
        return ok, failures

    return resolver


@pytest.mark.asyncio
async def test_full_resolve_posts_the_truthful_resolved_list():
    from performer.workflows.closer import CloserWorkflow

    t1 = _thread("t1", ASK, REPLY, CONFIRM)
    gh, score = _run_env([t1], review_context={"pr_author": "implementer", "review_decision": "REVIEW_REQUIRED", "changes_requested_humans": []})
    judgements = [{"thread_id": "t1", "addressed": True, "quote": "Confirmed, works now", "reason": ""}]
    result = await CloserWorkflow(fetcher=gh.fetcher, resolver=gh.resolver, poster=gh.poster).run(
        SimpleNamespace(path="/tmp/x"), score, _toolkit(judgements))
    record = result.report["closing"]
    assert record["verdict"] == "approved"
    posted = gh.reviews[0]["body"]
    assert "APPROVED" in posted
    assert "Resolved by this run (1)" in posted


@pytest.mark.asyncio
async def test_review_context_fetch_failure_holds_the_run(monkeypatch):
    """A failed review-context fetch must hold the run (fail closed), never degrade to an empty context."""
    import performer.github as gh_mod
    from performer.workflows.closer import CloserWorkflow

    gh, score = _run_env([], review_context={})

    async def stub_threads(owner, repo, number, token, max_pages):
        return [], 1

    async def boom(owner, repo, number, token):
        raise RuntimeError("502 bad gateway")

    monkeypatch.setattr(gh_mod, "fetch_review_threads", stub_threads)
    monkeypatch.setattr(gh_mod, "fetch_pr_review_context", boom)
    result = await CloserWorkflow(resolver=gh.resolver, poster=gh.poster).run(
        SimpleNamespace(path="/tmp/x"), score, _toolkit([]))
    record = result.report["closing"]
    assert record["verdict"] == "env_blocked"
    assert "502 bad gateway" in str(record["hold_reason"])
    assert gh.reviews == [], "no review is posted on a failed evidence fetch"


# --- quote authority -------------------------------------------------------------


def test_author_cannot_acquiesce_a_human_raised_thread():
    from performer.workflows.closer.gate import accept_judgement

    t = _thread("t1", ASK, REPLY)
    j = SimpleNamespace(thread_id="t1", addressed=True, quote="Added the guard in commit abc123", reason="")
    accepted, reason = accept_judgement(j, ["t1"], {"t1": t})
    assert accepted is False
    assert reason == "quote_not_from_authority"


def test_raiser_confirmation_is_authority():
    from performer.workflows.closer.gate import accept_judgement

    t = _thread("t1", ASK, REPLY, CONFIRM)
    j = SimpleNamespace(thread_id="t1", addressed=True, quote="Confirmed, works now", reason="")
    accepted, reason = accept_judgement(j, ["t1"], {"t1": t})
    assert accepted is True
    assert reason is None


def test_maintainer_confirmation_is_authority():
    from performer.workflows.closer.gate import accept_judgement

    maintainer = {"author": "lead", "body": "Agreed, the guard is in.", "created_at": "2026-09-07T12:00:00Z", "author_association": "MEMBER"}
    t = _thread("t1", ASK, maintainer)
    j = SimpleNamespace(thread_id="t1", addressed=True, quote="Agreed, the guard is in.", reason="")
    accepted, _reason = accept_judgement(j, ["t1"], {"t1": t})
    assert accepted is True


def test_author_reply_suffices_for_a_bot_raised_thread():
    from performer.workflows.closer.gate import accept_judgement

    bot = {"author": "lint-bot[bot]", "body": "Unused import detected.", "created_at": "2026-09-07T10:00:00Z", "author_association": "NONE"}
    reply = {"author": "implementer", "body": "Removed the import.", "created_at": "2026-09-07T11:00:00Z", "author_association": "NONE"}
    t = _thread("t1", bot, reply)
    j = SimpleNamespace(thread_id="t1", addressed=True, quote="Removed the import.", reason="")
    accepted, reason = accept_judgement(j, ["t1"], {"t1": t})
    assert accepted is True
    assert reason is None


def test_thread_comment_bot_flag():
    from performer.workflows.closer.models import ThreadComment

    assert ThreadComment(author="ci[bot]", body="x", created_at="2026-01-01T00:00:00Z").is_bot
    assert not ThreadComment(author="jason", body="x", created_at="2026-01-01T00:00:00Z").is_bot


def test_author_maintainer_cannot_close_a_human_raised_thread():
    """The PR author's reply closes nothing — even when the author is a MEMBER."""
    from performer.workflows.closer.gate import accept_judgement

    maintainer = {"author": "lead", "body": "Agreed, the guard is in.", "created_at": "2026-09-07T12:00:00Z", "author_association": "MEMBER"}
    t = _thread("t1", ASK, maintainer)
    j = SimpleNamespace(thread_id="t1", addressed=True, quote="Agreed, the guard is in.", reason="")
    accepted, reason = accept_judgement(j, ["t1"], {"t1": t}, pr_author="lead")
    assert accepted is False
    assert reason == "quote_not_from_authority"


# --- a human CHANGES_REQUESTED review keeps the verdict --------------------------


def test_human_changes_requested_review_forces_changes_requested():
    from performer.workflows.closer.gate import run_gate

    outcome = run_gate([], [], [], {}, review_context={"pr_author": "implementer", "changes_requested_humans": ["lead"]})
    assert outcome.verdict == "changes_requested"


def test_bot_changes_requested_review_does_not_force_changes_requested():
    from performer.workflows.closer.gate import run_gate

    outcome = run_gate([], [], [], {}, review_context={"pr_author": "implementer", "changes_requested_humans": ["lint-bot[bot]"]})
    assert outcome.verdict == "approved"


def test_stale_changes_requested_cleared_by_an_approved_review_decision():
    """reviewDecision APPROVED means no human request-for-changes is outstanding;
    the historical CHANGES_REQUESTED reviews must not block the verdict."""
    from performer.workflows.closer.gate import run_gate

    context = {"pr_author": "implementer", "review_decision": "APPROVED", "changes_requested_humans": ["lead"]}
    outcome = run_gate([], [], [], {}, review_context=context)
    assert outcome.verdict == "approved"


def test_changes_requested_without_an_approved_decision_still_blocks():
    from performer.workflows.closer.gate import run_gate

    context = {"pr_author": "implementer", "review_decision": "CHANGES_REQUESTED", "changes_requested_humans": ["lead"]}
    outcome = run_gate([], [], [], {}, review_context=context)
    assert outcome.verdict == "changes_requested"

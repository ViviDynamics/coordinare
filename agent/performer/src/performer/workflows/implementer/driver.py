"""The milestone driver of the implementer workflow (spec 167 FR-005 to FR-009, FR-020 to FR-023).

One :class:`RunContext` carries everything a phase needs (toolkit, stand,
score, budgets, the baseline, the record under construction) plus the
injectable callables the workflow uses to touch the outside world (push,
open the PR, poll checks, fetch a log, run the local gate, sleep), so the
state-machine tests drive a real repository with fakes for the network.

:func:`run_milestone` runs one milestone in the lane the plan chose:

* ``feature``: tests turn, observed red, implementation turn, observed green.
* ``bug``: one write-free investigation turn, then the feature cycle with the
  note carried into the briefs and the red step framed as the reproduction.
* ``chore`` and ``refactor``: one change turn, verified by the baseline.
* ``tests``: one tests turn with the inverted check (the new tests must pass).

Every turn is followed by the same housekeeping: harness commits are squashed
into the turn, out-of-scope paths are reverted and recorded, and a turn that
timed out or errored leaves the tree exactly as it was.
"""
from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from performer.backends._card_docs import completed_documentation_prompt_section
from performer.noise_paths import AGENT_CONFIG_DIRS
from performer.test_results import TestSummary
from performer.workflows.implementer import commits as git
from performer.workflows.implementer.baseline import regressions, run_tests
from performer.workflows.implementer.scoping import scoped_command
from performer.workflows.implementer.budgets import ImplementerBudgets
from performer.workflows.implementer.cycle import (
    changed_test_files,
    green_check,
    scope_violations,
)
from performer.workflows.implementer.resume import present_paths, resume_state, scope_segments
from performer.workflows.implementer.models import (
    Baseline,
    MilestonePlan,
    PerMilestoneRecord,
    PerTurnAttempt,
    SatisfiedBy,
    TurnBrief,
    TurnResult,
)
from performer.workflows.implementer.personas import render

log = structlog.get_logger(__name__)

__all__ = ["RunContext", "MilestoneFailed", "RedOutcome", "run_milestone", "run_turn", "DOCS_TREE"]

DOCS_TREE = "docs/"


@dataclass
class RedOutcome:
    """What the red step of one milestone produced (spec 171 FR-011).

    ``satisfied_by`` is set only when the milestone needs no implementation
    turn because a previous run of this card already did the work; ``files``,
    ``summary`` and ``changed`` are then the evidence for that.
    """

    files: list[str]
    summary: TestSummary
    changed: dict[str, str]
    satisfied_by: SatisfiedBy | None = None


class MilestoneFailed(Exception):
    """A milestone could not be completed within its caps (FR-009)."""

    def __init__(self, index: int, goal: str, reason: str) -> None:
        super().__init__(f"milestone {index} ({goal}): {reason}")
        self.index = index
        self.goal = goal
        self.reason = reason


async def _noop_async(*_args: Any, **_kwargs: Any) -> None:
    return None


@dataclass
class RunContext:
    """Everything one run needs, with the outside world injectable."""

    toolkit: Any
    stand: Any
    score: Any
    budgets: ImplementerBudgets
    runner_kind: str
    test_command: str
    baseline: Baseline
    plans: list[MilestonePlan] = field(default_factory=list)
    lane: str = "feature"
    lane_source: str = "unknown"
    issue_number: int = 0
    test_timeout_s: int = 600
    # accumulated record
    turn_attempts: list[PerTurnAttempt] = field(default_factory=list)
    scope_reverts: list[dict[str, str]] = field(default_factory=list)
    milestone_records: list[PerMilestoneRecord] = field(default_factory=list)
    github_api_calls: int = 0
    investigation_note: str | None = None
    investigated: bool = False
    # 171: the paths this card's own earlier runs committed on this branch. Empty
    # on a fresh branch, which leaves every resume rule inert.
    prior_paths: frozenset[str] = frozenset()
    # 379: scoped test command per milestone test-file set, asked once each.
    scoped_commands: dict[tuple[str, ...], str | None] = field(default_factory=dict)
    # 393: whether a failed milestone's work was committed and pushed
    # instead of being reset away.
    work_salvaged: bool = False
    pr_node_id: str | None = None
    # injectable edges (the workflow fills the defaults; tests fake them)
    push: Callable[[], Awaitable[None]] = _noop_async
    open_or_update_pr: Callable[[], Awaitable[tuple[str, str]]] | None = None
    get_check_runs: Callable[[str], Awaitable[list[dict]]] | None = None
    get_check_run_logs: Callable[[dict], Awaitable[str]] | None = None
    local_gate: Callable[[], Awaitable[tuple[str, str]]] | None = None
    sleep: Callable[[float], Awaitable[None]] = _noop_async
    poll_interval_s: float = 15.0
    pr_url: str | None = None

    @property
    def workspace(self) -> Path:
        return Path(self.stand.path)

    def milestone_tests_known(self) -> list[str]:
        """Names of tests added by completed milestones (grown baseline)."""
        return list(self.baseline.test_names or [])


_ARTIFACT_MARKERS = ("__pycache__/", ".pytest_cache/", ".ruff_cache/", ".mypy_cache/", "node_modules/", ".tox/")
_ARTIFACT_SUFFIXES = (".pyc", ".pyo")


def _is_build_artifact(path: str) -> bool:
    """Files the test runner or the tools write, never a change of ours."""
    return any(m in path for m in _ARTIFACT_MARKERS) or path.endswith(_ARTIFACT_SUFFIXES)


def _forbidden_paths(kind: str) -> list[str]:
    return [DOCS_TREE, "README.md"] if kind != "tests" else [DOCS_TREE, "README.md", "src/", "app/", "lib/"]


def _scope_list(milestone: MilestonePlan) -> list[str]:
    scope = (milestone.scope or "").strip()
    if not scope or scope == ".":
        return []
    if getattr(milestone, "lane", None) == "repair":
        return [scope]  # 169: one file group per milestone; a comma in the file name is part of the path
    return scope_segments(scope)


def _build_brief(
    ctx: RunContext,
    milestone: MilestonePlan,
    *,
    kind: str,
    persona_kind: str,
    failing_tests: list[str] | None = None,
    failure_excerpt: str | None = None,
    extra_values: dict[str, Any] | None = None,
) -> TurnBrief:
    scope_paths = _scope_list(milestone)
    values: dict[str, Any] = {
        "milestone_goal": milestone.goal,
        "scope_paths": ", ".join(scope_paths) or "the paths the plan names",
        "done_when": milestone.done_when,
        "test_conventions": f"{ctx.runner_kind} conventions",
        "failing_tests": "\n".join(failing_tests or []) or "(see excerpt)",
        "failure_excerpt": (failure_excerpt or "")[:8000],
        "passing_test_files": "",
        "command": "",
        "tool_output": "",
        "check_name": "",
        "log_excerpt": "",
        "reported_behaviour": f"{getattr(ctx.score, 'title', '')}\n\n{getattr(ctx.score, 'description', '')}".strip(),
    }
    if extra_values:
        values.update(extra_values)
    persona = render(persona_kind, **values)
    persona += "\n".join(completed_documentation_prompt_section(ctx.score))
    if ctx.investigation_note and persona_kind in ("TESTS", "IMPLEMENT", "REPAIR_TESTS", "REPAIR_IMPLEMENT"):
        persona += "\n\nInvestigation note from the bug investigation turn:\n" + ctx.investigation_note[:4000]
    return TurnBrief(
        kind=kind,
        persona_kind=persona_kind,
        persona=persona,
        milestone_index=milestone.index,
        milestone_goal=milestone.goal[:256],
        scope_paths=scope_paths,
        done_when=milestone.done_when[:256],
        forbidden_paths=_forbidden_paths(kind),
        failing_tests=(failing_tests or [])[:50],
        failure_excerpt=(failure_excerpt or None) and (failure_excerpt or "")[:8000],
    )


def _as_turn_result(raw: dict[str, Any]) -> TurnResult:
    changed = raw.get("changed_paths") or []
    if isinstance(changed, dict):
        changed = sorted(changed.keys())
    return TurnResult(
        exit_state=raw.get("exit_state", "error"),
        output_tail=str(raw.get("output_tail") or "")[:8000],
        changed_paths=[str(p) for p in changed],
        wall_ms=int(raw.get("wall_ms") or 0),
        harness_commits=[str(c) for c in (raw.get("harness_commits") or [])],
    )


async def _reset_hard(workspace: Path, sha: str) -> None:
    """Return the tree to *sha* exactly: tracked, staged and untracked."""
    await git._run_git(["git", "reset", "--hard", sha], workspace)
    await git._run_git(["git", "clean", "-fdq"], workspace)


async def run_turn(
    ctx: RunContext,
    brief: TurnBrief,
    *,
    attempt_number: int,
    revert_everything: bool = False,
    milestone_test_files: list[str] | None = None,
) -> tuple[TurnResult, PerTurnAttempt, dict[str, str]]:
    """Run one harness turn and do the housekeeping every turn needs.

    Returns the turn result, its record, and the paths that remain changed
    after squashing harness commits and reverting out-of-scope edits.
    ``revert_everything`` is the investigation turn's rule: nothing it did
    survives, but what it wrote in its output does.
    """
    start_sha = git.head_sha(ctx.workspace)
    raw = await ctx.toolkit.run_agent_turn(brief.model_dump(), timeout_s=ctx.budgets.turn_timeout_s)
    result = _as_turn_result(raw)
    changed: dict[str, str] = {}
    reverts: list[dict[str, str]] = []
    if result.exit_state != "done":
        # a turn that timed out or errored leaves nothing behind
        await _reset_hard(ctx.workspace, start_sha)
    else:
        squashed = await git.squash_turn_commits(ctx.workspace, start_sha)
        if squashed:
            log.info("implementer.harness_commits_squashed", count=squashed, kind=brief.kind)
        changed = await git.changed_paths_since(ctx.workspace, start_sha)
        # the harness's own state directory (.codex, .claude, ...) is never a
        # change of ours: it is not committed, not a scope violation, and stays
        # on disk for the next turn (131 strips it before any push)
        agent_state = [p for p in changed if p.split("/", 1)[0] in AGENT_CONFIG_DIRS or _is_build_artifact(p)]
        for p in agent_state:
            changed.pop(p, None)
        if agent_state:
            log.info("implementer.agent_state_ignored", paths=len(agent_state), kind=brief.kind)
        if revert_everything:
            await _reset_hard(ctx.workspace, start_sha)
            reverts = [{"path": p, "kind": "reverted_investigation", "reason": "investigation turns are write-free"} for p in sorted(changed)]
            changed = {}
        else:
            violations = scope_violations(brief.kind, changed, ctx.runner_kind, brief.scope_paths or None, docs_tree=DOCS_TREE, milestone_test_files=milestone_test_files,
                                          foreign_scope_paths=[scope for plan in ctx.plans if plan.index != brief.milestone_index for scope in _scope_list(plan)])
            if violations:
                await git.revert_paths(ctx.workspace, [v["path"] for v in violations])
                reverts = violations
                for v in violations:
                    changed.pop(v["path"], None)
    ctx.scope_reverts.extend(reverts)
    attempt = PerTurnAttempt(
        kind=brief.kind,
        milestone_index=brief.milestone_index,
        attempt_number=attempt_number,
        exit_state=result.exit_state,
        wall_ms=result.wall_ms,
        files_changed=len(changed),
        has_out_of_scope_reverts=bool(reverts),
        failure_reason=None if result.exit_state == "done" else f"turn {result.exit_state}",
    )
    ctx.turn_attempts.append(attempt)
    log.info(
        "implementer.turn",
        kind=brief.kind,
        persona=brief.persona_kind,
        milestone=brief.milestone_index,
        attempt=attempt_number,
        exit_state=result.exit_state,
        files_changed=len(changed),
        reverted=len(reverts),
        wall_ms=result.wall_ms,
    )
    return result, attempt, changed


async def _scoped_command(ctx: RunContext, files: list[str]) -> str | None:
    """The narrowed command for *files*, asked of the model once per file set.

    379: the green loop runs up to ``impl_attempts`` times over the same files;
    asking every attempt would trade suite minutes for gateway round trips.
    """
    key = tuple(sorted(files))
    if key not in ctx.scoped_commands:
        ctx.scoped_commands[key] = await scoped_command(ctx.toolkit, ctx.test_command, list(key))
    return ctx.scoped_commands[key]


async def _tests(ctx: RunContext, files: list[str] | None = None, *, scope: bool = False) -> TestSummary:
    """Run the suite, or just *files* when the caller asked a scoped question.

    379: red and green ask about one milestone's tests, so they scope. The
    baseline, the chore lane and the quality phase ask a whole-suite question
    and do not.
    """
    command = ctx.test_command
    if scope and files:
        command = await _scoped_command(ctx, files) or ctx.test_command
    return await run_tests(
        ctx.toolkit, command, ctx.runner_kind, ctx.workspace, ctx.test_timeout_s, files=files
    )


async def _red_observed(ctx: RunContext, milestone: MilestonePlan, files: list[str], summary: TestSummary) -> bool:
    """Is this the red the milestone expected? The model decides (#365).

    Replaces a boolean whose name branch only ever ran for pytest and whose
    fallback counted failures. A load error -- the canonical red for a class
    that does not exist yet -- reports no counts at all and was therefore
    invisible; that is what made card #106 bounce ten times.

    A mechanical floor remains, and it is about the loop rather than the
    judgement: a step that changed no test file has not written a test, so
    there is nothing whose failure could be the expected red.

    The judge is handed the reading ``run_tests`` actually got. An earlier cut
    of this rebuilt a ``TestObservation`` from the ``TestSummary`` instead, and
    that reconstruction was the old mistake wearing new clothes: a load error
    carries no counts, so it flattened to the same summary as a suite that
    selected nothing and came back out as ``no_tests_ran``. The judge persona
    says in as many words that a load error IS the expected red and a suite
    that selected nothing is not, so card #106 -- the case this whole change
    exists for -- would have been handed the one input that produces the wrong
    verdict. A summary is a projection; it is not something to invert.
    """
    from performer.workflows.implementer.observe import TestObservation, judge_red

    if not files:
        return False
    observation = summary.observation
    if not isinstance(observation, TestObservation):
        # Nothing was read: the exit-127 short circuit, or a summary built by
        # hand. A failure nobody could read is not a failure anyone can call
        # the expected red, and guessing one back from the counts is the thing
        # above.
        log.info("implementer.red_unjudgeable", milestone=milestone.index, exit_code=summary.exit_code)
        return False
    judgement = await judge_red(
        ctx.toolkit, milestone.goal, observation, files,
        already_failing=list(ctx.baseline.test_names_failed or []),
    )
    log.info(
        "implementer.red_judged",
        milestone=milestone.index,
        expected_red=judgement.is_expected_red,
        next_action=judgement.next_action,
        reason=judgement.reason[:200],
    )
    return judgement.is_expected_red


def _grow_baseline(ctx: RunContext, summary: TestSummary) -> None:
    if ctx.baseline.test_names is not None and summary.test_names_passed is not None:
        merged = sorted(set(ctx.baseline.test_names) | set(summary.test_names_passed))
        ctx.baseline = ctx.baseline.model_copy(update={"test_names": merged})
    elif ctx.baseline.pass_count is not None and summary.test_names_passed is not None:
        # 379: only ever grows. A scoped run reports just this milestone's
        # tests, so overwriting the count with it would shrink a whole-suite
        # baseline to a handful and blind the quality phase's comparison.
        grown = max(ctx.baseline.pass_count, len(summary.test_names_passed))
        ctx.baseline = ctx.baseline.model_copy(update={"pass_count": grown})


def _lane(ctx: RunContext, milestone: MilestonePlan) -> str:
    """The lane this milestone runs in: its own when the plan set one (spec 171
    FR-010 re-lanes a single milestone to ``tests``), else the run's."""
    return getattr(milestone, "lane", None) or ctx.lane


def _prefix(lane: str) -> str:
    return {"chore": "chore", "refactor": "refactor", "repair": "fix"}.get(lane, "feat")


def _issue(ctx: RunContext) -> str:
    return f"(#{ctx.issue_number})" if ctx.issue_number else ""


async def _commit(ctx: RunContext, paths: dict[str, str], message: str) -> str | None:
    if not paths:
        return None
    return await git.commit_paths(ctx.workspace, sorted(paths), message)


async def _investigate(ctx: RunContext, milestone: MilestonePlan) -> None:
    """Bug lane opener (FR-021): a write-free look, whose output is the note."""
    brief = _build_brief(ctx, milestone, kind="tests", persona_kind="INVESTIGATE")
    result, _attempt, _changed = await run_turn(ctx, brief, attempt_number=0, revert_everything=True)
    if result.exit_state == "done" and result.output_tail.strip():
        ctx.investigation_note = result.output_tail.strip()
        log.info("implementer.investigation_note", milestone=milestone.index, chars=len(ctx.investigation_note))
    else:
        log.warning("implementer.investigation_skipped", milestone=milestone.index, exit_state=result.exit_state)


def _log_red_miss(ctx: RunContext, milestone: MilestonePlan, files: list[str], changed: dict[str, str], summary: TestSummary, *, attempt: int) -> None:
    """Why red was not observed, with what the rule saw (live diagnostics)."""
    log.warning(
        "implementer.red_not_observed",
        milestone=milestone.index,
        attempt=attempt,
        changed_paths=sorted(changed)[:20],
        changed_test_files=files[:20],
        exit_code=summary.exit_code,
        passed=summary.passed,
        failed_count=summary.failed,
        failed_names=(summary.test_names_failed or [])[:20],
        passed_names=len(summary.test_names_passed or []) if summary.test_names_passed is not None else None,
        baseline_names=len(ctx.baseline.test_names or []) if ctx.baseline.test_names is not None else None,
        output_head=summary.raw_tail[:600],
    )


def _already_covered(ctx: RunContext, milestone: MilestonePlan, summary: TestSummary) -> bool:
    """Whether this card's own earlier commits already cover this milestone (171 FR-011).

    The same rule the plan step uses, judged against the CURRENT result set
    instead of the baseline. It requires the milestone's test files to have come
    from a ``test(#N):`` commit of this card, so a passing test file that merely
    exists on the base branch never excuses a tests turn that did nothing.
    """
    present = present_paths(ctx.workspace, scope_segments(milestone.scope))
    state = resume_state(milestone, ctx.prior_paths, present, summary.test_names_passed, summary.test_names_failed)
    return state == "done"


async def _red_phase(
    ctx: RunContext, milestone: MilestonePlan, record: PerMilestoneRecord
) -> RedOutcome:
    """Tests turn plus the observed red, with one reprompt (FR-005)."""
    brief = _build_brief(ctx, milestone, kind="tests", persona_kind="TESTS")
    result, attempt, changed = await run_turn(ctx, brief, attempt_number=1)
    record.tests_attempt = attempt
    files = changed_test_files(changed, ctx.runner_kind, brief.scope_paths or None)
    summary = await _tests(ctx, files, scope=True) if result.exit_state == "done" else TestSummary(passed=False, failed=None, exit_code=1, raw_tail="turn did not complete")
    if result.exit_state == "done" and await _red_observed(ctx, milestone, files, summary):
        return RedOutcome(files, summary, changed)
    # 171 FR-011: a tests turn that changed NO test file, over tests a previous
    # run of this card already wrote and which pass, is a resume and not a red
    # miss. A turn that DID write a test file stays under FR-005 (FR-012): a
    # passing new test is vacuous whatever the branch history holds.
    if result.exit_state == "done" and not files and _already_covered(ctx, milestone, summary):
        log.info("implementer.milestone_already_covered", milestone=milestone.index, goal=milestone.goal)
        return RedOutcome(files, summary, changed, satisfied_by="existing_tests")
    _log_red_miss(ctx, milestone, files, changed, summary, attempt=1)
    if ctx.budgets.tests_reprompts >= 1:
        reason = "the new tests pass without the behaviour" if summary.passed else ("no test file changed" if not files else "the tests turn regressed the baseline or did not complete")
        log.info("implementer.red_reprompt", milestone=milestone.index, reason=reason)
        brief2 = _build_brief(
            ctx, milestone, kind="tests", persona_kind="REPAIR_TESTS",
            extra_values={"passing_test_files": "\n".join(files) or "(no test file was changed)"},
        )
        result2, attempt2, changed2 = await run_turn(ctx, brief2, attempt_number=2)
        record.tests_reprompt = attempt2
        changed = {**changed, **changed2}
        files = changed_test_files(changed, ctx.runner_kind, brief.scope_paths or None)
        if result2.exit_state == "done":
            summary = await _tests(ctx, files, scope=True)
            if await _red_observed(ctx, milestone, files, summary):
                return RedOutcome(files, summary, changed)
            # no second FR-011 check here: the reprompt only writes test files,
            # so reaching it means the first turn's judgement already stood.
            _log_red_miss(ctx, milestone, files, changed, summary, attempt=2)
    raise MilestoneFailed(milestone.index, milestone.goal, "red was not observed: the tests did not fail for the right reason after one reprompt")


async def _green_phase(
    ctx: RunContext,
    milestone: MilestonePlan,
    record: PerMilestoneRecord,
    files: list[str],
    summary: TestSummary,
) -> tuple[TestSummary, dict[str, str]]:
    """Implementation turns until green, within the cap (FR-006)."""
    failing = list(summary.test_names_failed or [])
    excerpt = summary.raw_tail
    persona_kind = "IMPLEMENT"
    changed_all: dict[str, str] = {}
    for attempt_no in range(1, ctx.budgets.impl_attempts + 1):
        brief = _build_brief(ctx, milestone, kind="implement", persona_kind=persona_kind, failing_tests=failing, failure_excerpt=excerpt)
        result, attempt, changed = await run_turn(ctx, brief, attempt_number=attempt_no, milestone_test_files=files)
        record.implement_attempts.append(attempt)
        changed_all.update(changed)
        if result.exit_state != "done":
            excerpt = f"the previous turn {result.exit_state}"
            persona_kind = "REPAIR_IMPLEMENT"
            continue
        # 379: scoped to this milestone's test files. regressions() is NOT
        # consulted here: it compares against a whole-suite baseline, so a
        # scoped run that is simply not green yet reads as a count regression
        # ("0 -> 5") and the repair persona is sent hunting a regression that
        # does not exist. The whole-suite question is asked once, by the
        # spec-089 local gate, which runs the unscoped suite unconditionally
        # before the PR opens and rejects anything short of a clean pass. NOT
        # by the quality phase: that runs the lint set and only reaches
        # run_tests inside repair_turn, when a quality command has failed.
        summary = await _tests(ctx, files, scope=True)
        if green_check(summary, files or None, ctx.baseline):
            return summary, changed_all
        failing = list(summary.test_names_failed or [])
        excerpt = summary.raw_tail
        persona_kind = "REPAIR_IMPLEMENT"
        log.info("implementer.green_retry", milestone=milestone.index, attempt=attempt_no, failing=len(failing), scoped=True)
    raise MilestoneFailed(milestone.index, milestone.goal, f"green was not observed after {ctx.budgets.impl_attempts} implementation attempts; last failure: {excerpt[:500]}")


async def _feature(ctx: RunContext, milestone: MilestonePlan, record: PerMilestoneRecord) -> None:
    outcome = await _red_phase(ctx, milestone, record)
    if outcome.satisfied_by:
        # 171 FR-011: nothing to drive to green. A tests turn changes only test
        # files (every other path is reverted as out of scope), and this branch
        # is reached only when it changed none, so there is nothing to commit.
        record.satisfied_by = outcome.satisfied_by
        _grow_baseline(ctx, outcome.summary)
        return
    await _commit(ctx, outcome.changed, f"test{_issue(ctx)}: failing tests for {milestone.goal}")
    green_summary, impl_changed = await _green_phase(ctx, milestone, record, outcome.files, outcome.summary)
    await _commit(ctx, impl_changed, f"{_prefix(_lane(ctx, milestone))}{_issue(ctx)}: {milestone.goal}")
    _grow_baseline(ctx, green_summary)


async def _change(ctx: RunContext, milestone: MilestonePlan, record: PerMilestoneRecord) -> None:
    """Chore and refactor lanes (FR-022): one change turn, verified by the baseline."""
    persona_kind = "CHANGE"
    failing: list[str] = []
    excerpt: str | None = None
    changed_all: dict[str, str] = {}
    for attempt_no in range(1, ctx.budgets.impl_attempts + 1):
        brief = _build_brief(ctx, milestone, kind="implement", persona_kind=persona_kind, failing_tests=failing, failure_excerpt=excerpt)
        result, attempt, changed = await run_turn(ctx, brief, attempt_number=attempt_no)
        record.implement_attempts.append(attempt)
        changed_all.update(changed)
        if result.exit_state != "done":
            excerpt, persona_kind = f"the previous turn {result.exit_state}", "REPAIR_IMPLEMENT"
            continue
        summary = await _tests(ctx)
        regs = regressions(ctx.baseline, summary)
        if not regs and summary.passed:
            await _commit(ctx, changed_all, f"{_prefix(_lane(ctx, milestone))}{_issue(ctx)}: {milestone.goal}")
            _grow_baseline(ctx, summary)
            return
        failing = list(summary.test_names_failed or []) or regs
        excerpt = "regressed: " + ", ".join(regs) + "\n" + summary.raw_tail if regs else summary.raw_tail
        persona_kind = "REPAIR_IMPLEMENT"
    raise MilestoneFailed(milestone.index, milestone.goal, f"the change regressed the baseline after {ctx.budgets.impl_attempts} attempts; last failure: {(excerpt or '')[:500]}")


def _findings_text(findings: list[dict]) -> str:
    lines = []
    for f in findings:
        where = f"{f.get('path')}:{f.get('line')}" if f.get("path") else "(pull request)"
        ev = f" Evidence: {f.get('evidence')}" if f.get("evidence") else ""
        lines.append(f"- [{f.get('category')}] {where}: {f.get('problem')} Why blocking: {f.get('why_blocking')}{ev}")
    return "\n".join(lines) or "(none)"


async def _repair(ctx: RunContext, milestone: MilestonePlan, record: PerMilestoneRecord) -> None:
    """Repair lane (spec 169 FR-013): no red step; one bounded turn per file group of
    review findings carrying them verbatim, the milestone tests after the turn, a
    bounded repair on a regression, then the commit. Body-anchored findings ride
    with the first group."""
    from performer.workflows.implementer.plan import review_findings, review_findings_for

    group = review_findings_for(ctx.score, milestone.scope)
    if milestone.index == 0:
        group = group + [f for f in review_findings(ctx.score) if not f.get("path")]
    values = {"path": milestone.scope, "findings": _findings_text(group)}
    persona_kind = "REPAIR_REVIEW"
    failing: list[str] = []
    excerpt: str | None = None
    changed_all: dict[str, str] = {}
    for attempt_no in range(1, ctx.budgets.impl_attempts + 1):
        brief = _build_brief(ctx, milestone, kind="implement", persona_kind=persona_kind, failing_tests=failing, failure_excerpt=excerpt, extra_values=values if persona_kind == "REPAIR_REVIEW" else None)
        result, attempt, changed = await run_turn(ctx, brief, attempt_number=attempt_no)
        record.implement_attempts.append(attempt)
        changed_all.update(changed)
        if result.exit_state != "done":
            excerpt, persona_kind = f"the previous turn {result.exit_state}", "REPAIR_IMPLEMENT"
            continue
        summary = await _tests(ctx)
        regs = regressions(ctx.baseline, summary)
        if not regs and summary.passed:
            await _commit(ctx, changed_all, f"{_prefix(_lane(ctx, milestone))}{_issue(ctx)}: {milestone.goal}")
            _grow_baseline(ctx, summary)
            return
        failing = list(summary.test_names_failed or []) or regs
        excerpt = "regressed: " + ", ".join(regs) + "\n" + summary.raw_tail if regs else summary.raw_tail
        persona_kind = "REPAIR_IMPLEMENT"
    raise MilestoneFailed(milestone.index, milestone.goal, f"the repair regressed the baseline after {ctx.budgets.impl_attempts} attempts; last failure: {(excerpt or '')[:500]}")


async def _tests_lane(ctx: RunContext, milestone: MilestonePlan, record: PerMilestoneRecord) -> None:
    """Tests lane (FR-023): the new tests must pass against the existing code."""
    brief = _build_brief(ctx, milestone, kind="tests", persona_kind="TESTS")
    result, attempt, changed = await run_turn(ctx, brief, attempt_number=1)
    record.tests_attempt = attempt
    if result.exit_state != "done":
        raise MilestoneFailed(milestone.index, milestone.goal, f"the tests turn {result.exit_state}")
    files = changed_test_files(changed, ctx.runner_kind, brief.scope_paths or None)
    if not files:
        raise MilestoneFailed(milestone.index, milestone.goal, "the tests turn changed no test file")
    summary = await _tests(ctx)
    regs = regressions(ctx.baseline, summary)
    if regs or not summary.passed:
        failing = list(summary.test_names_failed or []) or regs
        record.failure_reason = "finding: new tests fail against the existing code: " + ", ".join(failing)
        raise MilestoneFailed(milestone.index, milestone.goal, record.failure_reason)
    await _commit(ctx, changed, f"test{_issue(ctx)}: cover {milestone.goal}")
    _grow_baseline(ctx, summary)


async def run_milestone(ctx: RunContext, milestone: MilestonePlan) -> PerMilestoneRecord:
    """Run one milestone in the context's lane; append and return its record.

    On failure the tree is returned to the milestone's start commit and
    :class:`MilestoneFailed` is raised with the reason (FR-009).
    """
    start_sha = git.head_sha(ctx.workspace)
    record = PerMilestoneRecord(index=milestone.index, goal=milestone.goal, done_when=milestone.done_when, implementation_successful=False)
    started = time.monotonic()
    # 171 FR-010: the MILESTONE's lane, not the run's, so a single milestone the
    # resume rule re-laned to ``tests`` runs there. Every lane a plan builds
    # today carries the run's lane, so this is the run's lane in every other case.
    lane = _lane(ctx, milestone)
    log.info("implementer.milestone_start", index=milestone.index, goal=milestone.goal, lane=lane)
    try:
        if lane == "bug":
            if not ctx.investigated:  # FR-021: one investigation per run, not per milestone
                ctx.investigated = True
                await _investigate(ctx, milestone)
            await _feature(ctx, milestone, record)
        elif lane in ("chore", "refactor"):
            await _change(ctx, milestone, record)
        elif lane == "tests":
            await _tests_lane(ctx, milestone, record)
        elif lane == "repair":
            await _repair(ctx, milestone, record)
        else:
            await _feature(ctx, milestone, record)
    except MilestoneFailed as exc:
        record.failure_reason = record.failure_reason or exc.reason
        record.implementation_successful = False
        # 393: spec-167 FR-009 returned the tree to the milestone's start
        # commit here, which kept the branch free of broken intermediate
        # states. It also threw the attempt away: card #160 ran twice, six
        # minutes of writing each, and produced the IDENTICAL verdict, because
        # both attempts started from an empty branch and rewrote the same file.
        #
        # The branch is already deterministic and already preserved across
        # runs, so the next container edits the partial file instead of
        # deriving it from nothing -- which only works if something is left
        # behind. The salvage commit is deliberately NOT a spec-171 resume
        # credit (see salvage.SALVAGE_PREFIX): unvalidated work must never let
        # a milestone be skipped. The clean-PR concern FR-009
        # protected is still served: these branches reach main only through a
        # PR with reviewer, security and CI gates, and the commit says plainly
        # that the milestone did not complete.
        #
        # The reset remains the fallback for when there is nothing worth
        # keeping, so a failure that produced only artifacts still leaves a
        # clean tree.
        # Salvage is best effort, and "best effort" has to hold for the call
        # itself, not just for the paths inside it. This block runs while a
        # MilestoneFailed is in flight; anything raised here would replace that
        # honest failure with a crash and lose the reason the operator needs.
        from performer.workflows.implementer.salvage import salvage_failed_work

        salvaged = False
        try:
            salvaged = await salvage_failed_work(ctx, since_sha=start_sha, reason=exc.reason)
        except Exception as salvage_exc:  # noqa: BLE001 - never mask the real failure
            log.warning("implementer.salvage_raised", error=str(salvage_exc)[:200])
        if salvaged:
            ctx.work_salvaged = True
        else:
            await _reset_hard(ctx.workspace, start_sha)
        ctx.milestone_records.append(record)
        log.warning("implementer.milestone_failed", index=milestone.index, reason=exc.reason, ms=int((time.monotonic() - started) * 1000))
        raise
    record.implementation_successful = True
    ctx.milestone_records.append(record)
    log.info("implementer.milestone_done", index=milestone.index, ms=int((time.monotonic() - started) * 1000))
    return record

"""Spec 167 state-machine scenarios against a REAL git repository.

A bare remote, a clone on the card branch, a scripted fake harness that edits
files per turn kind, and a fake test runner whose verdicts follow the files
that exist. The workflow's outside edges (push, PR, check runs, logs, sleep)
are injected fakes. The 165 review showed fakes over git hid two plumbing
bugs, so every scenario here runs real git.

Test convention of the fake runner: a test file under tests/ holds lines
``EXPECTS <path>``; each such line is one test that passes when the path
exists in the repository and fails otherwise. Output is pytest-shaped
(``PASSED tests/x.py::test_0`` lines plus a summary) so the real parser
reads names.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from performer.models import Stand
from performer.workflows.base import WorkflowMetrics
from performer.workflows.implementer import ImplementerWorkflow
from performer.workflows.toolkit import Toolkit

_G = ["git", "-c", "user.email=e@x", "-c", "user.name=t"]


def _sh(args, cwd):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout


def _repo(tmp_path: Path) -> tuple[Path, Path]:
    bare = tmp_path / "origin.git"
    _sh(["git", "init", "-q", "--bare", "-b", "main", str(bare)], tmp_path)
    repo = tmp_path / "repo"
    _sh(["git", "init", "-q", "-b", "main", str(repo)], tmp_path)
    (repo / "src").mkdir()
    (repo / "tests").mkdir()
    (repo / "src" / "base.py").write_text("BASE = 1\n")
    (repo / "tests" / "test_base.py").write_text("EXPECTS src/base.py\n")
    (repo / "README.md").write_text("readme\n")
    _sh([*_G, "add", "."], repo)
    _sh([*_G, "commit", "-q", "-m", "base"], repo)
    _sh(["git", "remote", "add", "origin", str(bare)], repo)
    _sh(["git", "push", "-q", "origin", "main"], repo)
    _sh(["git", "checkout", "-q", "-b", "feat/x"], repo)
    _sh(["git", "push", "-q", "origin", "feat/x"], repo)
    return repo, bare


def _log(repo: Path) -> list[str]:
    return [line for line in _sh(["git", "log", "--format=%s", "main..HEAD"], repo).splitlines() if line]


def _head(repo: Path) -> str:
    return _sh(["git", "rev-parse", "HEAD"], repo).strip()


def _fake_test_runner(repo: Path):
    """The fake runner: verdicts follow the files; lint follows a marker file."""

    async def run(cmd: str, cwd, timeout_s):
        cwd = Path(cwd or repo)
        if cmd.startswith("pytest"):
            lines, failures = [], 0
            for tf in sorted((cwd / "tests").glob("test_*.py")):
                for i, raw in enumerate(tf.read_text().splitlines()):
                    if raw.startswith("EXPECTS "):
                        target = raw.split(" ", 1)[1].strip()
                        name = f"tests/{tf.name}::test_{i}"
                        if (cwd / target).exists():
                            lines.append(f"PASSED {name}")
                        else:
                            lines.append(f"FAILED {name}")
                            failures += 1
            passed = len(lines) - failures
            lines.append(f"{failures} failed, {passed} passed" if failures else f"{passed} passed")
            return (1 if failures else 0), "\n".join(lines)
        if cmd == "lint":
            if (cwd / ".lint_fail").exists():
                return 1, "src/style.py:1:1: E999 lint error"
            return 0, "clean"
        return 0, ""

    return run


class Harness:
    """A scripted fake agent_turn_runner keyed by persona kind."""

    def __init__(self, repo: Path, script: dict) -> None:
        self.repo, self.script, self.briefs = repo, script, []

    async def __call__(self, brief: dict, *, timeout_s: float) -> dict:
        self.briefs.append(brief)
        action = self.script.get(brief["persona_kind"])
        output = ""
        if action is not None:
            output = action(self.repo, brief) or ""
        return {"exit_state": "done", "output_tail": output, "changed_paths": [], "wall_ms": 5, "harness_commits": []}


def _score(*, milestones=None, work_kind=None, single_turn=False, test_command="pytest -rA", env=None):
    brief = None
    if milestones is not None or work_kind is not None:
        brief = {"milestones": milestones or [], "implementer_single_turn": single_turn}
        if work_kind:
            brief["work_kind"] = work_kind
    return SimpleNamespace(
        title="Add the thing", description="The thing should work.", acceptance_criteria=["it works"],
        implementation_brief=brief, workflow_env=env or {}, test_command=test_command, issue_number=7,
        local_test_gate={"enabled": True, "lint_command": "lint"}, owner_repo=("o", "r"),
        effective_github_token="t", repo_url="https://example.invalid/o/r", pr_url="",
    )


def _milestones(n: int):
    return [{"goal": f"milestone {i}", "scope": f"src/m{i}.py, tests/test_m{i}.py", "done_when": f"m{i} tests pass"} for i in range(n)]


def _write(repo: Path, rel: str, text: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def _tests_turn(repo, brief):
    i = brief["milestone_index"]
    _write(repo, f"tests/test_m{i}.py", f"EXPECTS src/m{i}.py\n")


def _impl_turn(repo, brief):
    i = brief["milestone_index"]
    _write(repo, f"src/m{i}.py", f"M{i} = True\n")


class Edges:
    """Fake outside world with counters."""

    def __init__(self, checks=None):
        self.pushes = 0
        self.pr_opened = 0
        self.polls = 0
        self.checks = checks or (lambda n: [{"name": "Test", "status": "completed", "conclusion": "success"}])

    async def push(self):
        self.pushes += 1

    async def open_or_update_pr(self):
        self.pr_opened += 1
        return "https://example.invalid/o/r/pull/1", "node"

    async def get_check_runs(self, sha):
        self.polls += 1
        return self.checks(self.polls)

    async def get_check_run_logs(self, run):
        return f"log for {run.get('name')}: boom"

    async def sleep(self, s):
        return None

    def overrides(self, **extra):
        base = {
            "push": self.push, "open_or_update_pr": self.open_or_update_pr, "get_check_runs": self.get_check_runs,
            "get_check_run_logs": self.get_check_run_logs, "sleep": self.sleep, "poll_interval_s": 0.0,
        }
        base.update(extra)
        return base


async def _run(repo, score, harness, edges: Edges, **overrides):
    async def no_model(*_a, **_k):
        raise AssertionError("the implementer workflow makes no model calls")

    toolkit = Toolkit(metrics=WorkflowMetrics(), model_call=no_model, command_runner=_fake_test_runner(repo), agent_turn_runner=harness, call_limit=12)
    stand = Stand(path=repo, branch="feat/x")
    result = await ImplementerWorkflow().run(stand, score, toolkit, ctx_overrides=edges.overrides(**overrides))
    return result.report, toolkit


# --- US1: two milestones, test-first ------------------------------------------

@pytest.mark.asyncio
async def test_two_milestones_build_test_first_one_at_a_time(tmp_path):
    repo, _ = _repo(tmp_path)
    harness = Harness(repo, {"TESTS": _tests_turn, "IMPLEMENT": _impl_turn})
    edges = Edges()
    report, toolkit = await _run(repo, _score(milestones=_milestones(2)), harness, edges)
    run = report["implementer_run"]
    assert run["status"] == "pr_opened", run["reason"]
    assert _log(repo) == ["feat(#7): milestone 1", "test(#7): failing tests for milestone 1", "feat(#7): milestone 0", "test(#7): failing tests for milestone 0"]
    kinds = [(b["persona_kind"], b["milestone_index"]) for b in harness.briefs]
    assert kinds == [("TESTS", 0), ("IMPLEMENT", 0), ("TESTS", 1), ("IMPLEMENT", 1)]
    assert run["milestones_completed"] == 2 and run["turn_count"] == 4
    assert all(r["implementation_successful"] for r in run["per_milestone"])
    assert not (repo / "docs").exists()
    assert edges.pushes == 1 and edges.pr_opened == 1
    assert list(run["phase_durations_ms"]) == ["intake", "plan", "baseline", "resume", "milestones", "quality", "local_gate", "push_pr", "ci_wait"]
    assert run["resumed_from_milestone"] is None, "a fresh branch resumes nothing"
    assert [r["satisfied_by"] for r in run["per_milestone"]] == ["this_run", "this_run"]
    assert toolkit.metrics.agent_turns == 4 and toolkit.metrics.model_calls == 0


# --- US2: vacuous and stuck are bounded ----------------------------------------

@pytest.mark.asyncio
async def test_vacuous_tests_get_one_reprompt_then_fail_the_milestone(tmp_path):
    repo, _ = _repo(tmp_path)
    start = _head(repo)

    def vacuous(repo, brief):
        _write(repo, "tests/test_m0.py", "EXPECTS src/base.py\n")  # passes without any implementation

    harness = Harness(repo, {"TESTS": vacuous, "REPAIR_TESTS": vacuous, "IMPLEMENT": _impl_turn})
    edges = Edges()
    report, _ = await _run(repo, _score(milestones=_milestones(1)), harness, edges)
    run = report["implementer_run"]
    assert run["status"] == "partial_progress" and "red was not observed" in run["reason"]
    assert run["next_focus_milestone"] == "milestone 0"
    assert [b["persona_kind"] for b in harness.briefs] == ["TESTS", "REPAIR_TESTS"]
    assert _head(repo) == start and not (repo / "tests" / "test_m0.py").exists(), "the red tests commit is reverted"
    assert edges.pushes == 0 and edges.pr_opened == 0


@pytest.mark.asyncio
async def test_a_stuck_implementation_stops_after_three_attempts(tmp_path):
    repo, _ = _repo(tmp_path)
    start = _head(repo)

    def wrong(repo, brief):
        _write(repo, "src/wrong.py", "WRONG = 1\n")

    harness = Harness(repo, {"TESTS": _tests_turn, "IMPLEMENT": wrong, "REPAIR_IMPLEMENT": wrong})
    edges = Edges()
    report, _ = await _run(repo, _score(milestones=_milestones(1)), harness, edges)
    run = report["implementer_run"]
    assert run["status"] == "partial_progress" and "3 implementation attempts" in run["reason"]
    assert [b["persona_kind"] for b in harness.briefs] == ["TESTS", "IMPLEMENT", "REPAIR_IMPLEMENT", "REPAIR_IMPLEMENT"]
    assert harness.briefs[2]["failing_tests"] == ["tests/test_m0.py::test_0"]
    assert _head(repo) == start and edges.pushes == 0


# --- US3: quality, gate, CI with repairs, hand-off ------------------------------

@pytest.mark.asyncio
async def test_quality_and_ci_failures_are_repaired_once_each_then_handed_off(tmp_path):
    repo, _ = _repo(tmp_path)
    (repo / ".lint_fail").write_text("")
    _sh([*_G, "add", "."], repo)
    _sh([*_G, "commit", "-q", "-m", "lint marker"], repo)

    def fix_lint(repo, brief):
        assert "lint error" in brief["persona"]
        (repo / ".lint_fail").unlink()
        _write(repo, "src/style.py", "STYLE = 1\n")

    def fix_ci(repo, brief):
        assert "boom" in brief["persona"]
        _write(repo, "src/ci_fix.py", "CI = 1\n")

    harness = Harness(repo, {"TESTS": _tests_turn, "IMPLEMENT": _impl_turn, "REPAIR_QUALITY": fix_lint, "REPAIR_CI": fix_ci})
    edges = Edges(checks=lambda n: [{"name": "Lint", "id": 9, "status": "completed", "conclusion": "failure" if n == 1 else "success"}])
    report, _ = await _run(repo, _score(milestones=_milestones(1)), harness, edges)
    run = report["implementer_run"]
    assert run["status"] == "pr_opened", run["reason"]
    assert [b["persona_kind"] for b in harness.briefs] == ["TESTS", "IMPLEMENT", "REPAIR_QUALITY", "REPAIR_CI"]
    assert [a["passed"] for a in run["quality_attempts"]] == [False, True]
    assert [a["repair_needed"] for a in run["ci_attempts"]] == [True, False]
    assert run["ci_attempts"][0]["log_excerpt"].endswith("boom")
    assert edges.pushes == 2 and edges.polls == 2
    assert _log(repo)[:2] == ["fix(#7): Lint", "style(#7): satisfy lint"]


@pytest.mark.asyncio
async def test_ci_pending_past_the_wait_budget_is_an_environment_hold(tmp_path):
    repo, _ = _repo(tmp_path)
    harness = Harness(repo, {"TESTS": _tests_turn, "IMPLEMENT": _impl_turn})
    edges = Edges(checks=lambda n: [{"name": "Slow", "status": "queued", "conclusion": None}])
    report, _ = await _run(repo, _score(milestones=_milestones(1), env={"IMPL_CI_WAIT_S": "0"}), harness, edges)
    run = report["implementer_run"]
    assert run["status"] == "env_blocked" and "Slow" in run["reason"]
    assert edges.pushes == 1, "the PR was pushed; only the wait was exhausted"


@pytest.mark.asyncio
async def test_no_progress_on_ci_stops_early(tmp_path):
    repo, _ = _repo(tmp_path)
    harness = Harness(repo, {"TESTS": _tests_turn, "IMPLEMENT": _impl_turn, "REPAIR_CI": lambda r, b: _write(r, "src/try.py", "x\n")})
    edges = Edges(checks=lambda n: [{"name": "Lint", "status": "completed", "conclusion": "failure"}])
    report, _ = await _run(repo, _score(milestones=_milestones(1)), harness, edges)
    run = report["implementer_run"]
    assert run["status"] == "partial_progress" and "no progress" in run["reason"]
    assert [b["persona_kind"] for b in harness.briefs].count("REPAIR_CI") == 1


# --- housekeeping rules ---------------------------------------------------------

@pytest.mark.asyncio
async def test_harness_commits_are_squashed_into_the_step_commit(tmp_path):
    repo, _ = _repo(tmp_path)

    def tests_and_commit(repo, brief):
        _tests_turn(repo, brief)
        _sh([*_G, "add", "."], repo)
        _sh([*_G, "commit", "-q", "-m", "harness wrote tests"], repo)
        _write(repo, "tests/test_extra_m0.py", "EXPECTS src/m0.py\n")
        _sh([*_G, "add", "."], repo)
        _sh([*_G, "commit", "-q", "-m", "harness wrote more"], repo)

    harness = Harness(repo, {"TESTS": tests_and_commit, "IMPLEMENT": _impl_turn})
    report, _ = await _run(repo, _score(milestones=_milestones(1)), harness, Edges())
    assert report["implementer_run"]["status"] == "pr_opened"
    assert _log(repo) == ["feat(#7): milestone 0", "test(#7): failing tests for milestone 0"]


@pytest.mark.asyncio
async def test_documentation_edits_are_reverted_and_recorded(tmp_path):
    repo, _ = _repo(tmp_path)

    def impl_and_docs(repo, brief):
        _impl_turn(repo, brief)
        _write(repo, "docs/guide.md", "written by the implementer\n")

    harness = Harness(repo, {"TESTS": _tests_turn, "IMPLEMENT": impl_and_docs})
    report, _ = await _run(repo, _score(milestones=_milestones(1)), harness, Edges())
    run = report["implementer_run"]
    assert run["status"] == "pr_opened"
    assert not (repo / "docs" / "guide.md").exists()
    assert [r["path"] for r in run["scope_reverts"]] == ["docs/guide.md"]


@pytest.mark.asyncio
async def test_no_test_runner_is_a_hold_before_any_turn(tmp_path):
    repo, _ = _repo(tmp_path)
    for p in ("src/base.py", "tests/test_base.py"):
        (repo / p).unlink()
    harness = Harness(repo, {"TESTS": _tests_turn, "IMPLEMENT": _impl_turn})
    report, _ = await _run(repo, _score(milestones=_milestones(1), test_command=None), harness, Edges())
    run = report["implementer_run"]
    assert run["status"] == "env_blocked" and "no test runner" in run["reason"]
    assert harness.briefs == []


# --- lanes (FR-020 to FR-023) ---------------------------------------------------

@pytest.mark.asyncio
async def test_bug_lane_investigates_first_without_touching_the_tree(tmp_path):
    repo, _ = _repo(tmp_path)
    start = _head(repo)

    def investigate(repo, brief):
        assert "Investigate" in brief["persona"] or "investigat" in brief["persona"].lower()
        _write(repo, "src/scratch.py", "poking\n")  # write-free means this is reverted
        return "Suspected cause: base.py returns 1 where 2 is expected (src/base.py:1)."

    harness = Harness(repo, {"INVESTIGATE": investigate, "TESTS": _tests_turn, "IMPLEMENT": _impl_turn})
    report, _ = await _run(repo, _score(milestones=_milestones(1), work_kind="bug"), harness, Edges())
    run = report["implementer_run"]
    assert run["status"] == "pr_opened", run["reason"]
    assert [b["persona_kind"] for b in harness.briefs] == ["INVESTIGATE", "TESTS", "IMPLEMENT"]
    assert "Suspected cause" in harness.briefs[1]["persona"] and "Suspected cause" in harness.briefs[2]["persona"]
    assert not (repo / "src" / "scratch.py").exists()
    assert [r["kind"] for r in run["scope_reverts"]] == ["reverted_investigation"]
    assert _log(repo) == ["feat(#7): milestone 0", "test(#7): failing tests for milestone 0"]
    assert _sh(["git", "rev-parse", "HEAD~2"], repo).strip() == start


@pytest.mark.parametrize("kind,prefix", [("chore", "chore"), ("refactor", "refactor")])
@pytest.mark.asyncio
async def test_chore_and_refactor_change_once_and_verify_by_baseline(tmp_path, kind, prefix):
    repo, _ = _repo(tmp_path)
    harness = Harness(repo, {"CHANGE": lambda r, b: _write(r, "src/config.txt", "changed\n"), "TESTS": _tests_turn})
    report, _ = await _run(repo, _score(milestones=_milestones(1), work_kind=kind), harness, Edges())
    run = report["implementer_run"]
    assert run["status"] == "pr_opened", run["reason"]
    assert [b["persona_kind"] for b in harness.briefs] == ["CHANGE"]
    assert _log(repo) == [f"{prefix}(#7): milestone 0"]


@pytest.mark.asyncio
async def test_a_change_that_regresses_the_baseline_is_repaired_under_the_cap(tmp_path):
    repo, _ = _repo(tmp_path)

    def breaking_change(repo, brief):
        (repo / "src" / "base.py").unlink()  # tests/test_base.py now fails

    def repair(repo, brief):
        assert "tests/test_base.py::test_0" in brief["persona"]
        _write(repo, "src/base.py", "BASE = 2\n")

    harness = Harness(repo, {"CHANGE": breaking_change, "REPAIR_IMPLEMENT": repair})
    report, _ = await _run(repo, _score(milestones=_milestones(1), work_kind="chore"), harness, Edges())
    assert report["implementer_run"]["status"] == "pr_opened"
    assert [b["persona_kind"] for b in harness.briefs] == ["CHANGE", "REPAIR_IMPLEMENT"]


@pytest.mark.asyncio
async def test_tests_lane_adds_passing_coverage_without_an_implementation_turn(tmp_path):
    repo, _ = _repo(tmp_path)
    harness = Harness(repo, {"TESTS": lambda r, b: _write(r, "tests/test_cover.py", "EXPECTS src/base.py\n")})
    report, _ = await _run(repo, _score(milestones=_milestones(1), work_kind="tests"), harness, Edges())
    run = report["implementer_run"]
    assert run["status"] == "pr_opened", run["reason"]
    assert [b["persona_kind"] for b in harness.briefs] == ["TESTS"]
    assert _log(repo) == ["test(#7): cover milestone 0"]


@pytest.mark.asyncio
async def test_tests_lane_treats_a_failing_new_test_as_a_finding(tmp_path):
    repo, _ = _repo(tmp_path)
    start = _head(repo)
    harness = Harness(repo, {"TESTS": lambda r, b: _write(r, "tests/test_cover.py", "EXPECTS src/missing.py\n")})
    edges = Edges()
    report, _ = await _run(repo, _score(milestones=_milestones(1), work_kind="tests"), harness, edges)
    run = report["implementer_run"]
    assert run["status"] == "partial_progress" and "finding" in run["reason"] and "tests/test_cover.py::test_0" in run["reason"]
    assert _head(repo) == start and edges.pushes == 0


@pytest.mark.parametrize("kind", ["research", "docs"])
@pytest.mark.asyncio
async def test_research_and_docs_never_reach_the_implementer(tmp_path, kind):
    repo, _ = _repo(tmp_path)
    harness = Harness(repo, {"TESTS": _tests_turn, "IMPLEMENT": _impl_turn})
    report, _ = await _run(repo, _score(milestones=_milestones(1), work_kind=kind), harness, Edges())
    run = report["implementer_run"]
    assert run["status"] == "env_blocked" and kind in run["reason"]
    assert harness.briefs == []


@pytest.mark.asyncio
async def test_single_turn_card_collapses_to_one_milestone(tmp_path):
    repo, _ = _repo(tmp_path)
    harness = Harness(repo, {"TESTS": _tests_turn, "IMPLEMENT": _impl_turn})
    report, _ = await _run(repo, _score(milestones=_milestones(3), single_turn=True), harness, Edges())
    run = report["implementer_run"]
    assert run["status"] == "pr_opened", run["reason"]
    assert run["milestones_planned"] == 1 and run["turn_count"] == 2


@pytest.mark.asyncio
async def test_no_brief_uses_the_card_criteria_list_as_one_milestone(tmp_path):
    """The dispatch payload's acceptance_criteria is a list; the plan must
    join it into the single milestone's done-when (found by the eval)."""
    repo, _ = _repo(tmp_path)
    harness = Harness(repo, {"TESTS": _tests_turn, "IMPLEMENT": _impl_turn})
    score = _score()
    score.acceptance_criteria = ["the page loads", "the copy reads Contact us"]
    report, _ = await _run(repo, score, harness, Edges())
    run = report["implementer_run"]
    assert run["status"] == "pr_opened", run["reason"]
    assert run["milestones_planned"] == 1
    assert run["per_milestone"][0]["done_when"] == "the page loads; the copy reads Contact us"


# --- review of the branch and the first live rounds ----------------------------

@pytest.mark.asyncio
async def test_bug_lane_investigates_once_per_run_not_per_milestone(tmp_path):
    repo, _ = _repo(tmp_path)
    harness = Harness(repo, {"INVESTIGATE": lambda r, b: "note", "TESTS": _tests_turn, "IMPLEMENT": _impl_turn})
    report, _ = await _run(repo, _score(milestones=_milestones(2), work_kind="bug"), harness, Edges())
    assert report["implementer_run"]["status"] == "pr_opened"
    assert [b["persona_kind"] for b in harness.briefs].count("INVESTIGATE") == 1


@pytest.mark.asyncio
async def test_a_path_the_milestone_names_is_in_scope_even_when_it_is_documentation(tmp_path):
    """Live round: a chore whose whole point was the README had its edit reverted as docs."""
    repo, _ = _repo(tmp_path)
    ms = [{"goal": "retitle the README", "scope": "README.md", "done_when": "README updated"}]
    harness = Harness(repo, {"CHANGE": lambda r, b: _write(r, "README.md", "# calc, a tiny calculator\n")})
    report, _ = await _run(repo, _score(milestones=ms, work_kind="chore"), harness, Edges())
    run = report["implementer_run"]
    assert run["status"] == "pr_opened", run["reason"]
    assert run["scope_reverts"] == [] and (repo / "README.md").read_text().startswith("# calc, a tiny")
    assert _log(repo) == ["chore(#7): retitle the README"]


@pytest.mark.asyncio
async def test_the_harness_state_directory_is_never_a_change_of_ours(tmp_path):
    """Live round: codex wrote 92 files under .codex/ and they reached the commit."""
    repo, _ = _repo(tmp_path)

    def impl_with_state(repo, brief):
        _impl_turn(repo, brief)
        _write(repo, ".codex/.tmp/plugins/state.json", "{}")
        _write(repo, ".codex/session.log", "log")

    harness = Harness(repo, {"TESTS": _tests_turn, "IMPLEMENT": impl_with_state})
    report, _ = await _run(repo, _score(milestones=_milestones(1)), harness, Edges())
    run = report["implementer_run"]
    assert run["status"] == "pr_opened", run["reason"]
    committed = _sh(["git", "show", "--name-only", "--format=", "HEAD"], repo).split()
    assert committed == ["src/m0.py"], committed
    assert run["scope_reverts"] == []
    assert (repo / ".codex" / "session.log").exists(), "the state dir stays on disk for the next turn"


@pytest.mark.asyncio
async def test_a_rejected_push_is_a_bounded_failure_not_a_crash(tmp_path):
    repo, _ = _repo(tmp_path)
    harness = Harness(repo, {"TESTS": _tests_turn, "IMPLEMENT": _impl_turn})
    edges = Edges()

    async def rejected():
        raise RuntimeError("push_branch.rebase_conflict: nothing was pushed")

    report, _ = await _run(repo, _score(milestones=_milestones(1)), harness, edges, push=rejected)
    run = report["implementer_run"]
    assert run["status"] == "partial_progress" and "rebase_conflict" in run["reason"]
    assert edges.pr_opened == 0


@pytest.mark.asyncio
async def test_a_failing_pr_api_is_an_environment_hold(tmp_path):
    repo, _ = _repo(tmp_path)
    harness = Harness(repo, {"TESTS": _tests_turn, "IMPLEMENT": _impl_turn})

    async def api_down():
        raise RuntimeError("GitHub API 502")

    report, _ = await _run(repo, _score(milestones=_milestones(1)), harness, Edges(), open_or_update_pr=api_down)
    run = report["implementer_run"]
    assert run["status"] == "env_blocked" and "502" in run["reason"]


@pytest.mark.asyncio
async def test_the_report_carries_the_pr_node_id_for_coordinare(tmp_path):
    repo, _ = _repo(tmp_path)
    harness = Harness(repo, {"TESTS": _tests_turn, "IMPLEMENT": _impl_turn})
    report, _ = await _run(repo, _score(milestones=_milestones(1)), harness, Edges())
    assert report["pr_node_id"] == "node" and report["pr_url"].endswith("/pull/1")


@pytest.mark.asyncio
async def test_a_tests_turn_may_not_implement_through_an_in_scope_source_file(tmp_path):
    """Second live round: the model wrote the function during the tests turn
    via a source file the scope named, so the tests passed and red was never
    observed. The scope exempts the docs rule only."""
    repo, _ = _repo(tmp_path)

    def tests_and_source(repo, brief):
        _tests_turn(repo, brief)
        _impl_turn(repo, brief)  # src/m0.py is in the milestone scope

    harness = Harness(repo, {"TESTS": tests_and_source, "IMPLEMENT": _impl_turn})
    report, _ = await _run(repo, _score(milestones=_milestones(1)), harness, Edges())
    run = report["implementer_run"]
    assert run["status"] == "pr_opened", run["reason"]
    assert [r["path"] for r in run["scope_reverts"]] == ["src/m0.py"]
    assert [b["persona_kind"] for b in harness.briefs] == ["TESTS", "IMPLEMENT"], "red was observed after the revert"


@pytest.mark.asyncio
async def test_runner_artifacts_are_not_changes(tmp_path):
    repo, _ = _repo(tmp_path)

    def tests_with_pycache(repo, brief):
        _tests_turn(repo, brief)
        _write(repo, "tests/__pycache__/test_m0.cpython-312.pyc", "bytes")
        _write(repo, "src/__pycache__/base.cpython-312.pyc", "bytes")

    harness = Harness(repo, {"TESTS": tests_with_pycache, "IMPLEMENT": _impl_turn})
    report, _ = await _run(repo, _score(milestones=_milestones(1)), harness, Edges())
    run = report["implementer_run"]
    assert run["status"] == "pr_opened", run["reason"]
    assert run["scope_reverts"] == []
    assert "__pycache__" not in _sh(["git", "show", "--name-only", "--format=", "HEAD~1"], repo)


@pytest.mark.asyncio
async def test_an_implementation_turn_may_not_pre_write_later_milestones_tests(tmp_path):
    """Sixth live round: milestone one's implementation turn also wrote the
    tests for milestone two, whose tests turn then had nothing to write and
    red could not be observed. Foreign test files are reverted."""
    repo, _ = _repo(tmp_path)

    def impl_over_delivering(repo, brief):
        _impl_turn(repo, brief)
        if brief["milestone_index"] == 0:
            _write(repo, "tests/test_m1.py", "EXPECTS src/m1.py\n")  # milestone two's tests, too early
            _write(repo, "src/m1.py", "M1 = True\n")

    harness = Harness(repo, {"TESTS": _tests_turn, "IMPLEMENT": impl_over_delivering})
    report, _ = await _run(repo, _score(milestones=_milestones(2)), harness, Edges())
    run = report["implementer_run"]
    assert sorted((r["kind"], r["path"]) for r in run["scope_reverts"]) == [
        ("reverted_foreign_source", "src/m1.py"),
        ("reverted_foreign_test", "tests/test_m1.py"),
    ]
    assert run["status"] == "pr_opened", run["reason"]
    assert run["milestones_completed"] == 2
    assert [b["persona_kind"] for b in harness.briefs][:3] == ["TESTS", "IMPLEMENT", "TESTS"], "milestone two still gets its tests turn"


# --- spec 171: resuming a branch this card's earlier run already worked --------


def _card_commit(repo: Path, files: dict[str, str], subject: str) -> None:
    """Commit *files* under a subject the driver would have written."""
    for rel, text in files.items():
        _write(repo, rel, text)
    _sh([*_G, "add", "--", *files], repo)
    _sh([*_G, "commit", "-q", "-m", subject], repo)


def _over_delivering_impl(repo, brief):
    """Milestone zero's turn also implements milestone one (the live failure)."""
    _impl_turn(repo, brief)
    if brief["milestone_index"] == 0:
        _write(repo, "src/m1.py", "M1 = True\n")
        _write(repo, "tests/test_m1.py", "EXPECTS src/m1.py\n")  # reverted as a foreign test


@pytest.mark.asyncio
async def test_a_re_dispatch_resumes_instead_of_repeating_the_whole_plan(tmp_path):
    """The live loop, end to end over one real clone (171 SC-001).

    Run one over-implements milestone one during milestone zero and then cannot
    observe red for milestone one, so it stops at partial_progress. Before 171
    the next dispatch replanned from milestone zero, whose tests now pass at
    baseline, so it failed EARLIER than run one and the card could never finish.
    """
    repo, _ = _repo(tmp_path)
    score = _score(milestones=_milestones(2))

    # Seed the historical branch produced before #279. New runs now prevent
    # the leak, but deployed branches with old commits must still resume.
    _card_commit(repo, {"tests/test_m0.py": "EXPECTS src/m0.py\n"}, "test(#7): failing tests for milestone 0")
    _card_commit(repo, {"src/m0.py": "M0 = True\n", "src/m1.py": "M1 = True\n"}, "feat(#7): milestone 0")
    assert not (repo / "tests" / "test_m1.py").exists()

    second = Harness(repo, {"TESTS": _tests_turn, "IMPLEMENT": _impl_turn})
    edges = Edges()
    report_two, _ = await _run(repo, score, second, edges)
    run_two = report_two["implementer_run"]

    assert run_two["status"] == "pr_opened", run_two["reason"]
    assert [(b["persona_kind"], b["milestone_index"]) for b in second.briefs] == [("TESTS", 1)], (
        "milestone zero is skipped and milestone one runs in the tests lane"
    )
    assert run_two["resumed_from_milestone"] == 1
    assert run_two["milestones_planned"] == 2 and run_two["milestones_completed"] == 2
    assert [r["satisfied_by"] for r in run_two["per_milestone"]] == ["prior_run", "this_run"]
    assert _log(repo) == [
        "test(#7): cover milestone 1",
        "feat(#7): milestone 0",
        "test(#7): failing tests for milestone 0",
    ]
    assert edges.pushes == 1 and edges.pr_opened == 1


@pytest.mark.asyncio
async def test_a_plan_a_previous_run_finished_needs_no_turn_at_all(tmp_path):
    """Every milestone done: no turn runs and the run still hands the branch off."""
    repo, _ = _repo(tmp_path)
    score = _score(milestones=_milestones(1))
    first = Harness(repo, {"TESTS": _tests_turn, "IMPLEMENT": _impl_turn})
    assert (await _run(repo, score, first, Edges()))[0]["implementer_run"]["status"] == "pr_opened"
    before = _log(repo)

    second = Harness(repo, {"TESTS": _tests_turn, "IMPLEMENT": _impl_turn})
    edges = Edges()
    report, _ = await _run(repo, score, second, edges)
    run = report["implementer_run"]

    assert run["status"] == "pr_opened", run["reason"]
    assert second.briefs == [], "there was nothing left to do"
    assert run["milestones_completed"] == 1 and [r["satisfied_by"] for r in run["per_milestone"]] == ["prior_run"]
    assert run["resumed_from_milestone"] is None
    assert _log(repo) == before
    assert edges.pushes == 1 and edges.pr_opened == 1


@pytest.mark.asyncio
async def test_ci_still_repairs_when_every_milestone_was_skipped(tmp_path):
    """171: with nothing left to run, a red check still routes a repair turn.

    rerun_green looks a milestone up by index, and after a full skip the only
    records are the skipped ones, so this drives _green_phase for a milestone
    that never ran in this process. It must repair rather than crash.
    """
    repo, _ = _repo(tmp_path)
    score = _score(milestones=_milestones(1))
    first = Harness(repo, {"TESTS": _tests_turn, "IMPLEMENT": _impl_turn})
    assert (await _run(repo, score, first, Edges()))[0]["implementer_run"]["status"] == "pr_opened"

    second = Harness(repo, {"TESTS": _tests_turn, "IMPLEMENT": _impl_turn,
                            "REPAIR_CI": lambda r, b: _write(r, "src/ci_fix.py", "CI = 1\n")})
    edges = Edges(checks=lambda n: [{"name": "Lint", "id": 9, "status": "completed",
                                     "conclusion": "failure" if n == 1 else "success"}])
    report, _ = await _run(repo, score, second, edges)
    run = report["implementer_run"]

    assert run["status"] == "pr_opened", run["reason"]
    assert [b["persona_kind"] for b in second.briefs] == ["REPAIR_CI"]
    assert [r["satisfied_by"] for r in run["per_milestone"]] == ["prior_run"]
    assert run["per_milestone"][0]["implement_attempts"] == [], "the skipped milestone gained no attempt"
    assert _log(repo)[0] == "fix(#7): Lint"


@pytest.mark.asyncio
async def test_a_tests_turn_that_changes_nothing_over_covering_tests_is_satisfied(tmp_path):
    """171 FR-011. Milestone one is done but sits behind an open milestone zero,
    so the contiguous-prefix rule does not skip it; its tests turn writes nothing
    and the tests already there cover it."""
    repo, _ = _repo(tmp_path)
    _card_commit(repo, {"src/m1.py": "M1 = True\n"}, "feat(#7): milestone 1")
    _card_commit(repo, {"tests/test_m1.py": "EXPECTS src/m1.py\n"}, "test(#7): failing tests for milestone 1")

    def tests_turn_for_zero_only(repo, brief):
        if brief["milestone_index"] == 0:
            _tests_turn(repo, brief)

    before = _log(repo)
    harness = Harness(repo, {"TESTS": tests_turn_for_zero_only, "IMPLEMENT": _impl_turn})
    report, _ = await _run(repo, _score(milestones=_milestones(2)), harness, Edges())
    run = report["implementer_run"]

    assert run["status"] == "pr_opened", run["reason"]
    assert [(b["persona_kind"], b["milestone_index"]) for b in harness.briefs] == [
        ("TESTS", 0), ("IMPLEMENT", 0), ("TESTS", 1),
    ], "milestone one needed no implementation turn"
    assert [r["satisfied_by"] for r in run["per_milestone"]] == ["this_run", "existing_tests"]
    assert run["resumed_from_milestone"] is None, "nothing was skipped at plan time"
    assert [line for line in _log(repo) if line not in before] == [
        "feat(#7): milestone 0",
        "test(#7): failing tests for milestone 0",
    ], "the satisfied milestone added no commit of its own"


@pytest.mark.asyncio
async def test_a_vacuous_tests_turn_still_fails_on_a_resumed_branch(tmp_path):
    """171 FR-012. Prior commits somewhere on the branch never excuse a test the
    model wrote that passes with none of this milestone's code behind it."""
    repo, _ = _repo(tmp_path)
    _card_commit(repo, {"src/m0.py": "M0 = True\n"}, "feat(#7): milestone 0")
    _card_commit(repo, {"tests/test_m0.py": "EXPECTS src/m0.py\n"}, "test(#7): failing tests for milestone 0")
    start = _head(repo)

    def vacuous(repo, brief):
        _write(repo, "tests/test_m1.py", "EXPECTS src/base.py\n")  # passes without src/m1.py

    harness = Harness(repo, {"TESTS": vacuous, "REPAIR_TESTS": vacuous, "IMPLEMENT": _impl_turn})
    edges = Edges()
    report, _ = await _run(repo, _score(milestones=_milestones(2)), harness, edges)
    run = report["implementer_run"]

    assert run["status"] == "partial_progress" and "red was not observed" in run["reason"]
    assert run["resumed_from_milestone"] == 1, "milestone zero was still skipped as done"
    assert [b["persona_kind"] for b in harness.briefs] == ["TESTS", "REPAIR_TESTS"]
    assert _head(repo) == start and edges.pushes == 0


@pytest.mark.asyncio
async def test_a_passing_test_from_the_base_branch_is_not_a_previous_run(tmp_path):
    """171 FR-002. A brief may name a test file the base branch already has and
    passes; that is not evidence an earlier run of this card did the milestone,
    so a tests turn that writes nothing still fails."""
    repo, _ = _repo(tmp_path)
    # committed on main, not by this card
    (repo / "tests" / "test_m0.py").write_text("EXPECTS src/base.py\n")
    _sh([*_G, "add", "."], repo)
    _sh([*_G, "commit", "-q", "-m", "add coverage"], repo)
    _sh([*_G, "checkout", "-q", "main"], repo)
    _sh([*_G, "merge", "-q", "--ff-only", "feat/x"], repo)
    _sh([*_G, "push", "-q", "origin", "main"], repo)
    _sh([*_G, "checkout", "-q", "feat/x"], repo)

    harness = Harness(repo, {"TESTS": lambda r, b: None, "REPAIR_TESTS": lambda r, b: None, "IMPLEMENT": _impl_turn})
    edges = Edges()
    report, _ = await _run(repo, _score(milestones=_milestones(1)), harness, edges)
    run = report["implementer_run"]

    assert run["status"] == "partial_progress" and "red was not observed" in run["reason"]
    assert run["resumed_from_milestone"] is None
    assert [b["persona_kind"] for b in harness.briefs] == ["TESTS", "REPAIR_TESTS"]
    assert edges.pushes == 0


@pytest.mark.asyncio
async def test_next_focus_never_names_a_milestone_this_run_skipped(tmp_path):
    """171 review: main.py relays next_focus to the next dispatch, so pointing it
    at a milestone a previous run already finished is misleading guidance."""
    repo, _ = _repo(tmp_path)
    score = _score(milestones=_milestones(1))
    first = Harness(repo, {"TESTS": _tests_turn, "IMPLEMENT": _impl_turn})
    assert (await _run(repo, score, first, Edges()))[0]["implementer_run"]["status"] == "pr_opened"

    # everything is skipped, then the quality gate fails and cannot be repaired
    (repo / ".lint_fail").write_text("")
    _sh([*_G, "add", "."], repo)
    _sh([*_G, "commit", "-q", "-m", "lint marker"], repo)
    second = Harness(repo, {"REPAIR_QUALITY": lambda r, b: _write(r, "src/nope.py", "x\n")})
    report, _ = await _run(repo, score, second, Edges())
    run = report["implementer_run"]

    assert run["status"] == "partial_progress" and "lint" in run["reason"]
    assert run["next_focus_milestone"] is None, "no milestone ran, so none is the focus"


@pytest.mark.asyncio
async def test_an_unreadable_branch_history_costs_the_skip_not_the_run(tmp_path):
    """171 review: run() has no generic exception handler, so an error in the
    resume read would abort a run that the resume rule only ever optimises."""
    repo, _ = _repo(tmp_path)

    async def boom(*_a, **_k):
        raise TimeoutError("git command timed out after 120.0s: git log")

    from performer.workflows.implementer import commits as git_mod

    original = git_mod.branch_commit_entries
    git_mod.branch_commit_entries = boom
    try:
        harness = Harness(repo, {"TESTS": _tests_turn, "IMPLEMENT": _impl_turn})
        report, _ = await _run(repo, _score(milestones=_milestones(1)), harness, Edges())
    finally:
        git_mod.branch_commit_entries = original

    run = report["implementer_run"]
    assert run["status"] == "pr_opened", run["reason"]
    assert run["resumed_from_milestone"] is None
    assert [b["persona_kind"] for b in harness.briefs] == ["TESTS", "IMPLEMENT"], "the full plan ran"

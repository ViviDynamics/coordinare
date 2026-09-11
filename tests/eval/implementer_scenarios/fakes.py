"""Shared fake infrastructure for implementer scenario eval (spec 167 SC-006).

A bare remote, a clone on the card branch, a scripted fake harness that edits
files per turn kind, and a fake test runner whose verdicts follow the files
that exist. The workflow's outside edges (push, PR, check runs, logs, sleep)
are injected fakes.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

_G = ["git", "-c", "user.email=e@x", "-c", "user.name=t"]


def _sh(args, cwd):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout


def _repo(tmp_path: Path) -> tuple[Path, Path]:
    """Set up a bare remote and a clone on a feature branch."""
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
    """Get commit log messages from the feature branch."""
    return [line for line in _sh(["git", "log", "--format=%s", "main..HEAD"], repo).splitlines() if line]


def _head(repo: Path) -> str:
    """Get the current commit SHA."""
    return _sh(["git", "rev-parse", "HEAD"], repo).strip()


def _fake_test_runner(repo: Path):
    """Fake runner: verdicts follow files; lint follows a marker file."""

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
    """Scripted fake agent_turn_runner keyed by persona kind."""

    def __init__(self, repo: Path, script: dict) -> None:
        self.repo = repo
        self.script = script
        self.briefs = []

    async def __call__(self, brief: dict, *, timeout_s: float) -> dict:
        self.briefs.append(brief)
        action = self.script.get(brief["persona_kind"])
        output = ""
        if action is not None:
            output = action(self.repo, brief) or ""
        return {
            "exit_state": "done",
            "output_tail": output,
            "changed_paths": [],
            "wall_ms": 5,
            "harness_commits": [],
        }


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
            "push": self.push,
            "open_or_update_pr": self.open_or_update_pr,
            "get_check_runs": self.get_check_runs,
            "get_check_run_logs": self.get_check_run_logs,
            "sleep": self.sleep,
            "poll_interval_s": 0.0,
        }
        base.update(extra)
        return base


def _write(repo: Path, rel: str, text: str) -> None:
    """Write a file in the repo, creating directories as needed."""
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def stub_model_call():
    """The stub answer to the implementer's two model calls (#365).

    The implementer used to be the only workflow that never touched the model
    layer, and three separate harnesses each asserted that as an invariant:
    this eval runner, its pytest wrapper, and the unit end-to-end fixture. The
    workflow now reads the runner's output and judges the red, so all three had
    to answer those calls -- which is exactly why the answer lives here once
    rather than being written out three times. Two of the three copies were
    found by CI, one at a time, after each earlier fix reported green.

    It reads the fake runner's own PASSED/FAILED lines, so the stub and the
    fake it is reading cannot drift apart, and judges red the way a pair would:
    a test file the turn just changed is among the failures.
    """
    import json

    from performer.workflows.budget import ModelReply

    async def model_call(persona, content, max_tokens):
        text = "".join(c.get("text", "") for c in content)
        if "reading the raw output of a test run" in persona:
            failed = [ln.split(" ", 1)[1].strip() for ln in text.splitlines() if ln.startswith("FAILED ")]
            passed = [ln.split(" ", 1)[1].strip() for ln in text.splitlines() if ln.startswith("PASSED ")]
            outcome = "assertion_failure" if failed else ("all_passed" if passed else "no_tests_ran")
            return ModelReply(
                content=json.dumps({
                    "outcome": outcome, "failed": failed, "passed": passed,
                    "load_errors": [], "environment_problem": "", "summary": "",
                }),
                finish_reason="stop",
            )
        if "pair-programming, working test-first" in persona:
            changed = [ln[2:] for ln in text.splitlines() if ln.startswith("- ")]
            failed_line = next((ln for ln in text.splitlines() if ln.strip().startswith("failed:")), "")
            is_red = bool(changed) and any(c.split("::")[0] in failed_line for c in changed)
            return ModelReply(
                content=json.dumps({
                    "is_expected_red": is_red,
                    "reason": "a changed test file failed" if is_red else "no changed test file failed",
                    "next_action": "write_the_code" if is_red else "rewrite_the_test",
                }),
                finish_reason="stop",
            )
        raise AssertionError(f"unexpected model call: {persona[:60]}")

    return model_call

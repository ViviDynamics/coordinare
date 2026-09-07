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

"""Generate a scenario repository at run time (spec 164 T044, R8).

Never commit a .git directory inside this repository: it is a hazard for
clones, tooling and CI checkout. Generating also makes the base -> head
relationship explicit and reviewable as data rather than as opaque history.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from tests.eval.qa_scenarios.fixtures import ScenarioFixture


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        env={
            "GIT_AUTHOR_NAME": "qa-eval", "GIT_AUTHOR_EMAIL": "qa@eval.local",
            "GIT_COMMITTER_NAME": "qa-eval", "GIT_COMMITTER_EMAIL": "qa@eval.local",
            "PATH": "/usr/bin:/bin:/usr/local/bin",
        },
    )


def _write(repo: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)


def build(fixture: ScenarioFixture, dest: Path) -> tuple[Path, str, str]:
    """Materialise *fixture* at *dest*. Returns (repo, base_sha, head_sha)."""
    repo = dest / fixture.name
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")

    _write(repo, fixture.base_files)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()

    # head_files replaces the tree: a key absent from head_files but present in
    # base_files is DELETED, which is how the regression scenarios express a
    # silently removed file.
    for existing in list(fixture.base_files):
        if existing not in fixture.head_files:
            (repo / existing).unlink(missing_ok=True)
    # The head commit goes on a FEATURE BRANCH, leaving main at the base. A real
    # pull request diverges from its base branch; committing both to main makes
    # `git merge-base HEAD main` return HEAD itself, so the baseline worktree is
    # identical to head and no regression can ever be detected.
    _git(repo, "checkout", "-q", "-b", "feature")
    _write(repo, fixture.head_files)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", fixture.claimed_change or "head")
    head_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()

    return repo, base_sha, head_sha

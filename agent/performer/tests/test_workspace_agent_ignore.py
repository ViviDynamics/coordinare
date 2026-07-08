"""131: real-git tests for agent-config commit blocking.

US1 — the .git/info/exclude writer stops a broad `git add -A` from staging agent
tool-config dirs (without touching the repo's tracked .gitignore).
US2 — the commit/push guard strips agent-config paths that got committed anyway
(e.g. `git add -f`), leaving the real change intact.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from performer.models import Stand
from performer.workspace import _write_agent_ignore, strip_agent_artifacts


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def _init_repo(tmp: Path) -> None:
    _git(tmp, "init", "-q")
    _git(tmp, "config", "user.email", "t@t")
    _git(tmp, "config", "user.name", "t")


def _staged(tmp: Path) -> set[str]:
    out = subprocess.run(
        ["git", "diff", "--cached", "--name-only"], cwd=tmp, capture_output=True, text=True
    ).stdout
    return {ln for ln in out.splitlines() if ln}


def _tracked(tmp: Path) -> set[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=tmp, capture_output=True, text=True
    ).stdout
    return {ln for ln in out.splitlines() if ln}


# ---- US1: prevention (never staged) -------------------------------------------

def test_exclude_writer_blocks_agent_dirs_from_broad_add(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_agent_ignore(tmp_path)

    # agent tool-config dirs + a real change + legitimate dot-paths
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "session.json").write_text("junk")
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "state").write_text("junk")
    (tmp_path / "app.py").write_text("print('real change')")
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "ci.yml").write_text("on: push")
    (tmp_path / ".env.example").write_text("KEY=")

    _git(tmp_path, "add", "-A")
    staged = _staged(tmp_path)

    assert "app.py" in staged
    assert ".github/ci.yml" in staged      # legit dot-dir NOT excluded
    assert ".env.example" in staged        # legit dotfile NOT excluded
    assert not any(p.startswith(".codex/") for p in staged)
    assert not any(p.startswith(".claude/") for p in staged)
    # FR-004: the repo's tracked .gitignore is never created/modified
    assert not (tmp_path / ".gitignore").exists()


def test_exclude_writer_skips_when_gitdir_is_a_file(tmp_path: Path) -> None:
    # Adversarial finding (worktrees): a `.git` FILE (gitlink) must not crash the
    # writer or create a bogus dir — it skips (the push guard backstops such repos).
    (tmp_path / ".git").write_text("gitdir: /somewhere/else/.git/worktrees/x\n")
    _write_agent_ignore(tmp_path)  # must not raise
    assert (tmp_path / ".git").is_file()  # untouched; no `.git/info/` dir created


def test_exclude_writer_is_idempotent(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_agent_ignore(tmp_path)
    first = (tmp_path / ".git" / "info" / "exclude").read_text()
    _write_agent_ignore(tmp_path)
    second = (tmp_path / ".git" / "info" / "exclude").read_text()
    assert first == second  # no duplicate append


# ---- US2: guard (never merged) ------------------------------------------------

def test_guard_strips_force_added_agent_artifacts(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_agent_ignore(tmp_path)
    (tmp_path / "app.py").write_text("real")
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "session.json").write_text("junk")
    _git(tmp_path, "add", "app.py")
    _git(tmp_path, "add", "-f", ".codex/session.json")  # force past the exclude
    _git(tmp_path, "commit", "-qm", "work + junk")
    assert ".codex/session.json" in _tracked(tmp_path)

    stand = Stand(path=tmp_path, branch="main")
    stripped = asyncio.run(strip_agent_artifacts(stand, dict(os.environ)))

    assert stripped == [".codex/session.json"]
    assert ".codex/session.json" not in _tracked(tmp_path)  # removed from the tree
    assert "app.py" in _tracked(tmp_path)                   # real change survives


def test_guard_is_noop_on_clean_tree(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "app.py").write_text("real")
    _git(tmp_path, "add", "app.py")
    _git(tmp_path, "commit", "-qm", "clean")
    head_before = _git(tmp_path, "rev-parse", "HEAD").strip()

    stand = Stand(path=tmp_path, branch="main")
    stripped = asyncio.run(strip_agent_artifacts(stand, dict(os.environ)))

    assert stripped == []
    assert _git(tmp_path, "rev-parse", "HEAD").strip() == head_before  # no new commit


@pytest.mark.parametrize("legit", [".github", "docs/claude-guide"])
def test_guard_does_not_strip_legitimate_paths(tmp_path: Path, legit: str) -> None:
    _init_repo(tmp_path)
    p = tmp_path / legit
    p.mkdir(parents=True)
    (p / "file.txt").write_text("legit")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "legit")

    stand = Stand(path=tmp_path, branch="main")
    stripped = asyncio.run(strip_agent_artifacts(stand, dict(os.environ)))
    assert stripped == []
    assert f"{legit}/file.txt" in _tracked(tmp_path)

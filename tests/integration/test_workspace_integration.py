"""Integration tests for agent workspace management (spec 011).

All tests use a local bare git repository — no network access or GitHub token required.

Each integration test that invokes the real ``git`` binary is decorated with
``@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")``
so tests skip gracefully rather than erroring in environments without git.
"""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import coordinare.workspace as _ws_module
from coordinare.workspace import (
    WorkspaceManager,
    WorkspaceSetupError,
    _GitCommandError,
    make_branch_name,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_config(
    *,
    workspace_root: Path | None = None,
    agent_transport: str = "subprocess",
    github_token: str = "dummy-token",
) -> MagicMock:
    cfg = MagicMock()
    cfg.github_org = "test-org"
    cfg.project_name = "test-repo"
    cfg.github_token = MagicMock()
    cfg.github_token.get_secret_value.return_value = github_token
    cfg.workspace_root = workspace_root
    cfg.agent_transport = agent_transport
    return cfg


@pytest.fixture()
def bare_repo(tmp_path: Path):
    """Create a local bare git repo with at least one commit, usable as a clone source."""
    bare = tmp_path / "bare.git"
    bare.mkdir()
    import subprocess
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
    # Create a working clone to add an initial commit
    work = tmp_path / "work"
    subprocess.run(["git", "clone", str(bare), str(work)], check=True, capture_output=True)
    (work / "README.md").write_text("hello")
    subprocess.run(["git", "-C", str(work), "config", "user.email", "test@test.com"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(work), "config", "user.name", "Test"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(work), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(work), "commit", "-m", "init"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(work), "push", "origin", "HEAD"], check=True, capture_output=True)
    return bare


def _redirect_clone_to_local(bare_repo: Path):
    """Return a ``_run_git`` side-effect that redirects ``git clone`` to a local bare repo.

    All other git sub-commands are forwarded to the real implementation unchanged.
    ``GIT_CONFIG_NOSYSTEM=1`` is injected into every call to avoid credential-helper
    interference when running against a local path.
    """
    original = _ws_module._run_git

    async def _local_run_git(*args, **kwargs):
        if args[0] == "clone":
            # Replace the authenticated remote URL (args[2]) with the local bare repo.
            # args layout in prepare(): ("clone", "--depth=1", <remote-url>, <clone-dir>)
            args = (args[0], args[1], str(bare_repo), *args[3:])
        env = dict(kwargs.get("env") or {})
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        return await original(*args, **{**kwargs, "env": env})

    return _local_run_git


# ---------------------------------------------------------------------------
# Scenario 1: Happy path — workspace created, branch correct, teardown works
# (T014, T019)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
@pytest.mark.asyncio
async def test_prepare_creates_workspace_on_correct_branch(
    bare_repo: Path, tmp_path: Path,
) -> None:
    """Scenario 1: prepare() clones, checks out the correct branch; teardown removes dir."""
    import subprocess

    ws_root = tmp_path / "workspaces"
    ws_root.mkdir()

    cfg = _make_config(workspace_root=ws_root)
    mgr = WorkspaceManager(cfg)

    card = {"id": "TEST_1", "title": "Add retry logic"}
    with patch("coordinare.workspace._run_git", side_effect=_redirect_clone_to_local(bare_repo)):
        info = await mgr.prepare(card)

    expected_branch = make_branch_name("TEST_1", "Add retry logic")
    assert info.branch == expected_branch
    assert info.path is not None
    assert info.path.exists()

    # Verify actual branch checked out in the clone
    result = subprocess.run(
        ["git", "-C", str(info.path), "branch", "--show-current"],
        capture_output=True, text=True,
    )
    assert result.stdout.strip() == expected_branch

    # Teardown removes the directory
    await mgr.teardown(info.path)
    assert not info.path.exists()


# ---------------------------------------------------------------------------
# Scenario 2: Clone failure → WorkspaceSetupError, no orphaned directory
# (T011 — covered in unit tests; here we verify at integration level)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prepare_raises_on_clone_failure_cleans_up(tmp_path: Path) -> None:
    """Scenario 2: clone failure raises WorkspaceSetupError, no orphan dirs left."""
    ws_root = tmp_path / "workspaces"
    ws_root.mkdir()

    cfg = _make_config(workspace_root=ws_root)
    mgr = WorkspaceManager(cfg)

    async def _fail_on_clone(*args, **kwargs):
        if args[0] == "clone":
            raise _GitCommandError("simulated: repository not found")

    with patch("coordinare.workspace._run_git", side_effect=_fail_on_clone), pytest.raises(WorkspaceSetupError):
        await mgr.prepare({"id": "TEST_2", "title": "Fix bug"})

    remaining = list(ws_root.glob("coordinare-ws-*"))
    assert remaining == [], f"Orphaned directories found: {remaining}"


# ---------------------------------------------------------------------------
# Scenario 3: Concurrent workspaces are isolated (T022)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
@pytest.mark.asyncio
async def test_concurrent_workspaces_are_isolated(
    bare_repo: Path, tmp_path: Path,
) -> None:
    """Scenario 3: two concurrent prepare() calls produce distinct isolated dirs."""
    ws_root = tmp_path / "workspaces"
    ws_root.mkdir()

    cfg = _make_config(workspace_root=ws_root)
    mgr = WorkspaceManager(cfg)

    with patch("coordinare.workspace._run_git", side_effect=_redirect_clone_to_local(bare_repo)):
        info_a, info_b = await asyncio.gather(
            mgr.prepare({"id": "CARD_A", "title": "Feature A"}),
            mgr.prepare({"id": "CARD_B", "title": "Feature B"}),
        )

    assert info_a.path != info_b.path
    assert info_a.path is not None and info_a.path.exists()
    assert info_b.path is not None and info_b.path.exists()

    await mgr.teardown(info_a.path)
    await mgr.teardown(info_b.path)
    assert not info_a.path.exists()
    assert not info_b.path.exists()


# ---------------------------------------------------------------------------
# Scenario 4: Deterministic branch naming (T014 unit-level; T013 in unit tests)
# ---------------------------------------------------------------------------


def test_make_branch_name_deterministic_integration() -> None:
    """Scenario 4: same card ID and title always produce the same branch name."""
    results = [make_branch_name("PVTI_abc", "Add retry logic") for _ in range(3)]
    assert len(set(results)) == 1
    assert results[0] == "coordinare/PVTI_abc/add-retry-logic"


# ---------------------------------------------------------------------------
# Scenario 5: Teardown survives missing directory (T019)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_teardown_missing_directory_no_raise(tmp_path: Path) -> None:
    """Scenario 5: teardown() on nonexistent path logs warning but does not raise."""
    cfg = _make_config()
    mgr = WorkspaceManager(cfg)
    nonexistent = tmp_path / "coordinare-ws-does-not-exist"
    assert not nonexistent.exists()
    await mgr.teardown(nonexistent)  # must not raise


@pytest.mark.asyncio
async def test_teardown_full_lifecycle(tmp_path: Path) -> None:
    """Scenario 5 (part 2): teardown() removes an existing directory."""
    cfg = _make_config()
    mgr = WorkspaceManager(cfg)
    ws = tmp_path / "coordinare-ws-lifecycle"
    ws.mkdir()
    assert ws.exists()
    await mgr.teardown(ws)
    assert not ws.exists()


# ---------------------------------------------------------------------------
# Scenario 6: workspace_root PVC simulation (T021)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
@pytest.mark.asyncio
async def test_workspace_root_pvc_simulation(
    bare_repo: Path, tmp_path: Path,
) -> None:
    """Scenario 6: workspace_root directs workspace creation to a specified parent."""
    pvc_mount = tmp_path / "pvc"
    pvc_mount.mkdir()

    cfg = _make_config(workspace_root=pvc_mount)
    mgr = WorkspaceManager(cfg)

    with patch("coordinare.workspace._run_git", side_effect=_redirect_clone_to_local(bare_repo)):
        info = await mgr.prepare({"id": "TEST_6", "title": "Feature"})

    assert info.path is not None

    # Workspace must be under pvc_mount
    assert str(info.path).startswith(str(pvc_mount))

    # Teardown removes only workspace subdir, not pvc_mount itself
    await mgr.teardown(info.path)
    assert not info.path.exists()
    assert pvc_mount.exists()


@pytest.mark.asyncio
async def test_resumed_pr_preserves_commits_and_pushes_same_head(bare_repo: Path, tmp_path: Path) -> None:
    """A renamed card resumes prior work and updates only the actual PR branch."""
    import subprocess
    from unittest.mock import AsyncMock

    def git_at(path: Path, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(path), *args], check=True, capture_output=True, text=True,
        ).stdout.strip()

    original_work = tmp_path / "work"
    base_sha = git_at(original_work, "rev-parse", "HEAD")
    branch = "conductor/CARD/old-title"
    git_at(original_work, "checkout", "-b", branch)
    (original_work / "previous-work.txt").write_text("preserve this PR work")
    git_at(original_work, "add", ".")
    git_at(original_work, "commit", "-m", "Prior implementation")
    prior_sha = git_at(original_work, "rev-parse", "HEAD")
    git_at(original_work, "push", "origin", branch)

    github = MagicMock()
    github.check_mergeability = AsyncMock(return_value={"head_ref_name": branch})
    mgr = WorkspaceManager(_make_config(workspace_root=tmp_path), github_service=github)
    original_run_git = _ws_module._run_git

    async def local_remote(*args, **kwargs):
        if args[0] == "clone":
            args = ("clone", "--depth=1", str(bare_repo), *args[3:])
        elif args[:3] == ("remote", "set-url", "origin"):
            args = (*args[:3], str(bare_repo))
        return await original_run_git(*args, **kwargs)

    with patch("coordinare.workspace._run_git", side_effect=local_remote):
        info = await mgr.prepare({"id": "CARD", "title": "New title", "pr_node_id": "PR_retained"})
    assert info.path is not None
    assert info.branch == branch
    assert git_at(info.path, "branch", "--show-current") == branch
    assert git_at(info.path, "rev-parse", "HEAD") == prior_sha
    assert (info.path / "previous-work.txt").read_text() == "preserve this PR work"
    (info.path / "feedback.txt").write_text("new feedback change")
    git_at(info.path, "add", ".")
    git_at(info.path, "commit", "-m", "Address feedback")
    git_at(info.path, "push", "origin", branch)
    assert git_at(bare_repo, "rev-parse", f"refs/heads/{branch}") == git_at(info.path, "rev-parse", "HEAD")
    assert git_at(bare_repo, "rev-parse", "HEAD") == base_sha
    assert not git_at(bare_repo, "branch", "--list", "coordinare/*")
    await mgr.teardown(info.path)

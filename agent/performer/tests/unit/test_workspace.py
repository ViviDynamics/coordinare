"""Unit tests for performer.workspace."""
from __future__ import annotations

import errno
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from performer.models import Score, Stand
from performer.workspace import (
    CIRunResult,
    WorkspaceSetupError,
    _git_credential_env,
    _redact_auth_headers,
    cleanup_stand,
    clone_repository,
    get_head_sha,
    push_branch,
    run_command,
)


def _score(**kwargs) -> Score:  # type: ignore[type-arg]
    defaults = dict(
        title="T",
        repo_url="https://github.com/org/repo",
        branch="feat/x",
        github_token="ghp_secret",
    )
    defaults.update(kwargs)
    return Score(**defaults)


class TestGitCredentialEnv:
    def test_token_in_extra_header_not_in_url(self) -> None:
        import base64
        env = _git_credential_env("ghp_mytoken")
        expected = "Authorization: Basic " + base64.b64encode(b"x-access-token:ghp_mytoken").decode()
        assert env["GIT_CONFIG_VALUE_0"] == expected
        assert env["GIT_CONFIG_KEY_0"] == "http.extraHeader"

    def test_terminal_prompt_disabled(self) -> None:
        env = _git_credential_env("tok")
        assert env["GIT_TERMINAL_PROMPT"] == "0"

    def test_inherits_existing_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MY_CUSTOM_VAR", "present")
        env = _git_credential_env("tok")
        assert env["MY_CUSTOM_VAR"] == "present"


class TestCloneRepository:
    @pytest.fixture
    def mock_proc(self) -> MagicMock:
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        return proc

    async def test_successful_clone_returns_stand(self, tmp_path: Path, mock_proc: MagicMock) -> None:
        with (
            patch("performer.workspace.asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec,
            patch("performer.workspace.tempfile.mkdtemp", return_value=str(tmp_path)),
        ):
            stand = await clone_repository(_score())
        assert stand.branch == "feat/x"
        assert stand.path == tmp_path
        # Token must NOT appear in the command-line args — it goes via env vars
        call_args = mock_exec.call_args[0]
        assert all("ghp_secret" not in str(arg) for arg in call_args)

    async def test_token_passed_via_env_not_url(self, tmp_path: Path, mock_proc: MagicMock) -> None:
        import base64
        with (
            patch("performer.workspace.asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec,
            patch("performer.workspace.tempfile.mkdtemp", return_value=str(tmp_path)),
        ):
            await clone_repository(_score())
        kwargs = mock_exec.call_args[1]
        env = kwargs.get("env", {})
        raw = env.get("GIT_CONFIG_VALUE_0", "")
        # Value is "Authorization: Basic <base64(x-access-token:TOKEN)>"
        assert raw.startswith("Authorization: Basic ")
        decoded = base64.b64decode(raw.removeprefix("Authorization: Basic ")).decode()
        assert "ghp_secret" in decoded

    async def test_failed_clone_raises_workspace_error(self, tmp_path: Path) -> None:
        proc = MagicMock()
        proc.returncode = 128
        proc.communicate = AsyncMock(
            return_value=(b"", b"fatal: repository not found")
        )
        with (
            patch("performer.workspace.asyncio.create_subprocess_exec", return_value=proc),
            patch("performer.workspace.tempfile.mkdtemp", return_value=str(tmp_path)),
        ):
            with pytest.raises(WorkspaceSetupError):
                await clone_repository(_score())

    async def test_disk_full_raises_workspace_error(self, tmp_path: Path) -> None:
        os_error = OSError(errno.ENOSPC, "No space left on device")
        with (
            patch("performer.workspace.asyncio.create_subprocess_exec", side_effect=os_error),
            patch("performer.workspace.tempfile.mkdtemp", return_value=str(tmp_path)),
        ):
            with pytest.raises(WorkspaceSetupError, match="insufficient disk space"):
                await clone_repository(_score())

    async def test_mkdtemp_disk_full_raises_workspace_error(self, tmp_path: Path) -> None:
        os_error = OSError(errno.ENOSPC, "No space left on device")
        with patch("performer.workspace.tempfile.mkdtemp", side_effect=os_error):
            with pytest.raises(WorkspaceSetupError, match="insufficient disk space"):
                await clone_repository(_score())

    async def test_mkdtemp_other_oserror_raises_workspace_error(self, tmp_path: Path) -> None:
        os_error = OSError(errno.EPERM, "Permission denied")
        with patch("performer.workspace.tempfile.mkdtemp", side_effect=os_error):
            with pytest.raises(WorkspaceSetupError, match="failed to create temporary workspace"):
                await clone_repository(_score())

    async def test_clone_non_enospc_oserror_raises_workspace_error(self, tmp_path: Path) -> None:
        os_error = OSError(errno.ECONNREFUSED, "Connection refused")
        with (
            patch("performer.workspace.asyncio.create_subprocess_exec", side_effect=os_error),
            patch("performer.workspace.tempfile.mkdtemp", return_value=str(tmp_path)),
        ):
            with pytest.raises(WorkspaceSetupError, match="clone failed"):
                await clone_repository(_score())

    async def test_checkout_fails_raises_workspace_error(self, tmp_path: Path) -> None:
        """Clone succeeds, unshallow/fetch run, then checkout fails."""
        proc_ok = MagicMock()
        proc_ok.returncode = 0
        proc_ok.communicate = AsyncMock(return_value=(b"", b""))

        proc_fetch_missing = MagicMock()
        proc_fetch_missing.returncode = 128
        proc_fetch_missing.communicate = AsyncMock(
            return_value=(b"", b"fatal: couldn't find remote ref")
        )

        proc_fail = MagicMock()
        proc_fail.returncode = 1
        proc_fail.communicate = AsyncMock(return_value=(b"", b"error: checkout failed"))

        with (
            patch(
                "performer.workspace.asyncio.create_subprocess_exec",
                # clone ok, unshallow ok, fetch fail (missing ref), checkout -b fail
                side_effect=[proc_ok, proc_ok, proc_fetch_missing, proc_fail],
            ),
            patch("performer.workspace.tempfile.mkdtemp", return_value=str(tmp_path)),
        ):
            with pytest.raises(WorkspaceSetupError, match="git checkout failed"):
                await clone_repository(_score())

    async def test_checkout_oserror_raises_workspace_error(self, tmp_path: Path) -> None:
        """Clone/unshallow/fetch succeed; checkout raises OSError."""
        proc_ok = MagicMock()
        proc_ok.returncode = 0
        proc_ok.communicate = AsyncMock(return_value=(b"", b""))

        proc_fetch_missing = MagicMock()
        proc_fetch_missing.returncode = 128
        proc_fetch_missing.communicate = AsyncMock(
            return_value=(b"", b"fatal: couldn't find remote ref")
        )

        os_error = OSError(errno.EIO, "I/O error")

        with (
            patch(
                "performer.workspace.asyncio.create_subprocess_exec",
                # clone ok, unshallow ok, fetch fail (missing ref), checkout OSError
                side_effect=[proc_ok, proc_ok, proc_fetch_missing, os_error],
            ),
            patch("performer.workspace.tempfile.mkdtemp", return_value=str(tmp_path)),
        ):
            with pytest.raises(WorkspaceSetupError, match="git checkout failed"):
                await clone_repository(_score())

    async def test_fetch_unexpected_error_raises_workspace_error(self, tmp_path: Path) -> None:
        """Fetch fails with unexpected error (not missing ref) — raises immediately."""
        proc_ok = MagicMock()
        proc_ok.returncode = 0
        proc_ok.communicate = AsyncMock(return_value=(b"", b""))

        proc_fetch_auth_fail = MagicMock()
        proc_fetch_auth_fail.returncode = 128
        proc_fetch_auth_fail.communicate = AsyncMock(
            return_value=(b"", b"fatal: Authentication failed")
        )

        with (
            patch(
                "performer.workspace.asyncio.create_subprocess_exec",
                side_effect=[proc_ok, proc_ok, proc_fetch_auth_fail],
            ),
            patch("performer.workspace.tempfile.mkdtemp", return_value=str(tmp_path)),
        ):
            with pytest.raises(WorkspaceSetupError, match="git fetch failed unexpectedly"):
                await clone_repository(_score())

    async def test_clone_workspace_error_cleans_up_stand_path(self, tmp_path: Path) -> None:
        """WorkspaceSetupError (e.g. timeout) from _run_git during clone cleans up tmpdir."""
        stand_path = tmp_path / "stand"
        stand_path.mkdir()

        with (
            patch(
                "performer.workspace._run_git",
                new=AsyncMock(side_effect=WorkspaceSetupError("git command timed out")),
            ),
            patch("performer.workspace.tempfile.mkdtemp", return_value=str(stand_path)),
            patch("performer.workspace.shutil.rmtree") as mock_rmtree,
        ):
            with pytest.raises(WorkspaceSetupError, match="timed out"):
                await clone_repository(_score())

        mock_rmtree.assert_called_with(stand_path, ignore_errors=True)

    async def test_checkout_workspace_error_cleans_up_stand_path(self, tmp_path: Path) -> None:
        """WorkspaceSetupError (timeout) during checkout cleans up tmpdir."""
        stand_path = tmp_path / "stand"
        stand_path.mkdir()

        # Clone succeeds, unshallow ok, fetch fails (missing ref), checkout times out
        with (
            patch(
                "performer.workspace._run_git",
                new=AsyncMock(side_effect=[
                    (0, ""),   # clone OK
                    (0, ""),   # unshallow OK
                    (128, "fatal: couldn't find remote ref"), # fetch fail (branch doesn't exist)
                    WorkspaceSetupError("git command timed out"),
                ]),
            ),
            patch("performer.workspace.tempfile.mkdtemp", return_value=str(stand_path)),
            patch("performer.workspace.shutil.rmtree") as mock_rmtree,
        ):
            with pytest.raises(WorkspaceSetupError, match="timed out"):
                await clone_repository(_score())

        mock_rmtree.assert_called_with(stand_path, ignore_errors=True)


class TestPushBranch:
    async def test_successful_push(self, tmp_path: Path) -> None:
        stand = Stand(path=tmp_path, branch="feat/x")
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        with patch("performer.workspace.asyncio.create_subprocess_exec", return_value=proc):
            await push_branch(stand, _score())  # should not raise

    async def test_token_passed_via_env_not_url(self, tmp_path: Path) -> None:
        import base64
        stand = Stand(path=tmp_path, branch="feat/x")
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        with patch("performer.workspace.asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            await push_branch(stand, _score())
        call_args = mock_exec.call_args[0]
        assert all("ghp_secret" not in str(arg) for arg in call_args)
        env = mock_exec.call_args[1].get("env", {})
        raw = env.get("GIT_CONFIG_VALUE_0", "")
        assert raw.startswith("Authorization: Basic ")
        decoded = base64.b64decode(raw.removeprefix("Authorization: Basic ")).decode()
        assert "ghp_secret" in decoded

    async def test_non_fast_forward_falls_back_to_force(self, tmp_path: Path) -> None:
        """Non-fast-forward regular push triggers force-push fallback."""
        stand = Stand(path=tmp_path, branch="feat/x")
        proc_fail = MagicMock()
        proc_fail.returncode = 1
        proc_fail.communicate = AsyncMock(
            return_value=(b"", b"error: failed to push some refs\n [rejected] feat/x -> feat/x (non-fast-forward)")
        )
        proc_ok = MagicMock()
        proc_ok.returncode = 0
        proc_ok.communicate = AsyncMock(return_value=(b"", b""))
        with patch("performer.workspace.asyncio.create_subprocess_exec", side_effect=[proc_fail, proc_ok]):
            await push_branch(stand, _score())  # should succeed via force fallback

    async def test_both_push_attempts_fail_raises_workspace_error(self, tmp_path: Path) -> None:
        """When both regular and force push fail, raises WorkspaceSetupError."""
        stand = Stand(path=tmp_path, branch="feat/x")
        proc_fail = MagicMock()
        proc_fail.returncode = 1
        proc_fail.communicate = AsyncMock(return_value=(b"", b"error: network timeout"))
        with patch("performer.workspace.asyncio.create_subprocess_exec", return_value=proc_fail):
            with pytest.raises(WorkspaceSetupError):
                await push_branch(stand, _score())

    async def test_workflow_permission_push_error_is_summarized(self, tmp_path: Path) -> None:
        """Workflow-permission push rejections should be concise/actionable."""
        stand = Stand(path=tmp_path, branch="feat/x")
        verbose = (
            "17:26:10.642523 http.c:889 == Info: Request completely sent off\n"
            "To https://github.com/ViviDynamics/website.git\n"
            "! [remote rejected] HEAD -> feat/x "
            "(refusing to allow a GitHub App to create or update workflow "
            "`.github/workflows/main-branch-build.yml` without `workflows` permission)\n"
            "error: failed to push some refs to 'https://github.com/ViviDynamics/website.git'\n"
        )
        proc_fail = MagicMock()
        proc_fail.returncode = 1
        proc_fail.communicate = AsyncMock(return_value=(b"", verbose.encode()))
        with (
            patch(
                "performer.workspace.asyncio.create_subprocess_exec",
                side_effect=[proc_fail, proc_fail],
            ),
            pytest.raises(WorkspaceSetupError) as excinfo,
        ):
            await push_branch(stand, _score())
        msg = str(excinfo.value)
        assert "lacks `workflows` permission" in msg
        assert "Revert workflow-file changes and retry" in msg
        assert "http.c:889" not in msg

    async def test_push_non_enospc_oserror_raises_workspace_error(self, tmp_path: Path) -> None:
        stand = Stand(path=tmp_path, branch="feat/x")
        os_error = OSError(errno.EIO, "I/O error during push")
        with patch("performer.workspace.asyncio.create_subprocess_exec", side_effect=os_error):
            with pytest.raises(WorkspaceSetupError, match="git push failed"):
                await push_branch(stand, _score())

    async def test_push_disk_full_raises_workspace_error(self, tmp_path: Path) -> None:
        stand = Stand(path=tmp_path, branch="feat/x")
        os_error = OSError(errno.ENOSPC, "No space left on device")
        with patch("performer.workspace.asyncio.create_subprocess_exec", side_effect=os_error):
            with pytest.raises(WorkspaceSetupError, match="insufficient disk space"):
                await push_branch(stand, _score())


class TestGetHeadSha:
    async def test_returns_sha_on_success(self, tmp_path: Path) -> None:
        stand = Stand(path=tmp_path, branch="feat/x")
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"abc1234567890\n", b""))
        with patch("performer.workspace.asyncio.create_subprocess_exec", return_value=proc):
            sha = await get_head_sha(stand)
        assert sha == "abc1234567890"

    async def test_raises_on_nonzero_exit(self, tmp_path: Path) -> None:
        stand = Stand(path=tmp_path, branch="feat/x")
        proc = MagicMock()
        proc.returncode = 128
        proc.communicate = AsyncMock(return_value=(b"", b""))
        with patch("performer.workspace.asyncio.create_subprocess_exec", return_value=proc):
            with pytest.raises(WorkspaceSetupError, match="git rev-parse HEAD failed"):
                await get_head_sha(stand)

    async def test_raises_on_timeout_and_kills_proc(self, tmp_path: Path) -> None:
        stand = Stand(path=tmp_path, branch="feat/x")

        async def _hang() -> tuple[bytes, bytes]:
            import asyncio as _asyncio
            await _asyncio.sleep(9999)
            return b"", b""

        proc = MagicMock()
        proc.kill = MagicMock()
        proc.wait = AsyncMock()
        proc.communicate = _hang
        with patch("performer.workspace.asyncio.create_subprocess_exec", return_value=proc):
            with pytest.raises(WorkspaceSetupError, match="timed out"):
                await get_head_sha(stand)
        proc.kill.assert_called_once()


class TestCleanupStand:
    def test_removes_directory(self, tmp_path: Path) -> None:
        target = tmp_path / "stand"
        target.mkdir()
        (target / "file.txt").write_text("data")
        stand = Stand(path=target, branch="main")
        cleanup_stand(stand)
        assert not target.exists()

    def test_no_error_if_already_gone(self, tmp_path: Path) -> None:
        stand = Stand(path=tmp_path / "nonexistent", branch="main")
        cleanup_stand(stand)  # should not raise


class TestRedactAuthHeaders:
    def test_basic_auth_header_redacted(self) -> None:
        text = "fatal: Authorization: Basic abc123xyz in response"
        result = _redact_auth_headers(text)
        assert "abc123xyz" not in result
        assert "Authorization: <redacted>" in result

    def test_bearer_auth_header_redacted(self) -> None:
        text = "error: Authorization: Bearer ghp_mytoken — credentials rejected"
        result = _redact_auth_headers(text)
        assert "ghp_mytoken" not in result
        assert "Authorization: <redacted>" in result

    def test_no_auth_header_unchanged(self) -> None:
        text = "fatal: repository not found"
        assert _redact_auth_headers(text) == text

    def test_case_insensitive(self) -> None:
        text = "AUTHORIZATION: Basic token123"
        result = _redact_auth_headers(text)
        assert "token123" not in result


class TestGitCredentialEnvTraceDisabled:
    def test_trace_vars_disabled(self) -> None:
        env = _git_credential_env("tok")
        assert env["GIT_TRACE"] == "0"
        assert env["GIT_TRACE2"] == "0"
        assert env["GIT_TRACE_CURL"] == "0"
        assert env["GIT_CURL_VERBOSE"] == "0"


class TestRunGitTimeout:
    async def test_timeout_raises_workspace_error(self, tmp_path: Path) -> None:
        """If git hangs, _run_git raises WorkspaceSetupError after timeout."""
        from performer.workspace import _run_git

        proc = MagicMock()
        proc.kill = MagicMock()
        proc.wait = AsyncMock()

        async def _hang():
            import asyncio
            await asyncio.sleep(9999)

        proc.communicate = _hang

        env = _git_credential_env("tok")
        with patch("performer.workspace.asyncio.create_subprocess_exec", return_value=proc):
            with pytest.raises(WorkspaceSetupError, match="timed out"):
                await _run_git(["git", "clone", "https://github.com/x/y"], tmp_path, env, timeout=0.01)

        proc.kill.assert_called_once()


# ---------------------------------------------------------------------------
# 020 — commit_file tests
# ---------------------------------------------------------------------------


class TestCommitFile:
    """Tests for commit_file() helper (020)."""

    @pytest.mark.asyncio
    async def test_new_file_is_committed_and_pushed(self, tmp_path: Path) -> None:
        """commit_file writes a new file, commits, and pushes."""
        from performer.workspace import commit_file

        stand = Stand(path=tmp_path, branch="feat/test")
        stand.git_env = {}

        calls: list[list[str]] = []

        async def mock_run_git(args, cwd=None, env=None, timeout=120.0):
            calls.append(args)
            if "diff" in args:
                return (1, "")  # changes exist
            return (0, "")

        with patch("performer.workspace._run_git", side_effect=mock_run_git):
            await commit_file(stand, "docs/plan.md", "# Plan\nContent", "chore: add plan")

        assert (tmp_path / "docs" / "plan.md").read_text() == "# Plan\nContent"
        assert any("add" in c for c in calls)
        assert any("commit" in c for c in calls)
        assert any("push" in c for c in calls)

    @pytest.mark.asyncio
    async def test_existing_file_is_overwritten(self, tmp_path: Path) -> None:
        """commit_file overwrites an existing file (FR-009)."""
        from performer.workspace import commit_file

        stand = Stand(path=tmp_path, branch="feat/test")
        stand.git_env = {}

        (tmp_path / "docs").mkdir(parents=True)
        (tmp_path / "docs" / "plan.md").write_text("old content")

        async def mock_run_git(args, cwd=None, env=None, timeout=120.0):
            if "diff" in args:
                return (1, "")
            return (0, "")

        with patch("performer.workspace._run_git", side_effect=mock_run_git):
            await commit_file(stand, "docs/plan.md", "new content", "chore: update plan")

        assert (tmp_path / "docs" / "plan.md").read_text() == "new content"

    @pytest.mark.asyncio
    async def test_no_op_when_content_identical(self, tmp_path: Path) -> None:
        """commit_file skips commit/push when content is identical (idempotent)."""
        from performer.workspace import commit_file

        stand = Stand(path=tmp_path, branch="feat/test")
        stand.git_env = {}

        calls: list[list[str]] = []

        async def mock_run_git(args, cwd=None, env=None, timeout=120.0):
            calls.append(args)
            if "diff" in args:
                return (0, "")  # no changes
            return (0, "")

        with patch("performer.workspace._run_git", side_effect=mock_run_git):
            await commit_file(stand, "docs/plan.md", "same content", "chore: no-op")

        # Should not have called commit or push
        assert not any("commit" in c for c in calls)
        assert not any("push" in c for c in calls)

    @pytest.mark.asyncio
    async def test_parent_directories_created(self, tmp_path: Path) -> None:
        """commit_file creates parent directories if they don't exist."""
        from performer.workspace import commit_file

        stand = Stand(path=tmp_path, branch="feat/test")
        stand.git_env = {}

        async def mock_run_git(args, cwd=None, env=None, timeout=120.0):
            if "diff" in args:
                return (1, "")
            return (0, "")

        with patch("performer.workspace._run_git", side_effect=mock_run_git):
            await commit_file(stand, "deep/nested/dir/plan.md", "content", "chore: test")

        assert (tmp_path / "deep" / "nested" / "dir" / "plan.md").exists()

    @pytest.mark.asyncio
    async def test_commit_file_rejects_absolute_path(self, tmp_path: Path) -> None:
        """commit_file raises WorkspaceSetupError for absolute paths."""
        from performer.workspace import commit_file

        stand = Stand(path=tmp_path, branch="feat/test")
        stand.git_env = {}

        with pytest.raises(WorkspaceSetupError, match="unsafe path rejected"):
            await commit_file(stand, "/etc/plan.md", "content", "chore: test")

    @pytest.mark.asyncio
    async def test_commit_file_rejects_path_traversal(self, tmp_path: Path) -> None:
        """commit_file raises WorkspaceSetupError for path traversal attempts."""
        from performer.workspace import commit_file

        stand = Stand(path=tmp_path, branch="feat/test")
        stand.git_env = {}

        with pytest.raises(WorkspaceSetupError, match="unsafe path rejected"):
            await commit_file(stand, "../escape/plan.md", "content", "chore: test")

    @pytest.mark.asyncio
    async def test_commit_file_git_add_fails(self, tmp_path: Path) -> None:
        """commit_file raises WorkspaceSetupError when git add returns non-zero."""
        from performer.workspace import commit_file

        stand = Stand(path=tmp_path, branch="feat/test")
        stand.git_env = {}

        async def mock_run_git(args, cwd=None, env=None, timeout=120.0):
            if "add" in args:
                return (1, "error: could not add file")
            return (0, "")

        with patch("performer.workspace._run_git", side_effect=mock_run_git):
            with pytest.raises(WorkspaceSetupError, match="git add failed"):
                await commit_file(stand, "docs/plan.md", "content", "chore: test")

    @pytest.mark.asyncio
    async def test_commit_file_git_diff_error_exit(self, tmp_path: Path) -> None:
        """commit_file raises WorkspaceSetupError when git diff returns exit code > 1."""
        from performer.workspace import commit_file

        stand = Stand(path=tmp_path, branch="feat/test")
        stand.git_env = {}

        async def mock_run_git(args, cwd=None, env=None, timeout=120.0):
            if "diff" in args:
                return (2, "fatal: bad revision")
            return (0, "")

        with patch("performer.workspace._run_git", side_effect=mock_run_git):
            with pytest.raises(WorkspaceSetupError, match="git diff --cached failed"):
                await commit_file(stand, "docs/plan.md", "content", "chore: test")

    @pytest.mark.asyncio
    async def test_commit_file_git_commit_fails(self, tmp_path: Path) -> None:
        """commit_file raises WorkspaceSetupError when git commit returns non-zero."""
        from performer.workspace import commit_file

        stand = Stand(path=tmp_path, branch="feat/test")
        stand.git_env = {}

        async def mock_run_git(args, cwd=None, env=None, timeout=120.0):
            if "diff" in args:
                return (1, "")  # changes exist
            if "commit" in args:
                return (1, "error: commit failed")
            return (0, "")

        with patch("performer.workspace._run_git", side_effect=mock_run_git):
            with pytest.raises(WorkspaceSetupError, match="git commit failed"):
                await commit_file(stand, "docs/plan.md", "content", "chore: test")

    @pytest.mark.asyncio
    async def test_commit_file_git_push_fails(self, tmp_path: Path) -> None:
        """commit_file raises WorkspaceSetupError when git push returns non-zero."""
        from performer.workspace import commit_file

        stand = Stand(path=tmp_path, branch="feat/test")
        stand.git_env = {}

        async def mock_run_git(args, cwd=None, env=None, timeout=120.0):
            if "diff" in args:
                return (1, "")  # changes exist
            if "push" in args:
                return (1, "error: push rejected")
            return (0, "")

        with patch("performer.workspace._run_git", side_effect=mock_run_git):
            with pytest.raises(WorkspaceSetupError, match="git push failed"):
                await commit_file(stand, "docs/plan.md", "content", "chore: test")


# ---------------------------------------------------------------------------
# 043 — run_command tests
# ---------------------------------------------------------------------------


class TestRunCommand:
    @pytest.mark.asyncio
    async def test_success_returns_zero_exit(self, tmp_path: Path) -> None:
        result = await run_command("echo hello", tmp_path)
        assert result.success is True
        assert result.exit_code == 0
        assert "hello" in result.stdout
        assert result.command == "echo hello"
        assert result.duration_seconds >= 0

    @pytest.mark.asyncio
    async def test_failure_returns_nonzero_exit(self, tmp_path: Path) -> None:
        result = await run_command("exit 1", tmp_path)
        assert result.success is False
        assert result.exit_code == 1

    @pytest.mark.asyncio
    async def test_timeout_returns_failure(self, tmp_path: Path) -> None:
        result = await run_command("sleep 10", tmp_path, timeout=1)
        assert result.success is False
        assert result.exit_code == -1
        assert "timed out" in result.stderr.lower()

    @pytest.mark.asyncio
    async def test_stdout_truncated(self, tmp_path: Path) -> None:
        # Generate output longer than _MAX_OUTPUT (2000 chars)
        result = await run_command("python3 -c \"print('x' * 5000)\"", tmp_path)
        assert result.success is True
        assert len(result.stdout) <= 2000

    @pytest.mark.asyncio
    async def test_stderr_captured(self, tmp_path: Path) -> None:
        result = await run_command("python3 -c \"import sys; sys.stderr.write('err')\"", tmp_path)
        assert "err" in result.stderr

    @pytest.mark.asyncio
    async def test_result_is_frozen_dataclass(self, tmp_path: Path) -> None:
        result = await run_command("echo ok", tmp_path)
        assert isinstance(result, CIRunResult)
        with pytest.raises(AttributeError):
            result.success = False  # type: ignore[misc]


class TestActivateEnvCache:
    """Tests for _activate_env_cache (spec 060 Option B)."""

    @pytest.mark.asyncio
    async def test_empty_path_returns_empty(self) -> None:
        from performer.workspace import _activate_env_cache
        assert await _activate_env_cache("") == {}

    @pytest.mark.asyncio
    async def test_missing_activate_warns_and_returns_empty(
        self, tmp_path: Path
    ) -> None:
        from performer.workspace import _activate_env_cache
        # Cache dir exists but no activate.sh
        result = await _activate_env_cache(str(tmp_path))
        assert result == {}

    @pytest.mark.asyncio
    async def test_sources_activate_and_returns_delta(
        self, tmp_path: Path
    ) -> None:
        from performer.workspace import _activate_env_cache
        activate = tmp_path / "activate.sh"
        activate.write_text(
            'export PATH="/cache/bin:$PATH"\n'
            'export VIRTUAL_ENV="/cache/.venv"\n'
            'export TEST_CACHE_TOKEN="abc123"\n'
        )
        result = await _activate_env_cache(str(tmp_path))
        assert result.get("VIRTUAL_ENV") == "/cache/.venv"
        assert result.get("TEST_CACHE_TOKEN") == "abc123"
        assert result.get("PATH", "").startswith("/cache/bin:")

    @pytest.mark.asyncio
    async def test_default_bash_vars_excluded(self, tmp_path: Path) -> None:
        """Vars present in plain bash (PWD, SHLVL, etc.) shouldn't leak through."""
        from performer.workspace import _activate_env_cache
        activate = tmp_path / "activate.sh"
        activate.write_text("# no-op\n")
        result = await _activate_env_cache(str(tmp_path))
        # Empty script → empty delta (PWD/SHLVL match the reference shell)
        assert result == {}


class TestStartEnvCacheServices:
    """Tests for _start_env_cache_services (spec 063 Phase 1)."""

    @pytest.mark.asyncio
    async def test_empty_path_is_noop(self) -> None:
        from performer.workspace import _start_env_cache_services
        # Must not raise even with no env cache configured.
        await _start_env_cache_services("", {})

    @pytest.mark.asyncio
    async def test_missing_script_is_noop(self, tmp_path: Path) -> None:
        from performer.workspace import _start_env_cache_services
        # services/ dir absent — must silently skip.
        await _start_env_cache_services(str(tmp_path), {})

    @pytest.mark.asyncio
    async def test_non_executable_script_is_noop(self, tmp_path: Path) -> None:
        from performer.workspace import _start_env_cache_services
        services = tmp_path / "services"
        services.mkdir()
        start = services / "services-start.sh"
        start.write_text("#!/usr/bin/env bash\nexit 0\n")
        # No exec bit set → must not invoke.
        await _start_env_cache_services(str(tmp_path), {})

    @pytest.mark.asyncio
    async def test_executable_script_is_invoked(self, tmp_path: Path) -> None:
        from performer.workspace import _start_env_cache_services
        services = tmp_path / "services"
        services.mkdir()
        flag = tmp_path / "started"
        start = services / "services-start.sh"
        start.write_text(f"#!/usr/bin/env bash\ntouch {flag}\n")
        start.chmod(0o755)
        await _start_env_cache_services(str(tmp_path), {})
        assert flag.exists(), "services-start.sh must run when present and executable"

    @pytest.mark.asyncio
    async def test_nonzero_exit_logs_but_does_not_raise(
        self, tmp_path: Path
    ) -> None:
        from performer.workspace import _start_env_cache_services
        services = tmp_path / "services"
        services.mkdir()
        start = services / "services-start.sh"
        start.write_text("#!/usr/bin/env bash\necho boom >&2\nexit 17\n")
        start.chmod(0o755)
        # Must not raise; failure logged and swallowed so workspace setup proceeds.
        await _start_env_cache_services(str(tmp_path), {})

    @pytest.mark.asyncio
    async def test_cache_env_is_propagated(self, tmp_path: Path) -> None:
        from performer.workspace import _start_env_cache_services
        services = tmp_path / "services"
        services.mkdir()
        start = services / "services-start.sh"
        marker = tmp_path / "saw-token"
        start.write_text(
            f'#!/usr/bin/env bash\n[ "$CACHE_TOKEN" = "xyz" ] && touch {marker}\n'
        )
        start.chmod(0o755)
        await _start_env_cache_services(str(tmp_path), {"CACHE_TOKEN": "xyz"})
        assert marker.exists(), "cache_env vars must be exported to services-start.sh"


class TestStopEnvCacheServices:
    """Tests for stop_env_cache_services and stop_all_env_cache_services (spec 063 T007)."""

    def _clear_registry(self) -> None:
        from performer.workspace import _ACTIVE_SERVICE_CACHES
        _ACTIVE_SERVICE_CACHES.clear()

    def test_empty_path_is_noop(self) -> None:
        from performer.workspace import stop_env_cache_services
        stop_env_cache_services("")

    def test_missing_script_is_noop(self, tmp_path: Path) -> None:
        from performer.workspace import stop_env_cache_services
        stop_env_cache_services(str(tmp_path))

    def test_non_executable_script_is_noop(self, tmp_path: Path) -> None:
        from performer.workspace import stop_env_cache_services
        services = tmp_path / "services"
        services.mkdir()
        stop = services / "services-stop.sh"
        stop.write_text("#!/usr/bin/env bash\nexit 0\n")
        # No exec bit; helper must not invoke.
        stop_env_cache_services(str(tmp_path))

    def test_executable_script_is_invoked(self, tmp_path: Path) -> None:
        from performer.workspace import stop_env_cache_services
        services = tmp_path / "services"
        services.mkdir()
        flag = tmp_path / "stopped"
        stop = services / "services-stop.sh"
        stop.write_text(f"#!/usr/bin/env bash\ntouch {flag}\n")
        stop.chmod(0o755)
        stop_env_cache_services(str(tmp_path))
        assert flag.exists(), "services-stop.sh must run when present and executable"

    def test_nonzero_exit_does_not_raise(self, tmp_path: Path) -> None:
        from performer.workspace import stop_env_cache_services
        services = tmp_path / "services"
        services.mkdir()
        stop = services / "services-stop.sh"
        stop.write_text("#!/usr/bin/env bash\nexit 5\n")
        stop.chmod(0o755)
        # Must not raise on nonzero exit — shutdown path is best-effort.
        stop_env_cache_services(str(tmp_path))

    def test_cache_env_is_propagated(self, tmp_path: Path) -> None:
        from performer.workspace import stop_env_cache_services
        services = tmp_path / "services"
        services.mkdir()
        marker = tmp_path / "saw-stop-token"
        stop = services / "services-stop.sh"
        stop.write_text(
            f'#!/usr/bin/env bash\n[ "$CACHE_TOKEN" = "stop-xyz" ] && touch {marker}\n'
        )
        stop.chmod(0o755)
        stop_env_cache_services(str(tmp_path), {"CACHE_TOKEN": "stop-xyz"})
        assert marker.exists()

    @pytest.mark.asyncio
    async def test_successful_start_registers_for_shutdown_stop(
        self, tmp_path: Path
    ) -> None:
        from performer.workspace import (
            _ACTIVE_SERVICE_CACHES,
            _start_env_cache_services,
            stop_all_env_cache_services,
        )
        self._clear_registry()
        services = tmp_path / "services"
        services.mkdir()
        started = tmp_path / "started"
        stopped = tmp_path / "stopped"
        (services / "services-start.sh").write_text(
            f"#!/usr/bin/env bash\ntouch {started}\n"
        )
        (services / "services-stop.sh").write_text(
            f"#!/usr/bin/env bash\ntouch {stopped}\n"
        )
        (services / "services-start.sh").chmod(0o755)
        (services / "services-stop.sh").chmod(0o755)

        await _start_env_cache_services(str(tmp_path), {})
        assert started.exists()
        assert str(tmp_path) in _ACTIVE_SERVICE_CACHES

        stop_all_env_cache_services()
        assert stopped.exists(), "stop_all must invoke services-stop.sh for registered caches"
        # Registry must be cleared so a second call is a no-op.
        assert _ACTIVE_SERVICE_CACHES == {}

    def test_stop_all_empty_registry_is_noop(self) -> None:
        from performer.workspace import stop_all_env_cache_services
        self._clear_registry()
        stop_all_env_cache_services()  # must not raise

    def test_stop_all_swallows_per_cache_errors(self, tmp_path: Path) -> None:
        from performer.workspace import (
            _ACTIVE_SERVICE_CACHES,
            stop_all_env_cache_services,
        )
        self._clear_registry()

        good = tmp_path / "good"
        bad = tmp_path / "bad"
        for root in (good, bad):
            (root / "services").mkdir(parents=True)
        good_flag = tmp_path / "good-stopped"
        (good / "services" / "services-stop.sh").write_text(
            f"#!/usr/bin/env bash\ntouch {good_flag}\n"
        )
        (good / "services" / "services-stop.sh").chmod(0o755)
        (bad / "services" / "services-stop.sh").write_text(
            "#!/usr/bin/env bash\nexit 9\n"
        )
        (bad / "services" / "services-stop.sh").chmod(0o755)

        _ACTIVE_SERVICE_CACHES[str(bad)] = {}
        _ACTIVE_SERVICE_CACHES[str(good)] = {}

        # A failing stop in one cache must not prevent the other from stopping.
        stop_all_env_cache_services()
        assert good_flag.exists()
        assert _ACTIVE_SERVICE_CACHES == {}


class TestEnvCacheHealthCheck:
    """Spec 063 Phase 4 (T023): services-health.sh post-start invocation."""

    def _clear_flag(self) -> None:
        import performer.workspace as ws
        ws._ENV_CACHE_HEALTH_FAILED = False
        ws._ACTIVE_SERVICE_CACHES.clear()

    @pytest.mark.asyncio
    async def test_passing_health_check_does_not_flag(self, tmp_path: Path) -> None:
        self._clear_flag()
        from performer.workspace import _start_env_cache_services, consume_env_cache_health_failure
        services = tmp_path / "services"
        services.mkdir()
        (services / "services-start.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
        (services / "services-start.sh").chmod(0o755)
        (services / "services-health.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
        (services / "services-health.sh").chmod(0o755)
        await _start_env_cache_services(str(tmp_path), {})
        assert consume_env_cache_health_failure() is False

    @pytest.mark.asyncio
    async def test_failing_health_check_flags(self, tmp_path: Path) -> None:
        self._clear_flag()
        from performer.workspace import _start_env_cache_services, consume_env_cache_health_failure
        services = tmp_path / "services"
        services.mkdir()
        (services / "services-start.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
        (services / "services-start.sh").chmod(0o755)
        (services / "services-health.sh").write_text(
            "#!/usr/bin/env bash\necho dead >&2\nexit 1\n"
        )
        (services / "services-health.sh").chmod(0o755)
        await _start_env_cache_services(str(tmp_path), {})
        assert consume_env_cache_health_failure() is True
        # Flag is single-shot.
        assert consume_env_cache_health_failure() is False

    @pytest.mark.asyncio
    async def test_missing_health_script_is_noop(self, tmp_path: Path) -> None:
        self._clear_flag()
        from performer.workspace import _start_env_cache_services, consume_env_cache_health_failure
        services = tmp_path / "services"
        services.mkdir()
        (services / "services-start.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
        (services / "services-start.sh").chmod(0o755)
        # No services-health.sh present.
        await _start_env_cache_services(str(tmp_path), {})
        assert consume_env_cache_health_failure() is False

"""Unit tests for workspace.py — branch naming, WorkspaceManager prepare/teardown.

Integration tests (real git operations against a local bare repo) live in
tests/integration/test_workspace_integration.py.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from coordinare.workspace import (
    WorkspaceManager,
    WorkspaceSetupError,
    _GitCommandError,
    _redact_tokens,
    _run_git,
    make_branch_name,
)

# ---------------------------------------------------------------------------
# _redact_tokens (security: no token leakage in logs or error messages)
# ---------------------------------------------------------------------------


def test_redact_tokens_replaces_token_in_clone_url() -> None:
    """Token embedded in a GitHub clone URL is replaced with [REDACTED]."""
    raw = "repository 'https://x-access-token:ghp_abc123@github.com/org/repo.git' not found"
    redacted = _redact_tokens(raw)
    assert "ghp_abc123" not in redacted
    assert "x-access-token:[REDACTED]@" in redacted


def test_redact_tokens_passthrough_for_clean_text() -> None:
    """Text without an embedded token is returned unchanged."""
    clean = "Cloning into '/tmp/repo'..."
    assert _redact_tokens(clean) == clean


def test_redact_tokens_multiple_occurrences() -> None:
    """Every occurrence of an embedded token is redacted."""
    raw = "x-access-token:abc@host and x-access-token:xyz@host"
    redacted = _redact_tokens(raw)
    assert "abc" not in redacted
    assert "xyz" not in redacted
    assert redacted.count("[REDACTED]") == 2


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(
    *,
    github_org: str = "acme",
    project_name: str = "myrepo",
    github_token: str = "ghp_test",
    workspace_root: Path | None = None,
    agent_transport: str = "subprocess",
) -> MagicMock:
    """Return a minimal ProjectConfiguration mock."""
    cfg = MagicMock()
    cfg.github_org = github_org
    cfg.project_name = project_name
    cfg.github_token = MagicMock()
    cfg.github_token.get_secret_value.return_value = github_token
    cfg.workspace_root = workspace_root
    cfg.agent_transport = agent_transport
    return cfg


# ---------------------------------------------------------------------------
# Phase 3 (T013): make_branch_name — User Story 2
# ---------------------------------------------------------------------------


def test_branch_name_ascii_title() -> None:
    assert make_branch_name("ID", "Add retry logic") == "coordinare/ID/add-retry-logic"


def test_branch_name_unicode_accents() -> None:
    assert make_branch_name("ID", "Café résumé") == "coordinare/ID/cafe-resume"


def test_branch_name_special_chars_only() -> None:
    assert make_branch_name("ID", "---!!!") == "coordinare/ID/untitled"


def test_branch_name_empty_string() -> None:
    assert make_branch_name("ID", "") == "coordinare/ID/untitled"


def test_branch_name_long_title() -> None:
    title = "A" * 200
    result = make_branch_name("ID", title)
    slug = result.split("/")[-1]
    assert len(slug) <= 50


def test_branch_name_no_trailing_hyphen_after_truncation() -> None:
    # 49 'a' chars + 1 '-' + more = truncation lands on a hyphen
    title = "a" * 49 + " extra words here"
    result = make_branch_name("ID", title)
    slug = result.split("/")[-1]
    assert not slug.endswith("-")
    assert len(slug) <= 50


def test_branch_name_lock_suffix() -> None:
    # The dot in "some.lock" is collapsed to "-" by the slug regex, so the
    # result is "some-lock" (not ".lock"). The .lock strip guards against
    # edge cases where a slug somehow still ends in ".lock" after truncation.
    result = make_branch_name("ID", "some.lock")
    assert not result.endswith(".lock")
    assert result == "coordinare/ID/some-lock"


def test_branch_name_deterministic() -> None:
    results = {make_branch_name("PVTI_abc", "Add retry logic") for _ in range(5)}
    assert len(results) == 1


def test_branch_name_spaces_and_caps() -> None:
    assert make_branch_name("ID", "Add User Auth") == "coordinare/ID/add-user-auth"


# ---------------------------------------------------------------------------
# Phase 2 (T011): WorkspaceManager.prepare() unit tests — User Story 1
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prepare_calls_git_in_order(tmp_path: Path) -> None:
    """prepare() calls git ops in order: clone -> config x2 -> remote set-url -> checkout -b."""
    calls: list[tuple[str, ...]] = []

    envs: list[dict] = []

    async def _fake_run_git(*args: str, **kwargs: object) -> None:
        calls.append(args)
        if kwargs.get("env"):
            envs.append(dict(kwargs["env"]))

    cfg = _make_config(workspace_root=tmp_path)
    mgr = WorkspaceManager(cfg)

    with patch("coordinare.workspace._run_git", side_effect=_fake_run_git):
        info = await mgr.prepare({"id": "CARD_1", "title": "Add retry logic"})

    # Verify order of git commands
    assert calls[0][0] == "clone"
    assert calls[1] == ("config", "--local", "user.name", "Coordinare Bot")
    assert calls[2] == ("config", "--local", "user.email", "coordinare@localhost")
    assert calls[3][0] == "remote"  # remote set-url
    assert calls[4] == ("checkout", "-b", "coordinare/CARD_1/add-retry-logic")

    # Clone URL must be plain (no embedded token) — auth is via env vars
    clone_url_arg = calls[0][2]
    assert "x-access-token" not in clone_url_arg
    assert "ghp_test" not in clone_url_arg
    assert clone_url_arg == "https://github.com/acme/myrepo.git"

    # Verify env-based auth was passed to git clone
    assert len(envs) > 0, "env should be passed to _run_git"
    clone_env = envs[0]
    assert clone_env.get("GIT_CONFIG_COUNT") == "1"
    assert clone_env.get("GIT_CONFIG_KEY_0") == "http.extraHeader"
    assert "Authorization: Basic" in clone_env.get("GIT_CONFIG_VALUE_0", "")

    # Returned repo_url must also be plain
    assert "ghp_test" not in info.repo_url


@pytest.mark.asyncio
async def test_prepare_returns_correct_workspace_info(tmp_path: Path) -> None:
    """prepare() returns WorkspaceInfo with expected branch and plain repo_url."""

    async def _fake_run_git(*args: str, **kwargs: object) -> None:
        # Create the clone_dir so the path exists.
        # args layout: ("clone", "--depth=1", <clone_url>, <clone_dir_str>, ...)
        if args[0] == "clone":
            clone_dir = Path(args[3])
            clone_dir.mkdir(parents=True, exist_ok=True)

    cfg = _make_config(workspace_root=tmp_path)
    mgr = WorkspaceManager(cfg)

    with patch("coordinare.workspace._run_git", side_effect=_fake_run_git):
        info = await mgr.prepare({"id": "PVTI_abc", "title": "Add retry logic"})

    assert info.branch == "coordinare/PVTI_abc/add-retry-logic"
    assert info.repo_url == "https://github.com/acme/myrepo.git"
    assert "ghp_test" not in info.repo_url


@pytest.mark.asyncio
async def test_prepare_cleans_up_on_clone_failure(tmp_path: Path) -> None:
    """On clone failure, the mkdtemp container is removed (no orphaned dirs)."""
    call_count = 0

    async def _fail_on_clone(*args: str, **kwargs: object) -> None:
        nonlocal call_count
        call_count += 1
        if args[0] == "clone":
            raise _GitCommandError("simulated clone failure")

    cfg = _make_config(workspace_root=tmp_path)
    mgr = WorkspaceManager(cfg)

    with patch("coordinare.workspace._run_git", side_effect=_fail_on_clone), pytest.raises(WorkspaceSetupError):
        await mgr.prepare({"id": "CARD_X", "title": "Test"})

    # No coordinare-ws-* dirs should remain under workspace_root
    remaining = list(tmp_path.glob("coordinare-ws-*"))
    assert remaining == [], f"Orphaned dirs found: {remaining}"


@pytest.mark.asyncio
async def test_prepare_raises_workspace_setup_error_on_failure(tmp_path: Path) -> None:
    """Any git failure in prepare() raises WorkspaceSetupError with non-empty message."""

    async def _always_fail(*args: str, **kwargs: object) -> None:
        raise _GitCommandError("boom")

    cfg = _make_config(workspace_root=tmp_path)
    mgr = WorkspaceManager(cfg)

    with patch("coordinare.workspace._run_git", side_effect=_always_fail), pytest.raises(WorkspaceSetupError) as exc_info:
        await mgr.prepare({"id": "CARD_X", "title": "Test"})

    assert str(exc_info.value)


@pytest.mark.asyncio
async def test_prepare_token_not_in_repo_url(tmp_path: Path) -> None:
    """WorkspaceInfo.repo_url must never contain the token."""

    async def _fake_run_git(*args: str, **kwargs: object) -> None:
        pass

    cfg = _make_config(workspace_root=tmp_path, github_token="super-secret-token")
    mgr = WorkspaceManager(cfg)

    with patch("coordinare.workspace._run_git", side_effect=_fake_run_git):
        info = await mgr.prepare({"id": "CARD_1", "title": "Feature"})

    assert "super-secret-token" not in info.repo_url


# ---------------------------------------------------------------------------
# Phase 5 (T020): Kubernetes transport path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prepare_kubernetes_returns_no_path() -> None:
    """For kubernetes transport, prepare() skips git and returns path=None."""
    cfg = _make_config(agent_transport="kubernetes")
    mgr = WorkspaceManager(cfg)

    with patch("coordinare.workspace._run_git") as mock_git:
        info = await mgr.prepare({"id": "CARD_K8S", "title": "K8s task"})

    mock_git.assert_not_called()
    assert info.path is None
    assert info.branch == "coordinare/CARD_K8S/k8s-task"
    assert "ghp_test" not in info.repo_url


# ---------------------------------------------------------------------------
# Phase 4 (T017): WorkspaceManager.teardown() unit tests — User Story 3
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_teardown_removes_directory(tmp_path: Path) -> None:
    """teardown() removes the given directory."""
    ws_dir = tmp_path / "coordinare-ws-test"
    ws_dir.mkdir()
    assert ws_dir.exists()

    cfg = _make_config()
    mgr = WorkspaceManager(cfg)
    await mgr.teardown(ws_dir)

    assert not ws_dir.exists()


@pytest.mark.asyncio
async def test_teardown_warns_not_raises_on_missing_directory(
    tmp_path: Path,
) -> None:
    """teardown() on a nonexistent path does not raise.

    Note: structlog does not route to stdlib logging in tests, so we verify
    the behavioral guarantee (no exception) rather than the log record.
    The warning IS emitted to stdout — visible in test output with -s.
    """
    nonexistent = tmp_path / "does-not-exist"
    assert not nonexistent.exists()

    cfg = _make_config()
    mgr = WorkspaceManager(cfg)

    await mgr.teardown(nonexistent)  # must not raise


@pytest.mark.asyncio
async def test_teardown_warns_not_raises_on_permission_error(tmp_path: Path) -> None:
    """teardown() does not raise when shutil.rmtree raises PermissionError."""
    ws_dir = tmp_path / "coordinare-ws-perm"
    ws_dir.mkdir()

    cfg = _make_config()
    mgr = WorkspaceManager(cfg)

    with patch("shutil.rmtree", side_effect=PermissionError("permission denied")):
        await mgr.teardown(ws_dir)  # must not raise


# ---------------------------------------------------------------------------
# _run_git error branches (coverage for OSError and TimeoutError paths)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_git_raises_on_oserror() -> None:
    """_run_git raises _GitCommandError when git process fails to start (OSError)."""
    with patch("asyncio.create_subprocess_exec", side_effect=OSError("git not found")), pytest.raises(_GitCommandError, match="git not available"):
        await _run_git("status")


@pytest.mark.asyncio
async def test_run_git_raises_on_timeout() -> None:
    """_run_git raises _GitCommandError and kills the process on TimeoutError."""
    mock_proc = MagicMock()
    mock_proc.kill = MagicMock()
    mock_proc.wait = AsyncMock(return_value=None)

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc), patch("asyncio.wait_for", side_effect=TimeoutError), pytest.raises(_GitCommandError, match="timed out"):
        await _run_git("status", timeout=0.001)


@pytest.mark.asyncio
async def test_run_git_succeeds_with_empty_stderr() -> None:
    """_run_git returncode=0 with no stderr output succeeds silently."""
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.communicate = AsyncMock(return_value=(b"", b""))

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        await _run_git("status")  # should not raise


@pytest.mark.asyncio
async def test_run_git_succeeds_with_stderr_output() -> None:
    """_run_git returncode=0 with non-empty stderr logs debug and does not raise."""
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.communicate = AsyncMock(return_value=(b"", b"warning: detached HEAD"))

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        await _run_git("status")  # should not raise


@pytest.mark.asyncio
async def test_run_git_raises_on_nonzero_returncode() -> None:
    """_run_git raises _GitCommandError when the git process exits non-zero."""
    mock_proc = MagicMock()
    mock_proc.returncode = 128
    mock_proc.communicate = AsyncMock(return_value=(b"", b"fatal: not a git repo"))

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc), pytest.raises(_GitCommandError, match="128"):
        await _run_git("status")


@pytest.mark.asyncio
async def test_prepare_raises_when_mkdtemp_fails() -> None:
    """OSError from tempfile.mkdtemp (container still None) raises WorkspaceSetupError."""
    cfg = _make_config()
    mgr = WorkspaceManager(cfg)

    with patch("coordinare.workspace.tempfile.mkdtemp", side_effect=OSError("disk full")), pytest.raises(WorkspaceSetupError):
        await mgr.prepare({"id": "CARD_X", "title": "Test"})


# ---------------------------------------------------------------------------
# 042 — public get_fresh_github_token accessor
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_fresh_github_token_prefers_auth() -> None:
    """The public token accessor prefers the GitHubAuth protocol when
    configured (which supports App token refresh), falling back to the
    static PAT only when auth is absent.  This lets monitor_performer
    push refreshed tokens to performers without reaching into private
    attributes."""
    class _FakeAuth:
        def __init__(self) -> None:
            self.calls = 0
        async def get_token(self) -> str:
            self.calls += 1
            return f"fresh-token-{self.calls}"

    auth = _FakeAuth()
    cfg = _make_config(github_token="static-pat")
    mgr = WorkspaceManager(cfg, auth=auth)

    t1 = await mgr.get_fresh_github_token()
    t2 = await mgr.get_fresh_github_token()

    # Each call goes through the auth protocol (fresh every time)
    assert t1 == "fresh-token-1"
    assert t2 == "fresh-token-2"
    assert auth.calls == 2


@pytest.mark.asyncio
async def test_get_fresh_github_token_falls_back_to_static_pat() -> None:
    """When no auth protocol is configured, use the static token."""
    cfg = _make_config(github_token="static-pat")
    mgr = WorkspaceManager(cfg)  # no auth=

    token = await mgr.get_fresh_github_token()
    assert token == "static-pat"


@pytest.mark.asyncio
async def test_get_fresh_github_token_returns_none_when_no_credential() -> None:
    """With neither auth nor static token, return None (not raise) — the
    caller decides whether a missing credential is fatal."""
    cfg = _make_config(github_token=None)
    mgr = WorkspaceManager(cfg)

    token = await mgr.get_fresh_github_token()
    assert token is None

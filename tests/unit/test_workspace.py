"""Unit tests for workspace.py — branch naming, WorkspaceManager prepare/teardown.

Integration tests (real git operations against a local bare repo) live in
tests/integration/test_workspace_integration.py.
"""
from __future__ import annotations

from pathlib import Path
from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from coordinare.workspace import (
    WorkspaceManager,
    WorkspaceSetupError,
    _build_minimal_env,
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
    stale_branch_cleanup: bool = True,
    branch_collision_strategy: str = "delete",
) -> MagicMock:
    """Return a minimal ProjectConfiguration mock."""
    from coordinare.config import BranchCollisionStrategy

    cfg = MagicMock()
    cfg.github_org = github_org
    cfg.project_name = project_name
    cfg.git_base_url = "https://github.com"  # 151: production default git host
    cfg.performer_git_base_url = None  # 151: falls back to git_base_url in prod
    cfg.github_token = MagicMock()
    cfg.github_token.get_secret_value.return_value = github_token
    cfg.workspace_root = workspace_root
    cfg.agent_transport = agent_transport
    cfg.stale_branch_cleanup = stale_branch_cleanup
    cfg.branch_collision_strategy = BranchCollisionStrategy(branch_collision_strategy)
    cfg.bot_identity = None  # use defaults ("Coordinare Bot" / "coordinare@localhost")
    cfg.env_passthrough = []
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
async def test_prepare_raises_when_no_token_available(tmp_path: Path) -> None:
    """No auth protocol and no github_token → WorkspaceSetupError before any git op."""
    cfg = _make_config(workspace_root=tmp_path)
    cfg.github_token = None
    mgr = WorkspaceManager(cfg)
    mgr._auth = None
    mgr._github_token = None

    with pytest.raises(WorkspaceSetupError, match="No GitHub token available"):
        await mgr.prepare({"id": "CARD_X", "title": "no token"})


@pytest.mark.asyncio
async def test_prepare_uses_auth_protocol_when_configured(tmp_path: Path) -> None:
    """When _auth is set, prepare() pulls the token via _auth.get_token() (line 277)."""

    async def _fake_run_git(*args: str, **kwargs: object) -> None:
        if args[0] == "clone":
            Path(args[3]).mkdir(parents=True, exist_ok=True)

    cfg = _make_config(workspace_root=tmp_path)
    mgr = WorkspaceManager(cfg)
    auth = MagicMock()
    auth.get_token = AsyncMock(return_value="app-token-xyz")
    mgr._auth = auth

    with patch("coordinare.workspace._run_git", side_effect=_fake_run_git):
        info = await mgr.prepare({"id": "CARD_Y", "title": "auth path"})

    auth.get_token.assert_awaited()
    assert "app-token-xyz" not in info.repo_url


@pytest.mark.asyncio
async def test_get_token_async_returns_none_when_no_source(tmp_path: Path) -> None:
    """get_token_async() returns None when neither _auth nor _github_token is set (line 240)."""
    cfg = _make_config(workspace_root=tmp_path)
    cfg.github_token = None
    mgr = WorkspaceManager(cfg)
    mgr._auth = None
    mgr._github_token = None

    assert await mgr.get_fresh_github_token() is None


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
async def test_prepare_splits_host_and_performer_git_base(tmp_path: Path) -> None:
    """151: the host clones from git_base_url (127.0.0.1) while WorkspaceInfo.repo_url
    handed to the performer uses performer_git_base_url (host.docker.internal)."""
    clone_urls: list[str] = []

    async def _fake_run_git(*args: str, **kwargs: object) -> None:
        if args[0] == "clone":
            clone_urls.append(args[2])  # ("clone", "--depth=1", <clone_url>, <dir>, ...)
            Path(args[3]).mkdir(parents=True, exist_ok=True)

    cfg = _make_config(workspace_root=tmp_path)
    cfg.git_base_url = "git://127.0.0.1:9418"
    cfg.performer_git_base_url = "git://host.docker.internal:9418"
    mgr = WorkspaceManager(cfg)

    with patch("coordinare.workspace._run_git", side_effect=_fake_run_git):
        info = await mgr.prepare({"id": "PVTI_abc", "title": "Add retry logic"})

    assert clone_urls == ["git://127.0.0.1:9418/acme/myrepo.git"]  # host clone
    assert info.repo_url == "git://host.docker.internal:9418/acme/myrepo.git"  # performer


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


@pytest.mark.asyncio
async def test_prepare_kubernetes_injects_auth_protocol_token() -> None:
    """Kubernetes transport: the performer container handles its own git setup but
    still makes GitHub API calls, so prepare() must resolve the token via the auth
    protocol (App mode) instead of dispatching an empty credential."""
    cfg = _make_config(agent_transport="kubernetes", github_token="static-pat")
    mgr = WorkspaceManager(cfg)
    auth = MagicMock()
    auth.get_token = AsyncMock(return_value="app-token-k8s")
    mgr._auth = auth

    with patch("coordinare.workspace._run_git") as mock_git:
        info = await mgr.prepare({"id": "CARD_K8S", "title": "K8s task"})

    auth.get_token.assert_awaited_once()
    mock_git.assert_not_called()
    assert info.path is None
    assert info.github_token == "app-token-k8s"


@pytest.mark.asyncio
async def test_prepare_kubernetes_falls_back_to_static_pat() -> None:
    """Kubernetes transport with no auth protocol: the static PAT is injected."""
    cfg = _make_config(agent_transport="kubernetes", github_token="static-pat-fallback")
    mgr = WorkspaceManager(cfg)  # no auth protocol

    with patch("coordinare.workspace._run_git") as mock_git:
        info = await mgr.prepare({"id": "CARD_K8S", "title": "K8s task"})

    mock_git.assert_not_called()
    assert info.path is None
    assert info.github_token == "static-pat-fallback"


@pytest.mark.asyncio
async def test_prepare_kubernetes_raises_when_no_credential() -> None:
    """No auth protocol and no static token → fail fast at dispatch instead of
    the performer pod dying later with `permanent performer config error: GITHUB_TOKEN`."""
    cfg = _make_config(agent_transport="kubernetes")
    cfg.github_token = None
    mgr = WorkspaceManager(cfg)

    with pytest.raises(WorkspaceSetupError, match="No GitHub token available"):
        await mgr.prepare({"id": "CARD_K8S", "title": "K8s task"})


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


# ---------------------------------------------------------------------------
# 051 — _build_minimal_env tests
# ---------------------------------------------------------------------------


class _NoConfig:
    pass


class _MinEnvIdentity:
    name = "my-bot"
    email = "my-bot@example.com"


class _FullConfig:
    bot_identity = _MinEnvIdentity()
    env_passthrough: ClassVar[list[str]] = ["CUSTOM_VAR"]


def test_build_minimal_env_defaults_when_no_config() -> None:
    """No config → uses defaults 'Coordinare Bot' / 'coordinare@localhost'."""
    env = _build_minimal_env(None)
    assert env["GIT_AUTHOR_NAME"] == "Coordinare Bot"
    assert env["GIT_AUTHOR_EMAIL"] == "coordinare@localhost"
    assert env["GIT_COMMITTER_NAME"] == "Coordinare Bot"
    assert env["GIT_COMMITTER_EMAIL"] == "coordinare@localhost"
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_build_minimal_env_uses_config_identity() -> None:
    """Config with custom identity → custom name/email in env."""
    env = _build_minimal_env(_FullConfig())
    assert env["GIT_AUTHOR_NAME"] == "my-bot"
    assert env["GIT_AUTHOR_EMAIL"] == "my-bot@example.com"
    assert env["GIT_COMMITTER_NAME"] == "my-bot"
    assert env["GIT_COMMITTER_EMAIL"] == "my-bot@example.com"


def test_build_minimal_env_passthrough_copies_present_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """env_passthrough copies the named var when it is present on the host."""
    monkeypatch.setenv("CUSTOM_VAR", "hello")
    env = _build_minimal_env(_FullConfig())
    assert env["CUSTOM_VAR"] == "hello"


def test_build_minimal_env_passthrough_skips_absent_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """env_passthrough silently skips a named var when it is absent on the host."""
    monkeypatch.delenv("CUSTOM_VAR", raising=False)
    env = _build_minimal_env(_FullConfig())
    assert "CUSTOM_VAR" not in env


def test_build_minimal_env_excludes_arbitrary_host_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """Arbitrary host vars (not in allowlist/passthrough) are never included."""
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "supersecret")
    env = _build_minimal_env(_NoConfig())
    assert "AWS_SECRET_ACCESS_KEY" not in env


# ---------------------------------------------------------------------------
# T015 + T017 — Stale branch detection (US2) and suffix strategy (US3)
# ---------------------------------------------------------------------------


def _make_github_service(*, exists_responses: list[bool]) -> MagicMock:
    """Minimal GitHubService mock for stale branch tests."""
    svc = MagicMock()
    # branch_exists returns successive values from the list
    svc.branch_exists = AsyncMock(side_effect=exists_responses)
    svc.branch_has_open_pr = AsyncMock(return_value=False)
    svc.delete_branch = AsyncMock()
    return svc


@pytest.mark.asyncio
async def test_stale_branch_delete_strategy_calls_delete_branch() -> None:
    """T015a: stale branch exists + strategy delete → delete_branch called before clone."""
    card = {"id": "89", "title": "add auth"}
    cfg = _make_config(stale_branch_cleanup=True, branch_collision_strategy="delete")
    github_svc = _make_github_service(exists_responses=[True])
    mgr = WorkspaceManager(cfg, github_service=github_svc)

    branch = await mgr._resolve_branch("coordinare/89/add-auth", card)
    github_svc.delete_branch.assert_awaited_once_with("coordinare/89/add-auth")
    assert branch == "coordinare/89/add-auth"


@pytest.mark.asyncio
async def test_no_stale_branch_skips_delete() -> None:
    """T015b: no stale branch → workspace created normally (delete_branch not called)."""
    card = {"id": "89", "title": "add auth"}
    cfg = _make_config(stale_branch_cleanup=True, branch_collision_strategy="delete")
    github_svc = _make_github_service(exists_responses=[False])
    mgr = WorkspaceManager(cfg, github_service=github_svc)

    branch = await mgr._resolve_branch("coordinare/89/add-auth", card)
    github_svc.delete_branch.assert_not_awaited()
    assert branch == "coordinare/89/add-auth"


@pytest.mark.asyncio
async def test_stale_branch_cleanup_disabled_skips_check() -> None:
    """T015c: stale_branch_cleanup=false → no branch check performed."""
    card = {"id": "89", "title": "add auth"}
    cfg = _make_config(stale_branch_cleanup=False)
    github_svc = _make_github_service(exists_responses=[])
    mgr = WorkspaceManager(cfg, github_service=github_svc)

    branch = await mgr._resolve_branch("coordinare/89/add-auth", card)
    github_svc.branch_exists.assert_not_awaited()
    assert branch == "coordinare/89/add-auth"


@pytest.mark.asyncio
async def test_stale_branch_deletion_failure_continues() -> None:
    """T015d: deletion failure (delete_branch swallows errors) → resolve_branch still returns branch.

    delete_branch is contractually non-raising (logs warning internally).
    _resolve_branch trusts that contract and proceeds after the call.
    """
    card = {"id": "89", "title": "add auth"}
    cfg = _make_config(stale_branch_cleanup=True, branch_collision_strategy="delete")
    github_svc = _make_github_service(exists_responses=[True])
    # delete_branch returns None (success or swallowed failure) — test normal no-error path
    github_svc.delete_branch = AsyncMock(return_value=None)
    mgr = WorkspaceManager(cfg, github_service=github_svc)

    branch = await mgr._resolve_branch("coordinare/89/add-auth", card)
    github_svc.delete_branch.assert_awaited_once_with("coordinare/89/add-auth")
    assert branch == "coordinare/89/add-auth"


@pytest.mark.asyncio
async def test_in_flight_branch_preserved_no_delete() -> None:
    """In-flight cards keep their branch so prior stage commits are retained."""
    card = {"id": "89", "title": "add auth", "status": "IN_PROGRESS"}
    cfg = _make_config(stale_branch_cleanup=True, branch_collision_strategy="delete")
    github_svc = _make_github_service(exists_responses=[True])
    mgr = WorkspaceManager(cfg, github_service=github_svc)

    branch = await mgr._resolve_branch("coordinare/89/add-auth", card)
    github_svc.delete_branch.assert_not_awaited()
    github_svc.branch_has_open_pr.assert_not_awaited()
    assert branch == "coordinare/89/add-auth"


@pytest.mark.asyncio
async def test_open_pr_branch_preserved_even_from_todo() -> None:
    """TODO cards with an already-open PR must not delete the head branch."""
    card = {"id": "89", "title": "add auth", "status": "TODO"}
    cfg = _make_config(stale_branch_cleanup=True, branch_collision_strategy="delete")
    github_svc = _make_github_service(exists_responses=[True])
    github_svc.branch_has_open_pr = AsyncMock(return_value=True)
    mgr = WorkspaceManager(cfg, github_service=github_svc)

    branch = await mgr._resolve_branch("coordinare/89/add-auth", card)
    github_svc.delete_branch.assert_not_awaited()
    github_svc.branch_has_open_pr.assert_awaited_once_with("coordinare/89/add-auth")
    assert branch == "coordinare/89/add-auth"


@pytest.mark.asyncio
async def test_unknown_open_pr_state_preserves_branch() -> None:
    """Unknown open-PR state should preserve the branch fail-safe."""
    card = {"id": "89", "title": "add auth", "status": "TODO"}
    cfg = _make_config(stale_branch_cleanup=True, branch_collision_strategy="delete")
    github_svc = _make_github_service(exists_responses=[True])
    github_svc.branch_has_open_pr = AsyncMock(return_value=None)
    mgr = WorkspaceManager(cfg, github_service=github_svc)

    branch = await mgr._resolve_branch("coordinare/89/add-auth", card)
    github_svc.delete_branch.assert_not_awaited()
    github_svc.branch_has_open_pr.assert_awaited_once_with("coordinare/89/add-auth")
    assert branch == "coordinare/89/add-auth"


@pytest.mark.asyncio
async def test_no_github_service_skips_stale_check() -> None:
    """Stale branch check is silently skipped when no github_service is configured."""
    card = {"id": "89", "title": "add auth"}
    cfg = _make_config(stale_branch_cleanup=True)
    mgr = WorkspaceManager(cfg, github_service=None)

    branch = await mgr._resolve_branch("coordinare/89/add-auth", card)
    assert branch == "coordinare/89/add-auth"


# T017 — Suffix strategy

@pytest.mark.asyncio
async def test_suffix_applied_when_minus2_is_free() -> None:
    """T017a: suffix applied when -2 is free."""
    card = {"id": "89", "title": "add auth"}
    cfg = _make_config(stale_branch_cleanup=True, branch_collision_strategy="suffix")
    # original branch exists, -2 does not
    github_svc = _make_github_service(exists_responses=[True, False])
    mgr = WorkspaceManager(cfg, github_service=github_svc)

    branch = await mgr._resolve_branch("coordinare/89/add-auth", card)
    assert branch == "coordinare/89/add-auth-2"
    github_svc.delete_branch.assert_not_awaited()


@pytest.mark.asyncio
async def test_suffix_skips_taken_candidates() -> None:
    """T017b: suffix skips taken candidates and uses first free slot."""
    card = {"id": "89", "title": "add auth"}
    cfg = _make_config(stale_branch_cleanup=True, branch_collision_strategy="suffix")
    # original + -2 + -3 taken; -4 free
    github_svc = _make_github_service(exists_responses=[True, True, True, False])
    mgr = WorkspaceManager(cfg, github_service=github_svc)

    branch = await mgr._resolve_branch("coordinare/89/add-auth", card)
    assert branch == "coordinare/89/add-auth-4"


@pytest.mark.asyncio
async def test_suffix_exhausted_falls_back_to_delete() -> None:
    """T017c: all suffixes taken → falls back to delete + warning."""
    card = {"id": "89", "title": "add auth"}
    cfg = _make_config(stale_branch_cleanup=True, branch_collision_strategy="suffix")
    # original exists; -2 through -9 all exist
    github_svc = _make_github_service(exists_responses=[True, True, True, True, True, True, True, True, True])
    mgr = WorkspaceManager(cfg, github_service=github_svc)

    branch = await mgr._resolve_branch("coordinare/89/add-auth", card)
    github_svc.delete_branch.assert_awaited_once_with("coordinare/89/add-auth")
    assert branch == "coordinare/89/add-auth"


@pytest.mark.asyncio
@pytest.mark.parametrize("branch", ["conductor/CARD/original-title", "feature/custom-head"])
@pytest.mark.parametrize("transport", ["kubernetes", "subprocess"])
async def test_prepare_resumes_actual_pr_head(branch: str, transport: str, tmp_path: Path) -> None:
    github = MagicMock()
    github.check_mergeability = AsyncMock(return_value={"head_ref_name": branch, "head_repo_name_with_owner": "acme/myrepo"})
    github.branch_exists = AsyncMock(return_value=True)
    github.delete_branch = AsyncMock()
    mgr = WorkspaceManager(_make_config(agent_transport=transport), github_service=github)
    card = {"id": "CARD", "title": "Renamed title", "status": "TODO", "pr_node_id": "PR_existing"}
    with patch("coordinare.workspace._run_git", new_callable=AsyncMock) as git:
        info = await mgr.prepare(card)
    assert info.branch == branch
    github.check_mergeability.assert_awaited_once_with("PR_existing")
    github.branch_exists.assert_not_awaited()
    github.delete_branch.assert_not_awaited()
    if transport == "subprocess":
        assert "--depth=1" not in git.await_args_list[0].args
        assert any(c.args == ("fetch", "origin", f"refs/heads/{branch}") for c in git.await_args_list)
        assert git.await_args_list[-1].args == ("checkout", "-b", branch, "FETCH_HEAD")
    else:
        git.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [{}, {"head_ref_name": ""}, {"head_ref_name": None}])
async def test_prepare_unknown_pr_head_stops_before_git(result: dict[str, object]) -> None:
    github = MagicMock()
    github.check_mergeability = AsyncMock(return_value=result)
    mgr = WorkspaceManager(_make_config(agent_transport="kubernetes"), github_service=github)
    with (
        patch("coordinare.workspace._run_git", new_callable=AsyncMock) as git,
        pytest.raises(WorkspaceSetupError, match="PR head"),
    ):
        await mgr.prepare({"id": "CARD", "pr_node_id": "PR_existing"})
    git.assert_not_awaited()


@pytest.mark.asyncio
async def test_prepare_unreadable_pr_head_stops_without_token_details() -> None:
    github = MagicMock()
    github.check_mergeability = AsyncMock(side_effect=RuntimeError("sensitive-token"))
    mgr = WorkspaceManager(_make_config(agent_transport="kubernetes"), github_service=github)
    with pytest.raises(WorkspaceSetupError, match="PR head") as exc:
        await mgr.prepare({"id": "CARD", "pr_node_id": "PR_existing"})
    assert "sensitive-token" not in str(exc.value)


@pytest.mark.asyncio
async def test_prepare_retained_pr_without_resolver_stops() -> None:
    mgr = WorkspaceManager(_make_config(agent_transport="kubernetes"))
    with pytest.raises(WorkspaceSetupError, match="PR head"):
        await mgr.prepare({"id": "CARD", "pr_node_id": "PR_existing"})


@pytest.mark.asyncio
async def test_prepare_retained_pr_url_without_identity_stops() -> None:
    mgr = WorkspaceManager(_make_config(agent_transport="kubernetes"))
    with pytest.raises(WorkspaceSetupError, match="PR head"):
        await mgr.prepare({"id": "CARD", "pr_url": "https://github.com/acme/myrepo/pull/1"})


@pytest.mark.asyncio
@pytest.mark.parametrize("repository", ["contributor/myrepo", "", None])
async def test_prepare_cross_repository_or_unknown_head_stops(repository):
    github = MagicMock()
    github.check_mergeability = AsyncMock(return_value={"head_ref_name": "feature", "head_repo_name_with_owner": repository})
    mgr = WorkspaceManager(_make_config(agent_transport="kubernetes"), github_service=github)
    with pytest.raises(WorkspaceSetupError, match="PR head"):
        await mgr.prepare({"id": "CARD", "pr_node_id": "PR_existing"})

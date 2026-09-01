"""Agent workspace management for coordinare.

Provides:
- make_branch_name: pure function deriving a git-safe branch name from card ID + title
- WorkspaceSetupError: typed exception for BLOCKED routing on prepare failure
- WorkspaceInfo: transient value object returned by WorkspaceManager.prepare()
- WorkspaceManagerProtocol: Protocol for test mocking without importing WorkspaceManager
- WorkspaceManager: stateful service — clones repo, creates branch, configures creds
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import structlog

from coordinare.lib.subprocess_env import BASE_ENV_ALLOWLIST, inherited_host_env

if TYPE_CHECKING:
    from coordinare.config import ProjectConfiguration

logger = structlog.get_logger(__name__)

# Regex to redact embedded GitHub tokens from git output (e.g. in clone-failure messages).
_TOKEN_RE = re.compile(r"x-access-token:[^@]*@")


def _redact_tokens(text: str) -> str:
    """Replace ``x-access-token:<value>@`` with ``x-access-token:[REDACTED]@``."""
    return _TOKEN_RE.sub("x-access-token:[REDACTED]@", text)


# ---------------------------------------------------------------------------
# 051 — Minimal subprocess environment
# ---------------------------------------------------------------------------

#: Kept as a name because tests and callers refer to it; the list itself, and
#: the platform-dependent part of it, live in one place for both env builders.
#: A Windows fix applied here alone left the performer's own env still
#: stripping SystemRoot -- see coordinare.lib.subprocess_env.
_ENV_ALLOWLIST = BASE_ENV_ALLOWLIST


def _build_minimal_env(config: Any) -> dict[str, str]:
    """Return a minimal env dict for git subprocesses.

    Only copies a fixed allowlist of vars from the host env.  Sets
    GIT_TERMINAL_PROMPT=0 and injects GIT_AUTHOR/COMMITTER identity
    from ``config.bot_identity`` (falls back to defaults when config is None).
    Any var names listed in ``config.env_passthrough`` are also copied if
    present on the host.

    Note: GITHUB_TOKEN is intentionally absent here — workspace git ops
    authenticate via GIT_CONFIG_KEY_* (http.extraHeader), not an env var.
    The performer subprocess uses _build_subprocess_env() which does inject
    GITHUB_TOKEN for the performer's own git/API calls.
    """
    env: dict[str, str] = inherited_host_env()
    env["GIT_TERMINAL_PROMPT"] = "0"

    identity = getattr(config, "bot_identity", None) if config is not None else None
    name = (identity.name if identity is not None else None) or "Coordinare Bot"
    email = (identity.email if identity is not None else None) or "coordinare@localhost"
    env["GIT_AUTHOR_NAME"] = name
    env["GIT_AUTHOR_EMAIL"] = email
    env["GIT_COMMITTER_NAME"] = name
    env["GIT_COMMITTER_EMAIL"] = email

    for var in list(getattr(config, "env_passthrough", None) or []):
        if isinstance(var, str) and var in os.environ:
            env[var] = os.environ[var]

    return env


# ---------------------------------------------------------------------------
# Branch naming
# ---------------------------------------------------------------------------


def make_branch_name(card_id: str, card_title: str) -> str:
    """Return a deterministic git-safe branch name for the given card.

    Pattern: ``coordinare/{card_id}/{title_slug}``

    The slug is derived from the card title via:
    1. NFKD unicode normalization + ASCII encode (é→e, ü→u, CJK→dropped)
    2. Lowercase
    3. Collapse ``[^a-z0-9_]+`` to hyphens
    4. Strip leading/trailing hyphens
    5. Truncate to 50 characters (strip trailing hyphen after truncation)
    6. Strip ``.lock`` suffix (git disallows branches ending in ``.lock``)
    7. Fallback to ``"untitled"`` if the slug is empty after all transformations
    """
    normalized = unicodedata.normalize("NFKD", card_title)
    ascii_bytes = normalized.encode("ascii", "ignore")
    slug = ascii_bytes.decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9_]+", "-", slug)
    slug = slug.strip("-")
    if len(slug) > 50:
        slug = slug[:50].rstrip("-")
    if slug.endswith(".lock"):  # pragma: no cover — dots become hyphens via regex; defensive guard
        slug = slug[: -len(".lock")].rstrip("-")
    if not slug:
        slug = "untitled"
    return f"coordinare/{card_id}/{slug}"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class WorkspaceSetupError(RuntimeError):
    """Raised when workspace preparation fails.

    The message is human-readable and suitable for inclusion in a card's
    open_questions field so the operator can understand why the card was BLOCKED.
    """


# ---------------------------------------------------------------------------
# Value object
# ---------------------------------------------------------------------------


@dataclass
class WorkspaceInfo:
    """Transient value object returned by WorkspaceManager.prepare().

    ``path`` is ``None`` for the Kubernetes transport (performer self-clones).
    ``repo_url`` is the plain HTTPS URL without any embedded token — safe to
    include in dispatch payloads and logs.
    ``github_token`` is the raw token value passed to the performer so it can
    push branches and create PRs; never log this field.
    """

    path: Path | None
    branch: str
    repo_url: str
    github_token: str = ""


# ---------------------------------------------------------------------------
# Protocol (for test mocking)
# ---------------------------------------------------------------------------


class WorkspaceManagerProtocol(Protocol):
    async def prepare(self, card: dict[str, Any]) -> WorkspaceInfo: ...
    async def teardown(self, path: Path) -> None: ...


# ---------------------------------------------------------------------------
# WorkspaceManager
# ---------------------------------------------------------------------------


class _GitCommandError(RuntimeError):
    """Raised by _run_git when the subprocess returns a non-zero exit code."""


async def _run_git(
    *args: str,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float = 60.0,
) -> None:
    """Run a git subcommand, raising _GitCommandError on non-zero returncode.

    Mirrors the SubprocessTransport pattern: kill + wait on timeout; decode
    stderr with errors="replace"; log stderr at DEBUG level only (never logs
    command arguments to prevent token leakage).
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
        )
    except OSError as exc:
        msg = f"git not available or failed to start: {exc}"
        raise _GitCommandError(msg) from exc

    try:
        _, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        msg = f"git command timed out after {timeout}s"
        raise _GitCommandError(msg) from None

    stderr = _redact_tokens(stderr_bytes.decode("utf-8", errors="replace").strip())
    if stderr:
        logger.debug("git_stderr", stderr=stderr)

    if proc.returncode != 0:
        short_stderr = stderr[:200] if stderr else "(no stderr)"
        msg = f"git exited with code {proc.returncode}: {short_stderr}"
        raise _GitCommandError(msg)


class WorkspaceManager:
    """Clones repositories, creates branches, and manages workspace lifecycle.

    One instance per coordinare process. Initialized with ``ProjectConfiguration``
    and injected into ``CoordinareState`` like other services.
    """

    def __init__(self, config: ProjectConfiguration, auth: Any = None, github_service: Any = None) -> None:
        self._config = config
        self._github_org: str = config.github_org
        self._project_name: str = config.project_name
        # 151: git clone/push host (default real GitHub; bench → loopback daemon).
        # _git_base_url is HOST-facing (the coordinare's own clone); the performer
        # gets _performer_git_base_url in WorkspaceInfo.repo_url — same value in
        # production, but the bridged real bench points the container at
        # host.docker.internal while the host stays on 127.0.0.1.
        self._git_base_url: str = config.git_base_url
        self._performer_git_base_url: str = (
            getattr(config, "performer_git_base_url", None) or config.git_base_url
        )
        if self._performer_git_base_url != self._git_base_url:
            # Review (#206): setting performer_git_base_url alone is NOT ignored —
            # it is honoured, which is the subtle part: the container then clones
            # from a different host than the coordinare does. That split is correct
            # for the bench (container → host.docker.internal, host → 127.0.0.1)
            # and for a container-side mirror, but it is silent otherwise, so say
            # so once at construction rather than leaving it to be discovered.
            logger.info(
                "workspace.split_git_base_url",
                host_git_base_url=self._git_base_url,
                performer_git_base_url=self._performer_git_base_url,
                detail=(
                    "coordinare and performer clone from different hosts; "
                    "intended for the board-sim bench or a container-side mirror"
                ),
            )
        self._github_token = config.github_token  # static PAT (may be None in app mode)
        self._auth = auth  # GitHubAuth protocol — used to get current token
        self._workspace_root: Path | None = config.workspace_root
        self._agent_transport: str = config.agent_transport
        self._github_service = github_service  # GitHubService — for stale branch checks (052)

    async def get_fresh_github_token(self) -> str | None:
        """Return a current GitHub token suitable for API calls.

        Public accessor used by monitor_performer to push a refreshed
        token into each check_status payload so performers don't hit
        401s when their 1-hour App installation token expires mid-
        session.  Prefer this over reaching into ``_auth`` directly.
        Returns None when no credential source is configured.
        """
        if self._auth is not None:
            return await self._auth.get_token()
        if self._github_token is not None:
            return self._github_token.get_secret_value()
        return None

    def _make_git_env(self) -> dict[str, str]:
        """Return a minimal env dict for git subprocesses (051).

        Uses _build_minimal_env so only an allowlisted set of host vars
        is passed through and the bot git identity is always set.
        """
        return _build_minimal_env(self._config)

    async def prepare(self, card: dict[str, Any]) -> WorkspaceInfo:
        """Clone the target repo, create a card-specific branch, configure credentials.

        For the Kubernetes transport: returns ``WorkspaceInfo(path=None, ...)``
        without performing any git operations — the performer container handles
        its own workspace setup via Kubernetes Secrets.

        Raises:
            WorkspaceSetupError: if any git operation fails. Cleans up any
                partial temp directory before raising.
        """
        org = self._github_org
        project = self._project_name
        # Performer-facing remote (returned in WorkspaceInfo, handed to the container).
        repo_url = f"{self._performer_git_base_url}/{org}/{project}.git"
        branch = make_branch_name(str(card.get("id", "")), str(card.get("title", "")))

        # 052: Stale branch cleanup — detect and handle pre-existing remote branch
        # before any transport path so the performer always starts from a clean state.
        branch = await self._resolve_branch(branch, card)

        # For Kubernetes transport, the performer container handles its own workspace
        # setup via K8s Secrets — no local git ops, no token access needed here.
        if self._agent_transport == "kubernetes":
            return WorkspaceInfo(path=None, branch=branch, repo_url=repo_url, github_token="")

        # Get token: prefer auth protocol (supports App mode), fall back to static PAT
        if self._auth is not None:
            token = await self._auth.get_token()
        elif self._github_token is not None:
            token = self._github_token.get_secret_value()
        else:
            raise WorkspaceSetupError("No GitHub token available — configure github_token or github_auth=app")
        # Use plain URL — token is passed via http.extraHeader env var so it never
        # appears in process argv or /proc/*/cmdline. 151: host is configurable
        # (default real GitHub; bench overrides to a loopback git daemon).
        clone_url = f"{self._git_base_url}/{org}/{project}.git"

        container: Path | None = None
        try:
            container = Path(
                tempfile.mkdtemp(
                    dir=self._workspace_root,
                    prefix="coordinare-ws-",
                )
            )
            clone_dir = container / "repo"
            env = self._make_git_env()

            # Inject token via environment-based http.extraHeader (same
            # pattern as performer workspace — avoids token in argv).
            import base64 as _b64
            _encoded = _b64.b64encode(f"x-access-token:{token}".encode()).decode()
            env["GIT_CONFIG_COUNT"] = "1"
            env["GIT_CONFIG_KEY_0"] = "http.extraHeader"
            env["GIT_CONFIG_VALUE_0"] = f"Authorization: Basic {_encoded}"
            # Suppress git tracing to prevent auth header leakage in logs
            for _trace_var in (
                "GIT_TRACE", "GIT_TRACE_PACKET", "GIT_CURL_VERBOSE",
                "GIT_TRACE2", "GIT_TRACE_CURL",
            ):
                env.pop(_trace_var, None)

            # Clone with auth via env (required for private repos).
            await _run_git(
                "clone", "--depth=1", clone_url, str(clone_dir),
                env=env,
                timeout=120.0,
            )

            # Configure local identity for commits, derived from bot_identity config
            # so tools that read git config (rather than GIT_AUTHOR env vars) see the
            # same identity as the env-var path.
            _identity = getattr(self._config, "bot_identity", None)
            _git_name = ((_identity.name if _identity is not None else None) or "Coordinare Bot")
            _git_email = ((_identity.email if _identity is not None else None) or "coordinare@localhost")
            await _run_git(
                "config", "--local", "user.name", _git_name,
                cwd=clone_dir, env=env,
            )
            await _run_git(
                "config", "--local", "user.email", _git_email,
                cwd=clone_dir, env=env,
            )

            # Set authenticated remote URL for push operations.
            await _run_git(
                "remote", "set-url", "origin", clone_url,
                cwd=clone_dir, env=env,
            )

            # Create and check out the card-specific branch.
            await _run_git(
                "checkout", "-b", branch,
                cwd=clone_dir, env=env,
            )

        except (_GitCommandError, OSError) as exc:
            if container is not None:
                shutil.rmtree(container, ignore_errors=True)
            msg = f"Workspace setup failed: {exc}"
            raise WorkspaceSetupError(msg) from exc

        logger.info(
            "workspace_prepared",
            path=str(clone_dir),
            branch=branch,
            repo_url=repo_url,
        )
        return WorkspaceInfo(path=clone_dir, branch=branch, repo_url=repo_url, github_token=token)

    async def _resolve_branch(self, branch: str, card: dict[str, Any]) -> str:
        """Apply stale branch cleanup or suffix strategy before workspace creation.

        Returns the final branch name to use (may differ from the input when
        suffix strategy finds the original branch taken).  Silently skips when
        github_service is not set (test/health paths) or cleanup is disabled.
        """
        from coordinare.config import BranchCollisionStrategy

        cleanup_enabled = getattr(self._config, "stale_branch_cleanup", True)
        if not cleanup_enabled or self._github_service is None:
            return branch

        card_id = str(card.get("id", ""))
        strategy = getattr(self._config, "branch_collision_strategy", BranchCollisionStrategy.delete)

        if not await self._github_service.branch_exists(branch):
            return branch

        # Preserve in-flight branches so stage transitions keep prior commits
        # and don't tear down active PRs by deleting their head branch.
        card_status = str(card.get("status", "") or "").upper()
        if card_status and card_status not in {"TODO", "BACKLOG"}:
            logger.info(
                "workspace.branch_preserved_in_flight",
                branch=branch,
                card_id=card_id,
                status=card_status,
            )
            return branch

        if hasattr(self._github_service, "branch_has_open_pr"):
            open_pr_state = await self._github_service.branch_has_open_pr(branch)
            if open_pr_state is True:
                logger.info(
                    "workspace.branch_preserved_open_pr",
                    branch=branch,
                    card_id=card_id,
                )
                return branch
            if open_pr_state is None:
                logger.warning(
                    "workspace.branch_open_pr_state_unknown_preserved",
                    branch=branch,
                    card_id=card_id,
                )
                return branch

        if strategy == BranchCollisionStrategy.suffix:
            for i in range(2, 10):
                candidate = f"{branch}-{i}"
                if not await self._github_service.branch_exists(candidate):
                    logger.info(
                        "workspace.branch_suffix_applied",
                        original_branch=branch,
                        final_branch=candidate,
                        card_id=card_id,
                    )
                    return candidate
            # All suffixes taken — fall back to delete
            logger.warning("workspace.suffix_exhausted_delete_attempted", branch=branch, card_id=card_id)
            await self._github_service.delete_branch(branch)
            return branch

        # Default: delete strategy
        await self._github_service.delete_branch(branch)
        logger.info("workspace.stale_branch_delete_attempted", branch=branch, card_id=card_id)
        return branch

    async def teardown(self, path: Path) -> None:
        """Remove the workspace directory.

        Never raises — logs a warning on failure and returns so that cleanup
        errors never block the main workflow (FR-005, US3 scenario 3).
        """
        try:
            shutil.rmtree(path)
            logger.info("workspace_removed", path=str(path))
        except Exception as exc:
            logger.warning("workspace_cleanup_failed", path=str(path), error=str(exc))

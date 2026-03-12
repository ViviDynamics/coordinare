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
import re
import shutil
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import structlog

if TYPE_CHECKING:
    from coordinare.config import ProjectConfiguration

logger = structlog.get_logger(__name__)

# Regex to redact embedded GitHub tokens from git output (e.g. in clone-failure messages).
_TOKEN_RE = re.compile(r"x-access-token:[^@]*@")


def _redact_tokens(text: str) -> str:
    """Replace ``x-access-token:<value>@`` with ``x-access-token:[REDACTED]@``."""
    return _TOKEN_RE.sub("x-access-token:[REDACTED]@", text)


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

    def __init__(self, config: ProjectConfiguration) -> None:
        self._github_org: str = config.github_org
        self._project_name: str = config.project_name
        self._github_token = config.github_token
        self._workspace_root: Path | None = config.workspace_root
        self._agent_transport: str = config.agent_transport

    def _make_git_env(self) -> dict[str, str]:
        """Return an env dict for git subprocesses.

        Sets GIT_TERMINAL_PROMPT=0 so git fails rather than prompting for
        credentials interactively.
        """
        import os

        env = dict(os.environ)
        env["GIT_TERMINAL_PROMPT"] = "0"
        return env

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
        repo_url = f"https://github.com/{org}/{project}.git"
        branch = make_branch_name(str(card.get("id", "")), str(card.get("title", "")))

        # For Kubernetes transport, the performer container handles its own workspace
        # setup via K8s Secrets — no local git ops, no token access needed here.
        if self._agent_transport == "kubernetes":
            return WorkspaceInfo(path=None, branch=branch, repo_url=repo_url, github_token="")

        token = self._github_token.get_secret_value()
        clone_url = f"https://x-access-token:{token}@github.com/{org}/{project}.git"

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

            # Clone with authenticated URL (required for private repos).
            # NOTE: clone_url contains the token — do NOT log it.
            await _run_git(
                "clone", "--depth=1", clone_url, str(clone_dir),
                env=env,
                timeout=120.0,
            )

            # Configure local identity for commits.
            await _run_git(
                "config", "--local", "user.name", "Coordinare Bot",
                cwd=clone_dir, env=env,
            )
            await _run_git(
                "config", "--local", "user.email", "coordinare@localhost",
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

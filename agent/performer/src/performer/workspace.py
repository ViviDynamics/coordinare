"""Git workspace management — clone, push, cleanup."""
from __future__ import annotations

import asyncio
import base64
import errno
import os
import re
import shutil
import tempfile
from pathlib import Path

import structlog

from performer.models import Score, Stand

log = structlog.get_logger(__name__)

# Matches "Authorization: Basic <token>" or "Authorization: Bearer <token>"
# in git stderr output so credentials are never surfaced in error messages.
_AUTH_HEADER_RE = re.compile(r"Authorization:\s+\S+\s+\S+", re.IGNORECASE)


def _redact_auth_headers(text: str) -> str:
    """Replace any Authorization header values in *text* with a placeholder."""
    return _AUTH_HEADER_RE.sub("Authorization: <redacted>", text)


class WorkspaceSetupError(RuntimeError):
    """Raised when the git workspace cannot be set up."""


class BranchConflictError(RuntimeError):
    """Raised when a branch push fails due to a remote conflict."""


def _parse_owner_repo(repo_url: str) -> tuple[str, str]:
    """Extract (owner, repo) from a GitHub HTTPS URL."""
    # https://github.com/owner/repo[.git]
    parts = repo_url.rstrip("/").split("/")
    repo = parts[-1].removesuffix(".git")
    owner = parts[-2]
    return owner, repo


def _git_credential_vars(token: str) -> dict[str, str]:
    """Return ONLY the git-specific credential vars (no ``**os.environ``).

    Used to populate ``Stand.git_env`` so that AI subprocess launchers can
    merge these into the subprocess environment without inheriting unrelated
    workspace env state from the helper itself.
    """
    encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {encoded}",
        "GIT_TRACE": "0",
        "GIT_TRACE2": "0",
        "GIT_TRACE_CURL": "0",
        "GIT_CURL_VERBOSE": "0",
    }


def _git_credential_env(token: str) -> dict[str, str]:
    """Return env vars that pass *token* to git via http.extraHeader.

    Uses HTTP Basic auth with ``x-access-token`` as the username — the
    canonical form accepted by GitHub's HTTPS git endpoint and documented
    in GitHub's PAT authentication guide.  Passing the credential via
    environment variables keeps the token out of the process command line
    (``/proc/*/cmdline``) where it would be visible to other local users.

    Trace variables are explicitly disabled so git cannot echo the
    Authorization header to stderr (which would then appear in WorkspaceSetupError
    messages).
    """
    encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {encoded}",
        # Prevent git from echoing Authorization headers via trace output
        "GIT_TRACE": "0",
        "GIT_TRACE2": "0",
        "GIT_TRACE_CURL": "0",
        "GIT_CURL_VERBOSE": "0",
    }


async def _run_git(
    args: list[str],
    cwd: Path | None,
    env: dict[str, str],
    timeout: float = 120.0,
) -> tuple[int, str]:
    """Run a git command and return (returncode, stderr).

    Raises WorkspaceSetupError if the command takes longer than *timeout* seconds.
    Authorization headers are redacted from stderr before they appear in any
    error messages.
    """
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd) if cwd else None,
        env=env,
    )
    try:
        _stdout, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        # Credentials are in env vars, not args, so joining is safe
        cmd_str = " ".join(args)
        raise WorkspaceSetupError(
            f"git command timed out after {timeout}s: {cmd_str}"
        ) from None
    return proc.returncode, _redact_auth_headers(stderr_bytes.decode(errors="replace"))


async def clone_repository(score: Score) -> Stand:
    """Clone *score.repo_url* into a fresh temp directory and return a Stand.

    Clones the default/base branch (without ``--branch score.branch``) so that
    new branches — which don't yet exist on the remote — can be created locally
    via ``git checkout -b``.
    """
    try:
        tmpdir = tempfile.mkdtemp(prefix="performer-")
    except OSError as exc:
        if exc.errno == errno.ENOSPC:
            raise WorkspaceSetupError("insufficient disk space") from exc
        raise WorkspaceSetupError(f"failed to create temporary workspace: {exc}") from exc
    stand_path = Path(tmpdir)
    owner, repo = _parse_owner_repo(score.repo_url)
    clone_url = f"https://github.com/{owner}/{repo}.git"
    env = _git_credential_env(score.github_token)

    # Step 1: shallow-clone the default branch
    clone_cmd = ["git", "clone", "--depth=1", clone_url, str(stand_path)]
    try:
        returncode, stderr = await _run_git(clone_cmd, cwd=None, env=env)
    except (OSError, WorkspaceSetupError) as exc:
        shutil.rmtree(stand_path, ignore_errors=True)
        if isinstance(exc, OSError):
            if exc.errno == errno.ENOSPC:
                raise WorkspaceSetupError("insufficient disk space") from exc
            raise WorkspaceSetupError(f"clone failed: {exc}") from exc
        raise

    if returncode != 0:
        shutil.rmtree(stand_path, ignore_errors=True)
        raise WorkspaceSetupError(f"git clone failed (exit {returncode}): {stderr}")

    # Step 2: create and switch to the target branch
    checkout_cmd = ["git", "checkout", "-b", score.branch]
    try:
        returncode, stderr = await _run_git(checkout_cmd, cwd=stand_path, env=env)
    except (OSError, WorkspaceSetupError) as exc:
        shutil.rmtree(stand_path, ignore_errors=True)
        if isinstance(exc, OSError):
            raise WorkspaceSetupError(f"git checkout failed: {exc}") from exc
        raise

    if returncode != 0:
        shutil.rmtree(stand_path, ignore_errors=True)
        raise WorkspaceSetupError(f"git checkout -b failed (exit {returncode}): {stderr}")

    log.info("cloned repository", owner=owner, repo=repo, branch=score.branch)
    stand = Stand(path=stand_path, branch=score.branch)
    stand.git_env = _git_credential_vars(score.github_token)
    return stand


async def push_branch(stand: Stand, score: Score) -> None:
    """Push *stand.branch* to the remote with ``--force``.

    Coordinare-managed branches (``coordinare/<id>/<slug>``) are exclusively
    owned by the coordinare — no human ever pushes to them — so ``--force``
    is safe and correct.  ``--force-with-lease`` does not work here because
    we push directly to the URL (not a named remote), meaning git has no
    remote-tracking ref to evaluate the lease against; if the branch already
    exists on the remote git rejects the push with "(stale info)".

    Raises WorkspaceSetupError on push failure.
    """
    owner, repo = _parse_owner_repo(score.repo_url)
    remote_url = f"https://github.com/{owner}/{repo}.git"
    cmd = ["git", "-C", str(stand.path), "push", "--force", remote_url, f"HEAD:{stand.branch}"]
    env = _git_credential_env(score.github_token)
    try:
        returncode, err = await _run_git(cmd, cwd=None, env=env)
    except OSError as exc:
        if exc.errno == errno.ENOSPC:
            raise WorkspaceSetupError("insufficient disk space") from exc
        raise WorkspaceSetupError(f"git push failed: {exc}") from exc

    if returncode != 0:
        # Detect branch-already-exists / non-fast-forward patterns
        if any(
            phrase in err.lower()
            for phrase in ("rejected", "already exists", "non-fast-forward", "[remote rejected]")
        ):
            raise BranchConflictError(
                f"branch {stand.branch!r} was rejected by the remote: {err}"
            )
        raise WorkspaceSetupError(f"git push failed (exit {returncode}): {err}")

    log.info("pushed branch", branch=stand.branch, owner=owner, repo=repo)


def cleanup_stand(stand: Stand) -> None:
    """Remove the stand directory unconditionally."""
    shutil.rmtree(stand.path, ignore_errors=True)
    log.info("cleaned up stand", path=str(stand.path))

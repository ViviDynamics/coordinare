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
    # 036: Derive clone URL from repo_url to support GitHub Enterprise hosts.
    # Security: repo_url is validated by Score._validate_repo_url (HTTPS-only,
    # real host, owner/repo path) and originates from the coordinare's dispatch
    # payload. Git credential helper sends the token only to this host.
    clone_url = score.repo_url.rstrip("/")
    if not clone_url.endswith(".git"):
        clone_url += ".git"
    env = _git_credential_env(score.effective_github_token)

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

    # Step 2: try to fetch the existing remote branch (re-dispatch after feedback).
    # If it exists, check it out to preserve previous work. If not, create fresh.
    # Unshallow first so fetch can resolve the branch history.
    # Tolerate failure — repo may already be complete (non-shallow clone).
    unshallow_rc, unshallow_stderr = await _run_git(["git", "fetch", "--unshallow"], cwd=stand_path, env=env)
    if unshallow_rc != 0 and "not a shallow repository" not in (unshallow_stderr or "").lower():
        log.warning("clone_repository.unshallow_failed", exit_code=unshallow_rc, stderr=unshallow_stderr)
    fetch_cmd = ["git", "fetch", "origin", f"{score.branch}:{score.branch}"]
    fetch_rc, fetch_stderr = await _run_git(fetch_cmd, cwd=stand_path, env=env)

    if fetch_rc == 0:
        # Branch exists on remote — check it out (preserves previous commits)
        checkout_cmd = ["git", "checkout", score.branch]
        log.info("clone_repository.existing_branch", branch=score.branch)
    elif "couldn't find remote ref" in (fetch_stderr or "").lower():
        # Branch doesn't exist yet — create from main
        checkout_cmd = ["git", "checkout", "-b", score.branch]
        log.info("clone_repository.new_branch", branch=score.branch)
    else:
        # Unexpected fetch error (auth, DNS, etc.) — don't silently create a new branch
        shutil.rmtree(stand_path, ignore_errors=True)
        raise WorkspaceSetupError(
            f"git fetch failed unexpectedly (exit {fetch_rc}): {fetch_stderr}"
        )

    try:
        returncode, stderr = await _run_git(checkout_cmd, cwd=stand_path, env=env)
    except (OSError, WorkspaceSetupError) as exc:
        shutil.rmtree(stand_path, ignore_errors=True)
        if isinstance(exc, OSError):
            raise WorkspaceSetupError(f"git checkout failed: {exc}") from exc
        raise

    if returncode != 0:
        shutil.rmtree(stand_path, ignore_errors=True)
        raise WorkspaceSetupError(f"git checkout failed (exit {returncode}): {stderr}")

    # Set git identity so commits show as the bot, not the host user
    for cfg_cmd in [
        ["git", "config", "user.name", "vivi-coordinare[bot]"],
        ["git", "config", "user.email", "coordinare@users.noreply.github.com"],
    ]:
        await _run_git(cfg_cmd, cwd=stand_path, env=env)

    log.info("cloned repository", repo_url=score.repo_url, branch=score.branch)
    stand = Stand(path=stand_path, branch=score.branch)
    stand.git_env = _git_credential_vars(score.effective_github_token)
    return stand


async def push_branch(stand: Stand, score: Score) -> None:
    """Push *stand.branch* to the remote.

    Tries a regular push first to preserve PR history. Falls back to
    ``--force`` only if the regular push fails (e.g., first push to a
    new branch, or history has diverged).

    Raises WorkspaceSetupError on push failure.
    """
    # 036: Derive push URL from repo_url to support GitHub Enterprise hosts
    remote_url = score.repo_url.rstrip("/")
    if not remote_url.endswith(".git"):
        remote_url += ".git"
    # Try regular push first to preserve commit history for existing PRs
    cmd = ["git", "-C", str(stand.path), "push", remote_url, f"HEAD:{stand.branch}"]
    env = _git_credential_env(score.effective_github_token)
    try:
        returncode, err = await _run_git(cmd, cwd=None, env=env)
    except OSError as exc:
        if exc.errno == errno.ENOSPC:
            raise WorkspaceSetupError("insufficient disk space") from exc
        raise WorkspaceSetupError(f"git push failed: {exc}") from exc

    if returncode != 0:
        # Regular push failed — fall back to force push for new branches
        # or when history has diverged (e.g., first push after fresh clone)
        log.info("push_branch.regular_push_failed_trying_force", branch=stand.branch, error=err[:200])
        force_cmd = ["git", "-C", str(stand.path), "push", "--force", remote_url, f"HEAD:{stand.branch}"]
        try:
            returncode, err = await _run_git(force_cmd, cwd=None, env=env)
        except OSError as exc:
            raise WorkspaceSetupError(f"git force-push failed: {exc}") from exc
        if returncode != 0:
            raise WorkspaceSetupError(f"git push failed (exit {returncode}): {err}")

    log.info("pushed branch", branch=stand.branch, remote_url=remote_url)


async def get_head_sha(stand: Stand) -> str:
    """Return the current HEAD commit SHA for the stand's workspace.

    Raises WorkspaceSetupError if the git command fails or times out.
    The subprocess is always killed and reaped on timeout so it never leaks.
    """
    proc = await asyncio.create_subprocess_exec(
        "git", "rev-parse", "HEAD",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        cwd=str(stand.path),
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise WorkspaceSetupError("git rev-parse HEAD timed out") from None
    if proc.returncode != 0:
        raise WorkspaceSetupError(
            f"git rev-parse HEAD failed (exit {proc.returncode}) in {stand.path}"
        )
    return stdout.decode().strip()


async def commit_file(stand: Stand, path: str, content: str, message: str) -> None:
    """Write *content* to *path* in the stand's repo, commit, and push.

    If the file already exists, it is overwritten (FR-009: re-run safety).
    If the content is identical to the existing file, the commit is a no-op
    (idempotent).  Parent directories are created as needed.

    Raises WorkspaceSetupError on git failures or path traversal attempts.
    """
    # Validate path is relative and doesn't escape the workspace.
    if os.path.isabs(path) or ".." in Path(path).parts:
        raise WorkspaceSetupError(f"commit_file: unsafe path rejected: {path!r}")

    abs_path = stand.path / path
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_text(content, encoding="utf-8")

    env = {**os.environ, **stand.git_env} if stand.git_env else {**os.environ}

    # Stage the file (use -- separator to prevent option injection from paths starting with -)
    returncode, stderr = await _run_git(
        ["git", "add", "--", path], cwd=stand.path, env=env,
    )
    if returncode != 0:
        raise WorkspaceSetupError(f"git add failed (exit {returncode}): {stderr}")

    # Check if there are staged changes (exit 0 = no changes, 1 = changes, >1 = error)
    returncode, stderr = await _run_git(
        ["git", "diff", "--cached", "--quiet"], cwd=stand.path, env=env,
    )
    if returncode == 0:
        log.info("commit_file.no_changes", path=path)
        return
    if returncode > 1:
        raise WorkspaceSetupError(f"git diff --cached failed (exit {returncode}): {stderr}")

    # Set git identity for commit (container may not have global config)
    for cfg_cmd in [
        ["git", "config", "user.name", "coordinare-performer"],
        ["git", "config", "user.email", "coordinare@noreply"],
    ]:
        await _run_git(cfg_cmd, cwd=stand.path, env=env)

    # Commit
    returncode, stderr = await _run_git(
        ["git", "commit", "-m", message], cwd=stand.path, env=env,
    )
    if returncode != 0:
        raise WorkspaceSetupError(f"git commit failed (exit {returncode}): {stderr}")

    # Push (force — same rationale as push_branch: coordinare-managed branches)
    returncode, stderr = await _run_git(
        ["git", "push", "--force", "origin", f"HEAD:{stand.branch}"],
        cwd=stand.path, env=env,
    )
    if returncode != 0:
        raise WorkspaceSetupError(f"git push failed (exit {returncode}): {stderr}")

    log.info("commit_file.committed", path=path, branch=stand.branch)


def cleanup_stand(stand: Stand) -> None:
    """Remove the stand directory unconditionally."""
    shutil.rmtree(stand.path, ignore_errors=True)
    log.info("cleaned up stand", path=str(stand.path))

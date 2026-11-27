"""423: the workspace layer's checkouts for stage jobs (QAre and Progettare).

A stage job (``performer.stage_jobs``) runs one stage provider's CLI inside an ephemeral
performer container. Its checkout is made here, inside that container, so coordinare's
own host never clones or fetches (specs 107 and 555, issue #423).

Two shapes, both read-only and never pushed:

- :func:`checkout_revisions`: a pull request's head, and its base beside it as a
  worktree, each fetched by SHA at depth 1 (QAre);
- :func:`clone_default_branch`: a shallow clone of the default branch (Progettare).

The token reaches git through the environment, as an ``http.extraHeader`` scoped to the
clone URL's own scheme and host: never on a command line, never in a remote URL, never
on disk. Errors are raised as :class:`~performer.workspace.WorkspaceSetupError` with any
Authorization header redacted.
"""
from __future__ import annotations

import base64
import os
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from performer.workspace import WorkspaceSetupError, _redact_auth_headers, _run_git, _run_git_stdout

if TYPE_CHECKING:
    from pathlib import Path

#: Bound on one git step, separate from the stage job's own timeout.
GIT_STEP_TIMEOUT_SECONDS = 600.0
#: What git may see of the job's environment, besides the credential header.
_GIT_ENV = (
    "PATH", "HOME", "USER", "LANG", "LC_ALL", "TMPDIR", "TZ",
    "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "GIT_SSL_CAINFO",
)


def git_env(url: str, token: str, env: dict[str, str] | None = None) -> dict[str, str]:
    """The environment a stage job's git runs with: no prompts, no traces, and the token
    as an extra header that only the clone URL's own scheme and host receive."""
    source = os.environ if env is None else env
    result = {name: source[name] for name in _GIT_ENV if name in source}
    result.update({
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_TRACE": "0",
        "GIT_TRACE2": "0",
        "GIT_TRACE_CURL": "0",
        "GIT_CURL_VERBOSE": "0",
    })
    if token:
        parts = urlsplit(url)
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        result.update({
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": f"http.{parts.scheme}://{parts.netloc}/.extraheader",
            "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
        })
    return result


async def _git(args: list[str], *, cwd: Path | None, env: dict[str, str], what: str) -> None:
    try:
        code, stderr = await _run_git(args, cwd=cwd, env=env, timeout=GIT_STEP_TIMEOUT_SECONDS)
    except OSError as exc:
        msg = f"{what}: git could not be started: {exc.strerror or exc}"
        raise WorkspaceSetupError(msg) from exc
    if code != 0:
        msg = f"{what} failed (exit {code}): {_redact_auth_headers(stderr).strip()[-1500:]}"
        raise WorkspaceSetupError(msg)


async def checkout_revisions(
    url: str, repo: Path, head: str, *, token: str = "", base: tuple[str, Path] | None = None,
) -> None:
    """Check ``head`` out in ``repo``, detached; with ``base`` (a SHA and a directory), the
    base revision too, as a worktree of the same repository. Each is fetched by SHA, depth 1."""
    env = git_env(url, token)
    base_sha, base_dir = base if base is not None and base[0] and base[0] != head else ("", None)
    await _git(["git", "init", "-q", str(repo)], cwd=None, env=env, what="git init")
    await _git(
        ["git", "-C", str(repo), "fetch", "-q", "--no-tags", "--depth=1", url, head, *([base_sha] if base_sha else [])],
        cwd=None, env=env, what="git fetch",
    )
    await _git(["git", "-C", str(repo), "checkout", "-q", "--detach", head], cwd=None, env=env, what="git checkout")
    if base_sha and base_dir is not None:
        await _git(
            ["git", "-C", str(repo), "worktree", "add", "-q", "--detach", str(base_dir), base_sha],
            cwd=None, env=env, what="git worktree add",
        )


async def clone_default_branch(url: str, checkout: Path, *, token: str = "") -> None:
    """A shallow clone of ``url``'s default branch into ``checkout``."""
    await _git(
        ["git", "clone", "--quiet", "--depth", "1", "--no-tags", url, str(checkout)],
        cwd=None, env=git_env(url, token), what="git clone",
    )


async def diff_revisions(repo: Path, base: str, head: str) -> str:
    """The two fetched tips compared (a depth-1 checkout has no merge base)."""
    try:
        code, out = await _run_git_stdout(
            ["git", "-C", str(repo), "diff", "--no-color", base, head], cwd=None, env=git_env("", ""),
            timeout=GIT_STEP_TIMEOUT_SECONDS,
        )
    except OSError as exc:
        msg = f"git diff could not be started: {exc.strerror or exc}"
        raise WorkspaceSetupError(msg) from exc
    if code != 0:
        msg = f"git diff failed (exit {code})"
        raise WorkspaceSetupError(msg)
    return out

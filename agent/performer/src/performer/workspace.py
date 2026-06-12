"""Git workspace management — clone, push, cleanup."""
from __future__ import annotations

import asyncio
import base64
import errno
import os
import re
import shlex
import shutil
import tempfile
import threading
import time as _time
from dataclasses import dataclass
from pathlib import Path

import structlog

from performer.models import Score, Stand

log = structlog.get_logger(__name__)

# Matches "Authorization: Basic <token>" or "Authorization: Bearer <token>"
# in git stderr output so credentials are never surfaced in error messages.
_AUTH_HEADER_RE = re.compile(r"Authorization:\s+\S+\s+\S+", re.IGNORECASE)
_WORKFLOW_PERMISSION_MARKER = "refusing to allow a github app to create or update workflow"
_WORKFLOW_PATH_RE = re.compile(r"workflow `([^`]+)`", re.IGNORECASE)


def _redact_auth_headers(text: str) -> str:
    """Replace any Authorization header values in *text* with a placeholder."""
    return _AUTH_HEADER_RE.sub("Authorization: <redacted>", text)


def _summarise_git_push_error(stderr: str) -> str:
    """Return a concise, actionable push error message.

    Git can emit extremely verbose HTTP/TLS traces on failures.  For known
    permission failures we collapse that output into a short remediation hint
    so coordinare can route/retry cleanly without flooding dashboard/Slack logs.
    """
    text = (stderr or "").strip()
    lower = text.lower()
    if _WORKFLOW_PERMISSION_MARKER in lower:
        match = _WORKFLOW_PATH_RE.search(text)
        workflow_path = match.group(1) if match else ".github/workflows/*"
        return (
            f"Push rejected: attempted to modify workflow file `{workflow_path}` "
            "but the GitHub App token lacks `workflows` permission. Revert "
            "workflow-file changes and retry, or grant the app workflows permission."
        )
    return text


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
    stand.cache_env = await _activate_env_cache(score.env_cache_path)
    await _start_env_cache_services(score.env_cache_path, stand.cache_env)
    return stand


async def _activate_env_cache(env_cache_path: str) -> dict[str, str]:
    """Source ``<env_cache_path>/activate.sh`` and return the env-var delta.

    Returns an empty dict if ``env_cache_path`` is empty. Logs a warning if
    the path is set but ``activate.sh`` is missing or sourcing fails — the
    cache exists but downstream tools won't find its binaries.
    """
    if not env_cache_path:
        return {}
    activate = Path(env_cache_path) / "activate.sh"
    if not activate.exists():
        log.warning(
            "env_cache.activate_missing",
            env_cache_path=env_cache_path,
            detail=(
                "Env-cache volume mounted but activate.sh is missing — consumer "
                "tools will not see cached binaries. Bootstrap job did not write "
                "the required activation script."
            ),
        )
        return {}

    # Diff env after sourcing against a reference env (same shell, no source)
    # so we capture only the keys the script set/changed, not bash defaults.
    script = (
        f"source {shlex.quote(str(activate))} >/dev/null 2>&1 && "
        f"env -0"
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            "bash", "-c", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30.0)
    except (OSError, asyncio.TimeoutError) as exc:
        log.warning(
            "env_cache.activate_failed",
            env_cache_path=env_cache_path,
            error=str(exc),
        )
        return {}
    if proc.returncode != 0:
        log.warning(
            "env_cache.activate_nonzero",
            env_cache_path=env_cache_path,
            returncode=proc.returncode,
            stderr=stderr.decode(errors="replace")[:500],
        )
        return {}

    # Reference env: the same bash invocation without sourcing. Anything
    # unchanged between the two is a bash default we should NOT propagate.
    #
    # 087: the reference must NOT pay BASH_ENV/ENV. The container points
    # BASH_ENV at the devenv profile, which exports LD_LIBRARY_PATH (captured-deb
    # native libs) before sourcing activate.sh — in BOTH subprocesses. With the
    # profile on both sides, everything it set cancelled out of the delta, so
    # cache_env lacked LD_LIBRARY_PATH and snapshot-style agents (claude_code,
    # which does not re-source per command) couldn't load native extensions
    # (psych/libyaml → Rails wouldn't boot → no QA screenshots). Stripping
    # BASH_ENV/ENV from the reference keeps profile-set vars in the delta.
    ref_env = {k: v for k, v in os.environ.items() if k not in ("BASH_ENV", "ENV")}
    try:
        # Mirror the sourced invocation's command shape (source && env) so
        # bash-internal bookkeeping that depends on command structure (e.g.
        # checkwinsize) behaves identically on both sides of the diff.
        ref_proc = await asyncio.create_subprocess_exec(
            "bash", "-c", "source /dev/null >/dev/null 2>&1 && env -0",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=ref_env,
        )
        ref_stdout, _ = await asyncio.wait_for(ref_proc.communicate(), timeout=10.0)
    except (OSError, asyncio.TimeoutError):
        ref_stdout = b""

    def _parse(blob: bytes) -> dict[str, str]:
        out: dict[str, str] = {}
        for entry in blob.split(b"\x00"):
            if not entry:
                continue
            try:
                k, v = entry.decode("utf-8", errors="replace").split("=", 1)
            except ValueError:
                continue
            out[k] = v
        return out

    sourced = _parse(stdout)
    reference = _parse(ref_stdout)
    # Never propagate: the profile's re-entry guard (injecting _DEVENV_SOURCED=1
    # into an agent subprocess would SUPPRESS profile sourcing in backends whose
    # shells re-source per command — openclaw/pi/codex would lose the toolchain),
    # and the shell-bootstrap pointers themselves (only in the delta as an
    # artifact of stripping them from the reference).
    # COLUMNS/LINES: terminal geometry bash may inject as a checkwinsize
    # fallback (80/24) when there is no tty (CI runners) — never meaningful
    # to an agent subprocess and asymmetric between the two invocations.
    _never_propagate = {"_DEVENV_SOURCED", "BASH_ENV", "ENV", "COLUMNS", "LINES"}
    delta: dict[str, str] = {}
    for k, v in sourced.items():
        if k in _never_propagate:
            continue
        if reference.get(k) != v:
            delta[k] = v
    log.info(
        "env_cache.activated",
        env_cache_path=env_cache_path,
        var_count=len(delta),
        keys=sorted(delta.keys()),
    )
    return delta


# Spec 063 T007 — set of env-cache paths whose services-start.sh ran successfully
# during this performer process. Walked by `stop_all_env_cache_services` on
# shutdown so background daemons (redis, postgres, …) do not leak past the
# container's lifetime.
_ACTIVE_SERVICE_CACHES: dict[str, dict[str, str]] = {}

# Spec 063 Phase 4 (T023) — set True when services-health.sh exits non-zero
# after services-start.sh succeeds. The outbound PerformerResponse packaging
# reads this via `consume_env_cache_health_failure()` (which clears the flag)
# so the coordinare's monitor_performer can route it into
# EnvCacheService.mark_runtime_health_failed.
#
# Guarded by a lock so concurrent _run_env_cache_health_check coroutines (one
# per cache mounted in a single performer process) cannot race each other or
# the consumer in main.py packaging the outbound response.
_ENV_CACHE_HEALTH_FAILED: bool = False
_ENV_CACHE_HEALTH_LOCK = threading.Lock()


def consume_env_cache_health_failure() -> bool:
    """Return True (and clear) if a health-check failure occurred since last call."""
    global _ENV_CACHE_HEALTH_FAILED
    with _ENV_CACHE_HEALTH_LOCK:
        failed = _ENV_CACHE_HEALTH_FAILED
        _ENV_CACHE_HEALTH_FAILED = False
    return failed


def _mark_env_cache_health_failed() -> None:
    """Thread-safe setter used by the health-check coroutine."""
    global _ENV_CACHE_HEALTH_FAILED
    with _ENV_CACHE_HEALTH_LOCK:
        _ENV_CACHE_HEALTH_FAILED = True


# 088 US6 (FR-013) — human-readable description of the most recent
# services-start.sh failure (timeout or non-zero exit). Consumed single-shot
# by the outbound response packaging so QA can cite it as an environment
# blocker (it joins the `environment_error` channel) instead of judging the
# code change against a broken environment.
_SERVICES_START_FAILURE: str | None = None
_SERVICES_START_LOCK = threading.Lock()


def consume_services_start_failure() -> str | None:
    """Return (and clear) the stored services-start failure, if any."""
    global _SERVICES_START_FAILURE
    with _SERVICES_START_LOCK:
        failure = _SERVICES_START_FAILURE
        _SERVICES_START_FAILURE = None
    return failure


def _record_services_start_failure(
    script: str, returncode: int | str, output_tail: str
) -> None:
    """Emit the error-level event and store the failure for the QA channel."""
    log.error(
        "env_cache.services_start_failed",
        script=script,
        returncode=returncode,
        output_tail=output_tail,
    )
    global _SERVICES_START_FAILURE
    with _SERVICES_START_LOCK:
        _SERVICES_START_FAILURE = (
            f"env-cache services-start failed: {script} "
            f"(returncode={returncode}): {output_tail}".strip()
        )


async def _start_env_cache_services(
    env_cache_path: str, cache_env: dict[str, str]
) -> None:
    """Invoke ``<env_cache_path>/services/services-start.sh`` when present.

    Spec 063 Phase 1: the bootstrap drops service start/stop/health scripts into
    ``<env_cache_path>/services/``. If the start script exists and is executable,
    we run it once per performer setup with the activated env-cache vars on PATH.
    A failure is logged but does not abort workspace setup — the agent will
    surface a degraded run rather than crash the cache mount.
    """
    if not env_cache_path:
        return
    start = Path(env_cache_path) / "services" / "services-start.sh"
    if not start.is_file() or not os.access(start, os.X_OK):
        return
    env = {**os.environ, **cache_env}
    try:
        proc = await asyncio.create_subprocess_exec(
            "bash", str(start),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120.0)
    except (OSError, asyncio.TimeoutError) as exc:
        # builtin TimeoutError subclasses OSError (3.10+), so check it first.
        timed_out = isinstance(exc, asyncio.TimeoutError)
        _record_services_start_failure(
            script=str(start),
            returncode="timeout" if timed_out else type(exc).__name__,
            output_tail=str(exc) or ("timed out after 120s" if timed_out else ""),
        )
        return
    if proc.returncode != 0:
        output = (stderr or stdout).decode(errors="replace")
        _record_services_start_failure(
            script=str(start),
            returncode=proc.returncode,
            output_tail=output[-500:],
        )
        return
    log.info(
        "env_cache.services_started",
        env_cache_path=env_cache_path,
        stdout_tail=stdout.decode(errors="replace")[-200:],
    )
    # Register for shutdown cleanup. We re-record cache_env each time so a
    # rotated token (e.g. refreshed GITHUB_TOKEN) is what `services-stop.sh`
    # sees on the way out.
    _ACTIVE_SERVICE_CACHES[env_cache_path] = dict(cache_env)

    # Spec 063 Phase 4 (T023): run services-health.sh once after start. A
    # non-zero exit flips the module-level flag so the next outbound
    # PerformerResponse signals the coordinare to force env-cache regen.
    await _run_env_cache_health_check(env_cache_path, env)


async def _run_env_cache_health_check(
    env_cache_path: str, env: dict[str, str]
) -> None:
    """Run ``services-health.sh`` post-start; flag failures for the coordinare."""
    health = Path(env_cache_path) / "services" / "services-health.sh"
    if not health.is_file() or not os.access(health, os.X_OK):
        return
    try:
        proc = await asyncio.create_subprocess_exec(
            "bash", str(health),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60.0)
    except (OSError, asyncio.TimeoutError) as exc:
        log.warning(
            "env_cache.services_health_failed",
            env_cache_path=env_cache_path,
            error=str(exc),
        )
        _mark_env_cache_health_failed()
        return
    if proc.returncode != 0:
        log.warning(
            "env_cache.services_health_nonzero",
            env_cache_path=env_cache_path,
            returncode=proc.returncode,
            stderr=stderr.decode(errors="replace")[:500],
        )
        _mark_env_cache_health_failed()
        return
    log.info(
        "env_cache.services_healthy",
        env_cache_path=env_cache_path,
        stdout_tail=stdout.decode(errors="replace")[-200:],
    )


async def run_env_cache_verify(
    env_cache_path: str, cache_env: dict[str, str] | None = None
) -> tuple[bool | None, str]:
    """Run ``<env_cache_path>/verify.sh`` to confirm the bootstrap installed
    what the spec files require, BEFORE the bootstrap reports success.

    077: the bootstrap agent writes ``verify.sh`` asserting every installed
    dependency is present + runnable (it sources activate.sh, then invokes the
    tools). The env_bootstrap performer runs it after the install turn and
    FAILS the bootstrap on a non-zero exit — so a silent install failure (e.g.
    an ``apt-get install`` that located no package) can no longer be reported as
    success and have the cache marked ready. "Confirm installation before
    exiting."

    Returns ``(passed, detail)``:
      * ``passed is None``  → verify.sh absent (not run; caller treats as a
        degraded-but-not-failed legacy bootstrap for backward compatibility).
      * ``passed is False`` → verify.sh ran and failed (or errored/timed out).
      * ``passed is True``  → verify.sh passed.
    """
    if not env_cache_path:
        return None, "no env_cache_path"
    verify = Path(env_cache_path) / "verify.sh"
    if not verify.is_file():
        return None, "verify.sh absent"
    env = {**os.environ, **(cache_env or {})}
    try:
        proc = await asyncio.create_subprocess_exec(
            "bash", str(verify),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=180.0)
    except (OSError, asyncio.TimeoutError) as exc:
        log.warning(
            "env_cache.verify_errored", env_cache_path=env_cache_path, error=str(exc)
        )
        return False, f"verify.sh did not run cleanly: {exc}"
    out = out_b.decode(errors="replace")
    if proc.returncode != 0:
        log.warning(
            "env_cache.verify_nonzero",
            env_cache_path=env_cache_path,
            returncode=proc.returncode,
            output_tail=out[-500:],
        )
        return False, out[-1000:]
    log.info(
        "env_cache.verify_passed",
        env_cache_path=env_cache_path,
        output_tail=out[-200:],
    )
    return True, out[-500:]


def stop_env_cache_services(
    env_cache_path: str, cache_env: dict[str, str] | None = None
) -> None:
    """Synchronously run ``<env_cache_path>/services/services-stop.sh`` if present.

    Synchronous on purpose: this is invoked from `atexit` and signal handlers
    where the asyncio loop is no longer running. Failures are logged and
    swallowed — shutdown must never raise.
    """
    if not env_cache_path:
        return
    stop = Path(env_cache_path) / "services" / "services-stop.sh"
    if not stop.is_file() or not os.access(stop, os.X_OK):
        return
    env = {**os.environ, **(cache_env or {})}
    try:
        proc = __import__("subprocess").run(
            ["bash", str(stop)],
            env=env,
            capture_output=True,
            text=True,
            timeout=60.0,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001 — shutdown path swallows everything
        log.warning(
            "env_cache.services_stop_failed",
            env_cache_path=env_cache_path,
            error=str(exc),
        )
        return
    if proc.returncode != 0:
        log.warning(
            "env_cache.services_stop_nonzero",
            env_cache_path=env_cache_path,
            returncode=proc.returncode,
            stderr=proc.stderr[:500],
        )
        return
    log.info("env_cache.services_stopped", env_cache_path=env_cache_path)


def stop_all_env_cache_services() -> None:
    """Run services-stop.sh for every env-cache started in this process.

    Idempotent: clears the active-cache registry as it goes, so re-invocation
    from both atexit and a signal handler does not double-stop.
    """
    if not _ACTIVE_SERVICE_CACHES:
        return
    # Snapshot keys first; the loop mutates the dict.
    paths = list(_ACTIVE_SERVICE_CACHES.items())
    _ACTIVE_SERVICE_CACHES.clear()
    for path, env in paths:
        stop_env_cache_services(path, env)


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
            raise WorkspaceSetupError(
                f"git push failed (exit {returncode}): {_summarise_git_push_error(err)}",
            )

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
        raise WorkspaceSetupError(
            f"git push failed (exit {returncode}): {_summarise_git_push_error(stderr)}",
        )

    log.info("commit_file.committed", path=path, branch=stand.branch)


async def commit_files(
    stand: Stand,
    files: list[dict[str, str]],
    message: str,
) -> list[str]:
    """Batch-commit multiple files in a single git commit + push.

    Each entry in *files* must have ``path`` (relative to workspace) and
    ``content`` (full file content).  All files are written to disk, staged
    with a single ``git add``, committed with *message*, and pushed once.
    Returns the list of committed file paths.

    044: Replaces the per-file ``commit_file`` loop in the tech writer
    handler to produce 1 commit instead of N.
    """
    if not files:
        return []

    env = {**os.environ, **stand.git_env} if stand.git_env else {**os.environ}
    committed: list[str] = []

    # Write all files to disk
    for f in files:
        path = f.get("path", "")
        content = f.get("content", "")
        if not path or not isinstance(path, str):
            continue
        if os.path.isabs(path) or ".." in Path(path).parts:
            log.warning("commit_files.unsafe_path_skipped", path=path)
            continue
        abs_path = stand.path / path
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text(content, encoding="utf-8")
        committed.append(path)

    if not committed:
        return []

    # Stage all files
    returncode, stderr = await _run_git(
        ["git", "add", "--"] + committed, cwd=stand.path, env=env,
    )
    if returncode != 0:
        raise WorkspaceSetupError(f"git add (batch) failed (exit {returncode}): {stderr}")

    # Check for staged changes (exit 0 = no changes, 1 = changes, >1 = error)
    returncode, stderr = await _run_git(
        ["git", "diff", "--cached", "--quiet"], cwd=stand.path, env=env,
    )
    if returncode == 0:
        log.info("commit_files.no_changes", file_count=len(committed))
        return []
    if returncode > 1:
        raise WorkspaceSetupError(f"git diff --cached failed (exit {returncode}): {stderr}")

    # Set git identity
    for cfg_cmd in [
        ["git", "config", "user.name", "coordinare-performer"],
        ["git", "config", "user.email", "coordinare@noreply"],
    ]:
        await _run_git(cfg_cmd, cwd=stand.path, env=env)

    # Single commit
    returncode, stderr = await _run_git(
        ["git", "commit", "-m", message], cwd=stand.path, env=env,
    )
    if returncode != 0:
        raise WorkspaceSetupError(f"git commit (batch) failed (exit {returncode}): {stderr}")

    # Single push
    returncode, stderr = await _run_git(
        ["git", "push", "--force", "origin", f"HEAD:{stand.branch}"],
        cwd=stand.path, env=env,
    )
    if returncode != 0:
        raise WorkspaceSetupError(
            f"git push (batch) failed (exit {returncode}): {_summarise_git_push_error(stderr)}",
        )

    log.info("commit_files.committed", file_count=len(committed), branch=stand.branch)
    return committed


def cleanup_stand(stand: Stand) -> None:
    """Remove the stand directory (unless PERFORMER_KEEP_STAND is set).

    077: benchmarking / diagnostics need to inspect the post-job workspace
    (committed plan files, test results, docs) — but the job loop tears the
    stand down in its ``finally`` before the terminal status is even read, so an
    external grader that docker-execs after the job sees an empty container.
    Setting ``PERFORMER_KEEP_STAND=1`` skips the teardown so the workspace
    survives for inspection. Unset (the default) preserves the production
    behavior of always cleaning up.
    """
    if os.environ.get("PERFORMER_KEEP_STAND", "").strip().lower() in ("1", "true", "yes"):
        log.info("kept stand (PERFORMER_KEEP_STAND)", path=str(stand.path))
        return
    shutil.rmtree(stand.path, ignore_errors=True)
    log.info("cleaned up stand", path=str(stand.path))


# ---------------------------------------------------------------------------
# 043 — CI command execution helper
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CIRunResult:
    """Result of running a CI command in the workspace."""

    success: bool
    exit_code: int
    stdout: str
    stderr: str
    command: str
    duration_seconds: float


_MAX_OUTPUT = 2000  # truncate stdout/stderr to this many chars


def _fail_result(cmd: str, start: float, stderr_msg: str) -> CIRunResult:
    """Build a failure CIRunResult — shared by timeout and exception paths."""
    return CIRunResult(
        success=False,
        exit_code=-1,
        stdout="",
        stderr=stderr_msg[:_MAX_OUTPUT],
        command=cmd,
        duration_seconds=_time.monotonic() - start,
    )


async def _kill_proc(proc: asyncio.subprocess.Process | None) -> None:
    """Kill a subprocess and wait for it to exit (if it's still running)."""
    if proc is not None and proc.returncode is None:
        proc.kill()
        await proc.wait()


async def run_command(
    cmd: str,
    cwd: Path,
    timeout: int = 120,
) -> CIRunResult:
    """Run a shell command in *cwd* and return a structured result.

    Used by the performer to execute lint/test commands before committing.
    Truncates stdout/stderr to ``_MAX_OUTPUT`` chars to prevent oversized
    payloads in error reports.
    """
    start = _time.monotonic()
    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=timeout,
        )
    except asyncio.CancelledError:
        await _kill_proc(proc)
        raise
    except asyncio.TimeoutError:
        await _kill_proc(proc)
        return _fail_result(cmd, start, f"Command timed out after {timeout}s")
    except Exception as exc:
        await _kill_proc(proc)
        return _fail_result(cmd, start, str(exc))
    return CIRunResult(
        success=proc.returncode == 0,
        exit_code=proc.returncode if proc.returncode is not None else -1,
        stdout=(stdout_bytes.decode("utf-8", errors="replace"))[:_MAX_OUTPUT],
        stderr=(stderr_bytes.decode("utf-8", errors="replace"))[:_MAX_OUTPUT],
        command=cmd,
        duration_seconds=_time.monotonic() - start,
    )

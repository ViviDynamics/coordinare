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
from performer.noise_paths import exclude_globs, path_has_agent_config

log = structlog.get_logger(__name__)


def _write_agent_ignore(stand_path: Path) -> None:
    """131 US1: append agent-config-dir globs to the clone's ``.git/info/exclude``.

    Repo-local + untracked (never touches the target repo's ``.gitignore``,
    FR-004), so any agent's ``git add .`` / ``git add -A`` silently skips its own
    tool-config dirs. Idempotent (won't duplicate on re-setup) and fail-safe (a
    write error is logged, never aborts the clone).

    ``clone_repository`` always produces a normal clone (``.git`` is a directory).
    If ``.git`` is a *file* (a worktree/submodule gitlink — not produced here),
    we skip: the ``strip_agent_artifacts`` push-time guard still covers that
    repo, so prevention degrading to the backstop is safe."""
    git_dir = stand_path / ".git"
    if not git_dir.is_dir():
        log.debug("agent_ignore.skipped_non_dir_gitdir", path=str(git_dir))
        return
    try:
        exclude_file = git_dir / "info" / "exclude"
        exclude_file.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude_file.read_text(encoding="utf-8") if exclude_file.exists() else ""
        want = [g for g in exclude_globs() if g not in existing.split()]
        if not want:
            return
        block = "\n# spec-131: never commit performer agent tool-config dirs\n" + "\n".join(want) + "\n"
        with exclude_file.open("a", encoding="utf-8") as fh:
            fh.write(block)
        log.info("agent_ignore.written", count=len(want))
    except OSError as exc:
        log.warning("agent_ignore.write_failed", error=str(exc))


async def strip_agent_artifacts(stand: Stand, env: dict[str, str]) -> list[str]:
    """131 US2: remove any tracked agent-config paths from the branch before push.

    Prevention (``.git/info/exclude``) handles the normal ``git add .`` case; this
    is the backstop for ``git add -f`` and already-tracked junk. Lists tracked
    files, strips those whose path has an agent-config segment via
    ``git rm -r --cached`` + a removal commit (strip-and-continue; the real change
    survives). No-op when nothing offending is tracked. Returns the stripped
    paths (paths only — never contents, FR-007)."""
    # git ls-files prints to STDOUT (which _run_git discards), so run it directly.
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "ls-files", "-z",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(stand.path), env=env,
        )
        out_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=30.0)
    except (TimeoutError, OSError):
        return []
    if proc.returncode != 0:
        return []
    tracked = [p for p in out_bytes.decode("utf-8", "replace").split("\0") if p]
    offending = sorted({p for p in tracked if path_has_agent_config(p)})
    if not offending:
        return []
    rc, stderr = await _run_git(
        ["git", "rm", "-r", "--cached", "--quiet", "--"] + offending, cwd=stand.path, env=env,
    )
    if rc != 0:
        raise WorkspaceSetupError(
            f"commit guard: failed to unstage agent artifacts (exit {rc}): {stderr}"
        )
    rc, stderr = await _run_git(
        ["git", "commit", "-m", "chore: remove performer agent tool-config artifacts (spec 131)"],
        cwd=stand.path, env=env,
    )
    if rc != 0:
        raise WorkspaceSetupError(
            f"commit guard: failed to commit artifact removal (exit {rc}): {stderr}"
        )
    log.warning("commit_guard.agent_artifact_stripped", paths=offending, count=len(offending))
    return offending

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

    # 131 US1: prevent agents from committing their own tool-config dirs.
    _write_agent_ignore(stand_path)

    log.info("cloned repository", repo_url=score.repo_url, branch=score.branch)
    stand = Stand(path=stand_path, branch=score.branch)
    stand.git_env = _git_credential_vars(score.effective_github_token)
    stand.cache_env = await _activate_env_cache(score.env_cache_path)
    await _start_env_cache_services(score.env_cache_path, stand.cache_env)
    return stand


# 120 (US2): markers an activate.sh uses to advertise a language toolchain →
# the binary that MUST resolve on PATH once the cache is activated. Keyed on what
# the cache itself declares, so a cache that references none asserts nothing
# (non-Ruby / toolchain-less projects are unaffected). Markers are deliberately
# specific (a path segment or an env-var assignment) so a passing mention in a
# comment does not false-positive a non-Ruby project.
_TOOLCHAIN_ADVERTISEMENTS: tuple[tuple[str, str], ...] = (
    (".rbenv/", "ruby"),
    ("RBENV_ROOT", "ruby"),
)


def _unresolved_advertised_toolchain(
    activate_text: str, sourced_path: str
) -> str | None:
    """Return a names-only reason if activate.sh advertises a toolchain whose
    binary does not resolve on the post-activation PATH, else None.

    Pure (no I/O beyond the PATH lookup it is handed): asserts only toolchains the
    cache explicitly references, so caches that advertise none assert nothing. The
    returned string carries category names only — never secret values."""
    if not sourced_path:
        return None
    expected: set[str] = {
        binary
        for marker, binary in _TOOLCHAIN_ADVERTISEMENTS
        if marker in activate_text
    }
    missing = sorted(
        b for b in expected if shutil.which(b, path=sourced_path) is None
    )
    if missing:
        return f"advertised_toolchain_unresolved: {', '.join(missing)}"
    return None


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
    #
    # 120 (US2): pin DEVENV to THIS cache and clear the profile re-entry guard
    # before sourcing. activate.sh resolves the project toolchain through $DEVENV
    # (e.g. ``$DEVENV/.rbenv/versions/X/bin``). The performer process already
    # sourced the devenv profile at startup (``_DEVENV_SOURCED=1`` in os.environ),
    # so without clearing the guard the BASH_ENV profile SKIPS in this subshell,
    # $DEVENV is never set, and activate.sh prepends bogus empty-$DEVENV paths to
    # PATH — the consumer then reports "ruby not installed" even though the cache
    # is correct on disk. Exporting DEVENV inside the command (after BASH_ENV has
    # run) makes toolchain resolution deterministic regardless of profile state;
    # clearing the guard lets the profile re-run so the captured-deb
    # LD_LIBRARY_PATH is set too. (Mirrors the _DEVENV_SOURCED fix already used by
    # _start_env_cache_services.)
    source_env = {k: v for k, v in os.environ.items() if k != "_DEVENV_SOURCED"}
    # 120 fix: skip the profile's spec-117 services-start during activation. With
    # the re-entry guard cleared the profile runs in full, which includes starting
    # postgres/redis (initdb can take >30s) — that blew this subprocess's timeout,
    # leaving cache_env empty and the QA agent without ruby on PATH. We only need
    # the env here (PATH/LD_LIBRARY_PATH/activate.sh); _start_env_cache_services
    # starts the services separately right after with a 300s budget.
    source_env["_DEVENV_SKIP_SERVICES"] = "1"
    script = (
        f"export DEVENV={shlex.quote(str(env_cache_path))}; "
        f"source {shlex.quote(str(activate))} >/dev/null 2>&1 && "
        f"env -0"
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            "bash", "-c", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=source_env,
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
    # 120 (US2): also strip _DEVENV_SOURCED from the reference so both sides
    # treat the profile re-entry guard identically (the sourced side clears it);
    # otherwise the guard's presence on only one side could skew the delta.
    ref_env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("BASH_ENV", "ENV", "_DEVENV_SOURCED")
    }
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
    # 120 (US2): DEVENV is the bootstrap pointer we export ourselves before
    # sourcing activate.sh; it must not leak into the cache delta (the agent's
    # PATH/LD_LIBRARY_PATH are already fully resolved, so DEVENV is not needed
    # downstream, and an empty-script cache must still yield an empty delta).
    # SHLVL: bash's shell-nesting counter — it increments per subshell and is
    # asymmetric between the sourced and reference invocations at some nesting
    # depths (e.g. CI runners), so it must never enter the cache delta. Same
    # category as COLUMNS/LINES.
    _never_propagate = {
        "_DEVENV_SOURCED", "BASH_ENV", "ENV", "COLUMNS", "LINES", "DEVENV",
        "SHLVL", "_DEVENV_SKIP_SERVICES",
    }
    delta: dict[str, str] = {}
    for k, v in sourced.items():
        if k in _never_propagate:
            continue
        if reference.get(k) != v:
            delta[k] = v

    # 120 (US2/FR-010/FR-011): verify the project toolchain the cache ADVERTISES
    # actually resolves after activation, and record it for diagnosability. Only
    # toolchains the cache itself references are asserted, so non-Ruby caches
    # assert nothing. An advertised-but-unresolved toolchain is surfaced through
    # the existing env-failure channels (environment_error for QA + the
    # env_cache_health_failed signal the coordinare's evidence floor routes to a
    # HOLD) rather than letting the consumer judge code against a broken env.
    # Only assert against the cache's own PATH contribution (the activated delta),
    # not a system-PATH fallback — a broken activate.sh that never touched PATH
    # must not be masked by a system binary that the cache does not provide.
    sourced_path = delta.get("PATH", "")
    try:
        activate_text = activate.read_text(errors="replace")
    except OSError:
        activate_text = ""
    toolchain_issue = _unresolved_advertised_toolchain(activate_text, sourced_path)
    log.info(
        "env_cache.activated",
        env_cache_path=env_cache_path,
        var_count=len(delta),
        keys=sorted(delta.keys()),
        toolchain_resolved=(toolchain_issue is None),
    )
    if toolchain_issue is not None:
        log.warning(
            "env_cache.toolchain_unresolved",
            env_cache_path=env_cache_path,
            reason=toolchain_issue,
        )
        _record_env_cache_activation_failure(toolchain_issue)
        _mark_env_cache_health_failed()
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

# Outer cap on the services-start.sh run. MUST exceed the script's own internal
# readiness wait (spec-111 postgres pg_isready loop is 180s) plus initdb/createdb/
# redis time. 300s = 180s readiness + margin. (Kept as a named constant so the
# timeout and the failure message can never drift apart — the message used to say
# a stale "120s" while the real cap was 300s.)
_SERVICES_START_TIMEOUT_S = 300.0


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


def _record_env_cache_activation_failure(reason: str) -> None:
    """120 (US2): store an env-cache ACTIVATION failure for the QA env_error
    channel, with an accurate message (the toolchain a cache advertised did not
    resolve after activation) — distinct from a services-start failure. Shares
    the same single-shot channel consumed by the outbound response packaging.
    ``reason`` carries category names only, never secret values."""
    global _SERVICES_START_FAILURE
    with _SERVICES_START_LOCK:
        _SERVICES_START_FAILURE = (
            f"env-cache activation incomplete: {reason}".strip()
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
    # 109: services-start.sh (and services-health.sh, run with this same env) invoke
    # bare `initdb`/`postgres`/`redis-server`/`pg_isready`, which are only on PATH —
    # and whose native libs (e.g. libicu) are only on LD_LIBRARY_PATH — AFTER the
    # devenv profile (BASH_ENV) re-sources activate.sh + the captured-deb *.so
    # extraction. The performer process already sourced that profile at startup
    # (_DEVENV_SOURCED=1 in os.environ), so a non-interactive `bash script` would
    # hit the profile's re-entry guard and SKIP re-sourcing — running the start with
    # the STALE pre-service-extraction PATH/LD_LIBRARY_PATH (service debs are fetched
    # AFTER cache_env was snapshotted), so postgres binaries are not found and the
    # service never starts. Clear the guard so BASH_ENV re-sources fresh and the
    # now-extracted service binaries + libs are visible. (Empirically: guard set →
    # `initdb` NOT-FOUND; guard cleared → `initdb` found, `postgres --version` OK.)
    env.pop("_DEVENV_SOURCED", None)
    # Prevent the devenv profile's spec-117 start-on-activation from ALSO running
    # services-start.sh when this bash sources BASH_ENV: THIS call is the explicit
    # service start, so a profile-nested run would double-launch postgres/redis in
    # the same shell — extra teardown churn (and its ~60s TIME_WAIT sockets, which
    # the next run's port check trips over) plus double the time under the outer
    # cap. The profile still runs (guard cleared above) so it sets the captured-deb
    # LD_LIBRARY_PATH; only its service start is skipped. services-start.sh itself
    # does not consult this flag, so the explicit run below is unaffected.
    env["_DEVENV_SKIP_SERVICES"] = "1"
    # Capture to a temp FILE and wait on PROCESS EXIT (proc.wait), NOT a PIPE with
    # communicate(). services-start.sh backgrounds long-lived daemons; the postgres
    # launcher (``_pg_as postgres ... &``) runs the function in a subshell that then
    # blocks in wait() on the postgres daemon forever (wchan=do_wait). With
    # stdout=PIPE, communicate() waits for the pipe to reach EOF — which that
    # daemon-holding subshell defers indefinitely — so communicate() burned the
    # entire timeout even though the script had already finished and postgres/redis
    # were up (observed live as a bogus 300s "services-start timeout" while the DB
    # was actually running). proc.wait() returns the moment the main script process
    # exits, independent of any orphaned background jobs.
    out_fd, out_path = tempfile.mkstemp(prefix="services-start.", suffix=".log")
    try:
        try:
            with os.fdopen(out_fd, "wb") as out_fh:
                proc = await asyncio.create_subprocess_exec(
                    "bash", str(start),
                    stdout=out_fh,
                    stderr=asyncio.subprocess.STDOUT,
                    env=env,
                )
                await asyncio.wait_for(proc.wait(), timeout=_SERVICES_START_TIMEOUT_S)
        except (OSError, asyncio.TimeoutError) as exc:
            # builtin TimeoutError subclasses OSError (3.10+), so check it first.
            timed_out = isinstance(exc, asyncio.TimeoutError)
            if timed_out:
                try:
                    proc.kill()  # best-effort; proc exists once wait_for started
                except Exception:
                    pass
            _record_services_start_failure(
                script=str(start),
                returncode="timeout" if timed_out else type(exc).__name__,
                output_tail=str(exc)
                or (f"timed out after {_SERVICES_START_TIMEOUT_S:.0f}s" if timed_out else ""),
            )
            return
        try:
            services_out = Path(out_path).read_text(errors="replace")
        except OSError:
            services_out = ""
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass
    if proc.returncode != 0:
        _record_services_start_failure(
            script=str(start),
            returncode=proc.returncode,
            output_tail=services_out[-500:],
        )
        return
    log.info(
        "env_cache.services_started",
        env_cache_path=env_cache_path,
        stdout_tail=services_out[-200:],
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


async def run_service_readiness(
    env_cache_path: str,
    cache_env: dict[str, str] | None,
    declared_services: list[dict] | None,
    inference_result: dict | None,
    *,
    health_retries: int = 2,
    retry_delay: float = 2.0,
) -> tuple[bool, list[dict]]:
    """101: gate the env-cache bootstrap on REQUIRED declared services being
    running + connectable.

    Returns ``(ok, failures)`` where ``failures`` is a list of
    ``{"service": <names>, "reason": <secret-free>}``. ``ok`` is True when there
    are no required declared services (no-op), or when every required service's
    ``services-start.sh`` + ``services-health.sh`` succeed. A rejected/empty
    inference manifest with required services, or a required service that won't
    start/connect, → ``ok=False``. Optional services (``required: False``) that
    fail are warned, not blocking.

    Reuses the 091/063 ``_start_env_cache_services`` (which also runs the health
    check) + the consumable failure flags; the health check is retried briefly so
    a slow-to-accept service isn't falsely failed. Reasons are secret-free — they
    name the service + a generic cause, never raw script output or env values.
    """
    declared = declared_services or []
    required = [s for s in declared if s.get("required", True)]
    if not required:
        return True, []  # no required services → gate is a no-op (FR-007)

    names = ", ".join(str(s.get("name") or s.get("kind") or "?") for s in required)
    inference_ok = bool((inference_result or {}).get("inference_succeeded"))
    manifest = Path(env_cache_path) / "services" / "services.json"
    # Short-circuit to "rejected" ONLY when there is genuinely nothing to act on:
    # inference did not succeed AND no services.json was produced (the website
    # rejected-manifest case). If a manifest EXISTS — even after an inference
    # TIMEOUT (inference_succeeded=False but a manifest was written) — defer to the
    # real services-start + services-health check below, which (not the inference
    # success flag) is the ground truth for connectability.
    if not inference_ok and not manifest.is_file():
        # Clear any stale per-job failure flags before this early return so a flag
        # set by a prior operation can't leak into downstream response packaging.
        consume_services_start_failure()
        consume_env_cache_health_failure()
        reason = "no services manifest produced (inference failed/rejected) — required service(s) not set up"
        log.warning(
            "env_bootstrap.service_readiness",
            services=names, installed=False, started=False, connectable=False,
            reason=reason, bootstrap_outcome="error",
        )
        return False, [{"service": names, "reason": reason}]

    # Start (and health-check, internally) the services, bounded; reuse 063/091.
    consume_services_start_failure()
    consume_env_cache_health_failure()
    await _start_env_cache_services(env_cache_path, dict(cache_env or {}))
    start_failed = consume_services_start_failure() is not None
    health_failed = consume_env_cache_health_failure()

    # Brief bounded retry on a health-only failure (slow-to-accept service).
    if health_failed and not start_failed:
        env = {**os.environ, **(cache_env or {})}
        for _ in range(max(0, health_retries)):
            await asyncio.sleep(retry_delay)
            await _run_env_cache_health_check(env_cache_path, env)
            if not consume_env_cache_health_failure():
                health_failed = False
                break

    if start_failed or health_failed:
        # Secret-free reason: name the service + the failing stage, NOT raw output.
        cause = "services-start failed" if start_failed else "not connectable (services-health non-zero)"
        reason = f"required service(s) [{names}]: {cause}"
        log.warning(
            "env_bootstrap.service_readiness",
            services=names, installed=True,
            started=not start_failed, connectable=False,
            reason=reason, bootstrap_outcome="error",
        )
        return False, [{"service": names, "reason": reason}]

    log.info(
        "env_bootstrap.service_readiness",
        services=names, installed=True, started=True, connectable=True,
        bootstrap_outcome="ok",
    )
    return True, []


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
    # 131 US2: last line of defence — strip any agent tool-config artifacts that
    # got committed (e.g. an agent's `git add -f`) before they reach the PR. No-op
    # when the tree is clean.
    guard_env = {**os.environ, **stand.git_env} if stand.git_env else {**os.environ}
    await strip_agent_artifacts(stand, guard_env)

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


async def _git_ignored_subset(
    paths: list[str], cwd: Path, env: dict[str, str], timeout: float = 30.0,
) -> set[str]:
    """Return the subset of *paths* git would refuse to add as ignored.

    ``git check-ignore`` lists matching paths on STDOUT (which ``_run_git``
    discards), so this runs the probe directly. Exit 0 = some ignored (listed),
    1 = none ignored, 128 = error — on error we return an empty set (fail-open:
    let the normal ``git add`` surface a real problem rather than silently drop
    paths).
    """
    if not paths:
        return set()
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "check-ignore", "--", *paths,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd), env=env,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (TimeoutError, OSError):
        return set()
    if proc.returncode not in (0, 1):
        return set()
    return {line for line in out.decode("utf-8", "replace").splitlines() if line}


def _safe_doc_deletions(deletions: list | None) -> list[str]:
    """Filter model-supplied deletion paths to safe, docs-scoped relative paths.

    124: the documenter may retire dead wiki pages, but deletion is a sharp tool —
    restrict it to ``docs/`` (the documenter's domain) so a hallucinated or wrong
    path can never ``git rm`` source code or a root file like ``AGENTS.md`` /
    ``README.md``. Absolute paths and ``..`` traversal are rejected outright.
    """
    safe: list[str] = []
    for d in deletions or []:
        if not isinstance(d, str) or not d:
            continue
        if os.path.isabs(d) or ".." in Path(d).parts:
            log.warning("commit_files.unsafe_deletion_skipped", path=d)
            continue
        if Path(d).parts[:1] != ("docs",):
            log.warning("commit_files.non_docs_deletion_skipped", path=d)
            continue
        safe.append(d)
    return safe


async def commit_files(
    stand: Stand,
    files: list[dict[str, str]],
    message: str,
    deletions: list[str] | None = None,
) -> list[str]:
    """Batch-commit multiple files in a single git commit + push.

    Each entry in *files* must have ``path`` (relative to workspace) and
    ``content`` (full file content).  All files are written to disk, staged
    with a single ``git add``, committed with *message*, and pushed once.

    124: optional *deletions* is a list of repo-relative paths (restricted to
    ``docs/`` by :func:`_safe_doc_deletions`) to ``git rm`` in the SAME commit —
    this lets the documenter RETIRE dead/low-value pages, not only rewrite them.
    Returns the list of paths that changed (written + removed).

    044: Replaces the per-file ``commit_file`` loop in the tech writer
    handler to produce 1 commit instead of N.
    """
    files = files or []
    del_paths = _safe_doc_deletions(deletions)
    if not files and not del_paths:
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

    # Drop gitignored paths before staging. A model may emit a doc path under a
    # gitignored dir (e.g. Rails' .bundle); `git add` of an explicitly-named
    # ignored path exits non-zero and would poison the WHOLE batch (blocked #171
    # at documenting). Filter + log them so one bad path can't fail the commit.
    if committed:
        ignored = await _git_ignored_subset(committed, stand.path, env)
        if ignored:
            log.warning(
                "commit_files.ignored_paths_skipped",
                paths=sorted(ignored), count=len(ignored),
            )
            committed = [p for p in committed if p not in ignored]

    # Stage deletions: only paths that actually exist on disk (a nonexistent path
    # is a no-op); --ignore-unmatch keeps an untracked-but-present path from
    # failing the whole batch. `git rm` both removes from disk AND stages.
    removed: list[str] = [d for d in del_paths if (stand.path / d).exists()]
    if removed:
        returncode, stderr = await _run_git(
            ["git", "rm", "-q", "--ignore-unmatch", "--"] + removed, cwd=stand.path, env=env,
        )
        if returncode != 0:
            raise WorkspaceSetupError(f"git rm (batch) failed (exit {returncode}): {stderr}")

    if not committed and not removed:
        return []

    # Stage all written files
    if committed:
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
        log.info("commit_files.no_changes", file_count=len(committed), deleted=len(removed))
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

    log.info(
        "commit_files.committed",
        file_count=len(committed), deleted=len(removed), branch=stand.branch,
    )
    return committed + removed


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

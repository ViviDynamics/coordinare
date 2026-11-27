"""404: one QAre run, detached from the daemon so a restart finds it running or finished.

The QAre provider starts this module as its own process (``python -m
coordinare.services.qare_runner <job dir>``) in a session of its own, so the run outlives
a daemon restart. Everything it needs is in ``<job dir>/request.json``, which holds no
secret; the GitHub token for the checkout arrives in the environment and is removed from
it before anything else runs. When the run ends, one file says how:
``<job dir>/status.json``, written atomically and last.

Steps, each of which can end the run:

1. ``qare --version`` must print the pinned version.
2. The pull request's head and base are fetched into ``repo`` and ``base``.
3. The QA profile is found: the base revision's when it has one (the profile that runs is
   then one the pull request cannot change, as in QAre's own pipeline), else the head's.
4. The work item's criteria and the change's diff are written for the planner.
5. ``qare plan --criteria`` maps each criterion to checks. A job whose criteria carry no
   checks runs nothing in QAre (each is ``unverified``), so the plan comes first.
6. ``qare run --plan`` runs them with posting off and writes ``evidence/result.json``.

Nothing here reads the result: the provider does, so the rules for what a result means
live in one place.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import os
import shutil
import signal
import sys
import time
from http import HTTPStatus
from pathlib import Path
from typing import Any

import httpx

REQUEST_FILE = "request.json"
STATUS_FILE = "status.json"
LAUNCH_FILE = "launch.json"
LOG_FILE = "runner.log"
PLAN_FILE = "plan.json"
CRITERIA_FILE = "criteria.json"
DIFF_FILE = "change.diff"
EVIDENCE_DIR = "evidence"
#: The environment variable the provider hands the checkout token in.
CREDENTIAL_ENV = "COORDINARE_QARE_GITHUB_TOKEN"

#: Outcomes the runner reports. The provider maps each onto the stage contract.
RAN = "ran"  # qare run ended; its exit code and result.json say how
UNAVAILABLE = "unavailable"  # qare could not be started at all
MISCONFIGURED = "misconfigured"  # the pinned version is not the one installed
NO_PROFILE = "no_profile"  # neither side of the change has a QA profile
NO_CRITERIA = "no_criteria"  # the work item states no acceptance criteria
ERROR = "error"  # a step before the verdict failed; nothing was decided
TIMEOUT = "timeout"  # the run took longer than timeout_seconds

#: What QAre and the nare it runs may see of the host. Everything else, coordinare's own
#: secrets included, stays out; the operator adds names with ``env_passthrough``.
_BASE_ENV = (
    "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TMPDIR", "TZ",
    "DOCKER_HOST", "DOCKER_CONFIG", "DOCKER_CONTEXT", "DOCKER_CERT_PATH", "DOCKER_TLS_VERIFY",
    "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME", "XDG_CACHE_HOME",
)
_TAIL = 1500
#: The most of a locally computed diff handed to the planner.
_MAX_DIFF_CHARS = 2_000_000
#: GitHub's answers for a compare diff past its size limits. Any other failure is retried.
_DIFF_TOO_LARGE = frozenset({HTTPStatus.NOT_ACCEPTABLE, HTTPStatus.UNPROCESSABLE_ENTITY})
#: In ``command``, replaced by a name for the step, so a container can be removed on timeout.
NAME_PLACEHOLDER = "{name}"
#: Arguments of a command the log shows; the rest are paths it does not need.
_SHOWN_ARGS = 3


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    detach = bool(args) and args[0] == "--detach"
    if detach:
        args = args[1:]
    if len(args) != 1:
        sys.stderr.write("usage: python -m coordinare.services.qare_runner [--detach] <job dir>\n")
        return 2
    token = os.environ.pop(CREDENTIAL_ENV, "")
    job_dir = Path(args[0]).expanduser().absolute()
    if detach and _detached(job_dir):
        return 0  # the launcher: the run carries on in the detached child
    run_job(job_dir, token=token)
    return 0


def _detached(job_dir: Path) -> bool:
    """Fork the run into a session of its own; True in the launcher once the run has ended.

    The launcher records the run's pid, then waits for the run and reaps it, so a finished
    run never lingers as a zombie, even where the daemon is the container's PID 1 and reaps
    only its own children. The run is in another session, so stopping the launcher (a daemon
    shutting down) does not stop the run.
    """
    pid = os.fork()
    if pid:
        _write_atomic(job_dir / LAUNCH_FILE, json.dumps({"pid": pid, "started_at": time.time()}))
        with contextlib.suppress(ChildProcessError, InterruptedError):
            os.waitpid(pid, 0)
        return True
    os.setsid()
    return False


def run_job(job_dir: Path, *, token: str = "", env: dict[str, str] | None = None) -> dict[str, Any]:
    """Run the job in ``job_dir`` to its end and write ``status.json``. Returns the status."""
    source = dict(os.environ if env is None else env)
    source.pop(CREDENTIAL_ENV, None)
    job_dir = job_dir.expanduser().absolute()
    started = time.monotonic()
    try:
        request = json.loads((job_dir / REQUEST_FILE).read_text())
        status = asyncio.run(_Run(job_dir, request, token=token, env=source, started=started).run())
    except _StopError as stop:
        status = stop.status
    except Exception as exc:  # a runner bug must still end the run with a status
        status = {"outcome": ERROR, "step": "runner", "reason": f"{type(exc).__name__}: {exc}"}
    _drop_checkouts(job_dir)
    status["finished_at"] = time.time()
    status["elapsed_seconds"] = round(time.monotonic() - started, 1)
    _write_atomic(job_dir / STATUS_FILE, json.dumps(status, indent=2))
    return status


def _drop_checkouts(job_dir: Path) -> None:
    """Remove the run's checkouts once it ended: the evidence is all that is read later."""
    for name in ("base", "repo"):
        shutil.rmtree(job_dir / name, ignore_errors=True)


class _StopError(Exception):
    def __init__(self, status: dict[str, Any]) -> None:
        super().__init__(status.get("reason", ""))
        self.status = status


class _Run:
    """One job's steps, sharing its directory, deadline, log and environments."""

    def __init__(
        self, job_dir: Path, request: dict[str, Any], *, token: str, env: dict[str, str], started: float,
    ) -> None:
        self.job_dir = job_dir
        self.request = request
        self.token = token
        self.env = env
        self.deadline = started + float(request.get("timeout_seconds") or 3600)
        self.command = [str(part) for part in request["command"]]
        self.qare_env = _qare_env(env, request.get("env_passthrough") or [])
        self.log = job_dir / LOG_FILE
        self.head = str(request["head_sha"])
        self.base = str(request.get("base_sha") or "")
        self.repo = job_dir / "repo"
        self.base_dir = job_dir / "base"
        #: Whether there is a base revision of its own to check (and so a base checkout).
        self.two_sided = bool(self.base) and self.base != self.head

    async def run(self) -> dict[str, Any]:
        await self._check_version()
        await self._checkout()
        profile = self._profile()
        criteria = [{"id": c["id"], "text": c["text"]} for c in self.request.get("criteria") or []]
        if not criteria:
            raise _StopError({
                "outcome": NO_CRITERIA, "step": "criteria",
                "reason": "the work item states no acceptance criteria, so QAre has nothing to prove",
            })
        _write_atomic(self.job_dir / CRITERIA_FILE, json.dumps(criteria, indent=2))
        _write_atomic(self.job_dir / DIFF_FILE, await self._diff())
        code, _out, err = await self._call([
            *self.command, "plan", "--criteria", str(self.job_dir / CRITERIA_FILE),
            "--diff", str(self.job_dir / DIFF_FILE), "--profile", str(profile),
            "--out", str(self.job_dir / PLAN_FILE),
        ], cwd=self.repo, env=self.qare_env, step="plan")
        if code != 0:
            raise _StopError({"outcome": ERROR, "step": "plan", "exit_code": code, "reason": _tail(err) or "qare plan failed"})
        run = [*self.command, "run", "--plan", str(self.job_dir / PLAN_FILE), "--id", self.request["request_id"],
               "--repo", str(self.repo), "--base", self.base or self.head, "--head", self.head,
               "--profile", str(profile), "--evidence", str(self.job_dir / EVIDENCE_DIR)]
        if self.two_sided:
            run += ["--base-repo", str(self.base_dir)]
        code, _out, err = await self._call(run, cwd=self.repo, env=self.qare_env, step="run")
        if code is None:
            raise _StopError({"outcome": UNAVAILABLE, "step": "run", "reason": err})
        return {"outcome": RAN, "step": "run", "exit_code": code, "reason": _tail(err)}

    async def _check_version(self) -> None:
        code, out, err = await self._call([*self.command, "--version"], cwd=self.job_dir, env=self.qare_env, step="version")
        if code is None:
            raise _StopError({"outcome": UNAVAILABLE, "step": "version", "reason": err})
        installed = out.strip()
        pinned = self.request["version"]
        if code != 0 or installed != pinned:
            raise _StopError({
                "outcome": MISCONFIGURED, "step": "version",
                "reason": f"qare --version printed {installed or _tail(err)!r}; the pinned version is {pinned}",
            })

    async def _checkout(self) -> None:
        git_env = {k: self.env[k] for k in _BASE_ENV if k in self.env}
        git_env["GIT_TERMINAL_PROMPT"] = "0"
        web = str(self.request["web_base"]).rstrip("/")
        if self.token:
            # In the environment, never on a command line or in the remote URL, where git
            # and ps would show it.
            basic = base64.b64encode(f"x-access-token:{self.token}".encode()).decode()
            git_env.update({
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": f"http.{web}/.extraheader",
                "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
            })
        url = f"{web}/{self.request['repository']}.git"
        two_sided = self.two_sided
        repo = str(self.repo)
        steps = [
            ["git", "init", "-q", repo],
            ["git", "-C", repo, "fetch", "-q", "--no-tags", "--depth=1", url, self.head, *([self.base] if two_sided else [])],
            ["git", "-C", repo, "checkout", "-q", "--detach", self.head],
        ]
        if two_sided:
            steps.append(["git", "-C", repo, "worktree", "add", "-q", "--detach", str(self.base_dir), self.base])
        for argv in steps:
            code, _out, err = await self._call(argv, cwd=self.job_dir, env=git_env, step="checkout")
            if code != 0:
                raise _StopError({"outcome": ERROR, "step": "checkout", "reason": _tail(err) or f"{argv[3]} failed"})

    def _profile(self) -> Path:
        name = self.request.get("profile") or ".qa"
        for side in ((self.base_dir,) if self.two_sided else ()) + (self.repo,):
            if (side / name).is_dir():
                return side / name
        raise _StopError({
            "outcome": NO_PROFILE, "step": "profile",
            "reason": (
                f"the repository has no QA profile at {name}/ on the head or the base; run "
                "`qare init` in the repository, commit the profile it writes, and restart QA"
            ),
        })

    async def _diff(self) -> str:
        """The change for the planner: GitHub's compare, else the two checked-out trees.

        GitHub refuses a compare diff past its size limits; the planner still gets one,
        from the trees both fetched (a shallow checkout has no merge base, so it is the
        two tips compared, and the run says so in its log).
        """
        if not self.two_sided:
            return ""
        try:
            return await asyncio.to_thread(
                _change_diff, self.request, base=self.base, head=self.head, token=self.token,
                deadline=self.deadline,
            )
        except _StopError as refused:
            if refused.status.get("http_status") not in _DIFF_TOO_LARGE:
                raise
            code, out, err = await self._call(
                ["git", "-C", str(self.repo), "diff", "--no-color", self.base, self.head],
                cwd=self.job_dir, env={k: self.env[k] for k in _BASE_ENV if k in self.env},
                step=f"diff (fallback: {refused.status.get('reason')})",
            )
            if code != 0:
                raise _StopError({"outcome": ERROR, "step": "diff", "reason": _tail(err) or "git diff failed"}) from refused
            if len(out) > _MAX_DIFF_CHARS:
                out = out[: out.rfind("\n", 0, _MAX_DIFF_CHARS) + 1]
            return out

    async def _call(self, argv: list[str], *, cwd: Path, env: dict[str, str], step: str) -> tuple[int | None, str, str]:
        """Run one command. ``None`` as the code when it could not be started."""
        name = f"coordinare-qare-{hashlib.sha256(str(self.job_dir).encode()).hexdigest()[:12]}-{step.split()[0]}"
        named = any(NAME_PLACEHOLDER in part for part in argv)
        argv = [part.replace(NAME_PLACEHOLDER, name) for part in argv]
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise _StopError({"outcome": TIMEOUT, "step": step, "reason": "the run took longer than timeout_seconds"})
        try:
            # A process group of its own, so a timeout stops what it started too (QAre
            # boots apps and containers), not only the command itself.
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=cwd, env=env, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            return None, "", f"{argv[0]} could not be started: {exc.strerror or exc}"
        try:
            out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=remaining)
        except TimeoutError as exc:
            _kill_group(proc.pid)
            await proc.wait()
            if named:
                # Killing the docker client leaves its container running; remove it.
                await self._remove_container(name, env)
            _append(self.log, f"{step} (timed out)", argv, "")
            raise _StopError({"outcome": TIMEOUT, "step": step, "reason": "the run took longer than timeout_seconds"}) from exc
        out, err = out_b.decode(errors="replace"), err_b.decode(errors="replace")
        _append(self.log, f"{step} -> {proc.returncode}", argv, f"{_tail(out)}\n{_tail(err)}")
        return proc.returncode, out, err


    async def _remove_container(self, name: str, env: dict[str, str]) -> None:
        with contextlib.suppress(OSError, TimeoutError):
            proc = await asyncio.create_subprocess_exec(
                "docker", "rm", "-f", name, env=env, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=60)


def _change_diff(request: dict[str, Any], *, base: str, head: str, token: str, deadline: float) -> str:
    """The change as GitHub compares base and head (a shallow checkout has no merge base)."""
    if not base or base == head:
        return ""
    url = f"{request['api_base'].rstrip('/')}/repos/{request['repository']}/compare/{base}...{head}"
    headers = {"Accept": "application/vnd.github.diff"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        response = httpx.get(url, headers=headers, timeout=max(5.0, min(120.0, deadline - time.monotonic())))
    except httpx.HTTPError as exc:
        raise _StopError({"outcome": ERROR, "step": "diff", "reason": f"GitHub compare: {type(exc).__name__}"}) from exc
    if response.status_code != HTTPStatus.OK:
        raise _StopError({
            "outcome": ERROR, "step": "diff", "http_status": response.status_code,
            "reason": f"GitHub compare answered {response.status_code}",
        })
    return response.text


def qare_env(env: dict[str, str], passthrough: list[str]) -> dict[str, str]:
    """What QAre may see of ``env``: the base names and the operator's passthrough names."""
    return _qare_env(env, passthrough)


def _qare_env(env: dict[str, str], passthrough: list[str]) -> dict[str, str]:
    names = [*_BASE_ENV, *passthrough]
    return {name: env[name] for name in names if name in env and name != CREDENTIAL_ENV}


def _append(log: Path, heading: str, argv: list[str], text: str) -> None:
    shown = " ".join(argv[:_SHOWN_ARGS]) + (" ..." if len(argv) > _SHOWN_ARGS else "")
    with log.open("a", encoding="utf-8") as handle:
        handle.write(f"== {heading}: {shown}\n{text}\n")


def _kill_group(pid: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGKILL)


def _tail(text: str) -> str:
    text = (text or "").strip()
    return text[-_TAIL:]


def _write_atomic(path: Path, text: str) -> None:
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(text, encoding="utf-8")
    os.replace(temp, path)


if __name__ == "__main__":
    raise SystemExit(main())

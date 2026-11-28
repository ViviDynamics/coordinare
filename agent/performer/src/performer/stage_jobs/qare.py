"""404/423: one QAre run, inside an ephemeral performer container.

Everything the run needs is in ``<job dir>/request.json``, which holds no secret; the
GitHub token arrives as an argument (``__main__`` took it out of the environment). When
the run ends, one file says how: ``<job dir>/status.json``, written atomically and last.

Steps, each of which can end the run:

1. ``qare --version`` must print the pinned version.
2. The pull request's head and base are checked out by the workspace layer, in a
   directory of the container's own (never the mounted job directory).
3. The QA profile is found: the base revision's when it has one (the profile that runs is
   then one the pull request cannot change, as in QAre's own pipeline), else the head's.
4. The work item's criteria and the change's diff are written for the planner.
5. ``qare plan --criteria`` maps each criterion to checks. A job whose criteria carry no
   checks runs nothing in QAre (each is ``unverified``), so the plan comes first.
6. ``qare run --plan`` runs them with posting off and writes ``evidence/result.json``.

450: the token is needed for steps 2 and 4 only (the checkout and GitHub's compare diff),
which run before QAre runs anything of the pull request's. QAre then runs that code as root
in this container, where it can read any process's memory. So the entry
(:mod:`performer.stage_jobs.__main__`) hands steps 5 and 6 to a fresh process of its own
(``execve``), started without the token: the process that waits on QAre never held it.

Nothing here reads the result: coordinare's provider does, so the rules for what a result
means live in one place.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import time
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from performer.stage_checkout import checkout_revisions, diff_revisions
from performer.stage_jobs import CREDENTIAL_ENV, hand_back, scratch_dir, write_atomic
from performer.workspace import WorkspaceSetupError

if TYPE_CHECKING:
    from collections.abc import Callable

REQUEST_FILE = "request.json"
STATUS_FILE = "status.json"
LOG_FILE = "runner.log"
PLAN_FILE = "plan.json"
CRITERIA_FILE = "criteria.json"
DIFF_FILE = "change.diff"
EVIDENCE_DIR = "evidence"

#: Outcomes the run reports. Coordinare's provider maps each onto the stage contract.
RAN = "ran"  # qare run ended; its exit code and result.json say how
UNAVAILABLE = "unavailable"  # qare could not be started at all
MISCONFIGURED = "misconfigured"  # the pinned version is not the one installed
NO_PROFILE = "no_profile"  # neither side of the change has a QA profile
NO_CRITERIA = "no_criteria"  # the work item states no acceptance criteria
ERROR = "error"  # a step before the verdict failed; nothing was decided
TIMEOUT = "timeout"  # the run took longer than timeout_seconds

#: What QAre and the nare it runs may see of the job's environment. Everything else
#: stays out; the operator adds names with ``env_passthrough``.
BASE_ENV = (
    "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TMPDIR", "TZ",
    "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR",
    "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME",
)
#: 442: never handed to QAre, even when named. Either moves where QAre's Playwright looks
#: for browsers, away from the revisions the performer image links into the default cache.
BROWSER_CACHE_ENV = frozenset({"XDG_CACHE_HOME", "PLAYWRIGHT_BROWSERS_PATH"})
_TAIL = 1500
#: The most of a locally computed diff handed to the planner.
_MAX_DIFF_CHARS = 2_000_000
#: GitHub's answers for a compare diff past its size limits. Any other failure is retried.
_DIFF_TOO_LARGE = frozenset({HTTPStatus.NOT_ACCEPTABLE, HTTPStatus.UNPROCESSABLE_ENTITY})
#: Arguments of a command the log shows; the rest are paths it does not need.
_SHOWN_ARGS = 3


def run_job(
    job_dir: Path,
    *,
    token: str = "",
    env: dict[str, str] | None = None,
    restart: Callable[[dict[str, Any]], object] | None = None,
) -> dict[str, Any]:
    """Run the job in ``job_dir`` to its end and write ``status.json``. Returns the status.

    450: with ``restart``, once the steps that need the token are done the rest of the run
    is handed over: ``restart`` is called with what a fresh process needs to go on (no
    secret in it) and replaces this one (``execve``), which then calls :func:`resume_job`.
    Should ``restart`` return (a test that resumes in the same process), the status the
    resumed run wrote is returned.
    """
    return _run_job(job_dir, token=token, env=env, restart=restart, resume=None)


def resume_job(job_dir: Path, state: dict[str, Any], *, env: dict[str, str] | None = None) -> dict[str, Any]:
    """450: the rest of a run :func:`run_job` handed over, in a process that never held the token."""
    return _run_job(job_dir, token="", env=env, restart=None, resume=state)


def _run_job(
    job_dir: Path,
    *,
    token: str,
    env: dict[str, str] | None,
    restart: Callable[[dict[str, Any]], object] | None,
    resume: dict[str, Any] | None,
) -> dict[str, Any]:
    source = dict(os.environ if env is None else env)
    source.pop(CREDENTIAL_ENV, None)
    job_dir = job_dir.expanduser().absolute()
    started = time.monotonic()
    run: _Run | None = None
    try:
        request = json.loads((job_dir / REQUEST_FILE).read_text())
        if resume is not None:
            started = float(resume["started"])
            run = _Run(job_dir, request, token="", env=source, started=started)
            run.adopt(Path(resume["scratch"]))
            status = asyncio.run(run.finish())
        elif restart is not None:
            run = _Run(job_dir, request, token=token, env=source, started=started)
            asyncio.run(run.prepare())
            (job_dir / STATUS_FILE).unlink(missing_ok=True)  # an earlier attempt's is not this run's
            # execve: nothing below runs, and the process it starts removes the checkouts.
            restart({"scratch": str(run.scratch), "started": started})
            resumed: dict[str, Any] = json.loads((job_dir / STATUS_FILE).read_text())
            return resumed
        else:
            run = _Run(job_dir, request, token=token, env=source, started=started)
            status = asyncio.run(run.run())
    except _StopError as stop:
        status = stop.status
    except Exception as exc:  # a runner bug must still end the run with a status
        status = {"outcome": ERROR, "step": "runner", "reason": f"{type(exc).__name__}: {exc}"}
    finally:
        # The checkouts go once the run ends: the evidence is all that is read later.
        if run is not None:
            shutil.rmtree(run.scratch, ignore_errors=True)
    status["finished_at"] = time.time()
    status["elapsed_seconds"] = round(time.monotonic() - started, 1)
    write_atomic(job_dir / STATUS_FILE, json.dumps(status, indent=2))
    hand_back(job_dir)
    return status


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
        self.qare_env = qare_env(env, request.get("env_passthrough") or [])
        self.log = job_dir / LOG_FILE
        self.head = str(request["head_sha"])
        self.base = str(request.get("base_sha") or "")
        #: The checkouts' own directory, in the container, never the mounted job directory.
        self.scratch = scratch_dir()
        self.repo = self.scratch / "repo"
        self.base_dir = self.scratch / "base"
        #: Whether there is a base revision of its own to check (and so a base checkout).
        self.two_sided = bool(self.base) and self.base != self.head

    def adopt(self, scratch: Path) -> None:
        """450: go on with the checkouts of the process this run was handed over from."""
        with contextlib.suppress(OSError):
            self.scratch.rmdir()  # its own, still empty
        self.scratch = scratch
        self.repo = scratch / "repo"
        self.base_dir = scratch / "base"

    async def run(self) -> dict[str, Any]:
        await self.prepare()
        return await self.finish()

    async def prepare(self) -> None:
        """Steps 1 to 4: everything that needs the token, before anything of the change runs."""
        await self._check_version()
        await self._checkout()
        self._profile()
        criteria = [{"id": c["id"], "text": c["text"]} for c in self.request.get("criteria") or []]
        if not criteria:
            raise _StopError({
                "outcome": NO_CRITERIA, "step": "criteria",
                "reason": "the work item states no acceptance criteria, so QAre has nothing to prove",
            })
        write_atomic(self.job_dir / CRITERIA_FILE, json.dumps(criteria, indent=2))
        write_atomic(self.job_dir / DIFF_FILE, await self._diff())
        self.token = ""

    async def finish(self) -> dict[str, Any]:
        """Steps 5 and 6: QAre plans and runs, against what :meth:`prepare` wrote."""
        if not self.repo.is_dir():
            raise _StopError({"outcome": ERROR, "step": "checkout", "reason": "the checkout is gone"})
        profile = self._profile()
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
        web = str(self.request["web_base"]).rstrip("/")
        url = f"{web}/{self.request['repository']}.git"
        try:
            await asyncio.wait_for(checkout_revisions(
                url, self.repo, self.head, token=self.token,
                base=(self.base, self.base_dir) if self.two_sided else None,
            ), timeout=self._remaining("checkout"))
        except WorkspaceSetupError as exc:
            _append(self.log, "checkout (failed)", ["git"], str(exc))
            raise _StopError({"outcome": ERROR, "step": "checkout", "reason": _tail(str(exc))}) from exc
        except TimeoutError as exc:
            raise _StopError({"outcome": TIMEOUT, "step": "checkout", "reason": "the run took longer than timeout_seconds"}) from exc
        _append(self.log, "checkout -> 0", ["git", "fetch", self.head[:12]], "")

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
            try:
                out = await diff_revisions(self.repo, self.base, self.head)
            except WorkspaceSetupError as exc:
                raise _StopError({"outcome": ERROR, "step": "diff", "reason": _tail(str(exc))}) from refused
            _append(self.log, f"diff (fallback: {refused.status.get('reason')})", ["git", "diff"], "")
            if len(out) > _MAX_DIFF_CHARS:
                out = out[: out.rfind("\n", 0, _MAX_DIFF_CHARS) + 1]
            return out

    def _remaining(self, step: str) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise _StopError({"outcome": TIMEOUT, "step": step, "reason": "the run took longer than timeout_seconds"})
        return remaining

    async def _call(self, argv: list[str], *, cwd: Path, env: dict[str, str], step: str) -> tuple[int | None, str, str]:
        """Run one command. ``None`` as the code when it could not be started."""
        remaining = self._remaining(step)
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
            _append(self.log, f"{step} (timed out)", argv, "")
            raise _StopError({"outcome": TIMEOUT, "step": step, "reason": "the run took longer than timeout_seconds"}) from exc
        out, err = out_b.decode(errors="replace"), err_b.decode(errors="replace")
        _append(self.log, f"{step} -> {proc.returncode}", argv, f"{_tail(out)}\n{_tail(err)}")
        return proc.returncode, out, err


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
    names = [*BASE_ENV, *passthrough]
    return {
        name: env[name]
        for name in names
        if name in env and name != CREDENTIAL_ENV and name not in BROWSER_CACHE_ENV
    }


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

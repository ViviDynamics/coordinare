"""403: Progettare as the architecting stage's provider.

Progettare (ViviDynamics/progettare) plans one GitHub issue: a bounded survey, one
blueprint, sizing, and three reader briefs, written as version-stamped artifacts in a run
directory. It commits nothing and posts nothing. Its front doors are the CLI, an MCP stdio
server and a GitHub App; coordinare uses the released CLI at a pinned version:

    progettare blueprint --issue owner/repo#N --repo <checkout> --config <yaml> --runs-dir <dir>

Progettare has no job API and no request ids (ViviDynamics/progettare#69), so this
provider gives every request its own directory under ``runs_dir``, named by the request
id, and runs the CLI there in a detached runner (``progettare_runner``) that holds an
``flock`` for the life of the run. Looking a request up is reading its directory:

- the lock is held: the run is in flight;
- no ``request.json``: never started;
- ``run.json`` with ``complete``: the plan, read from ``intake.json``, ``size.json`` and
  the per-reader briefs under ``briefs/`` after checking each is contract version 1;
- ``run.json`` with ``blocked``: the questions for a person;
- ``run.json`` with ``failed``, or no ``run.json`` at all (a clone that failed, a run that
  timed out or was killed by a restart of the host): ended without a result. Progettare
  posts nothing, so every such attempt is safe to retry under the next request id.

A record whose contract version is not the one this module speaks is refused for a person,
never interpreted. This mirrors ``progettare.adapter``'s check; the adapter is not
imported, because Progettare runs as a CLI and is not installed in coordinare.
"""
from __future__ import annotations

import asyncio
import contextlib
import errno
import hashlib
import json
import os
import re
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from coordinare.services.progettare_runner import without_coordinare_python, write_json
from coordinare.services.stage_contract import (
    JobState,
    StageJob,
    StageOutcome,
    StageVerdict,
    WorkItemRequest,
    work_item_digest,
)
from coordinare.transport.base import TransportError
from coordinare.transport.http_transport import PerformerAuthError

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import ModuleType

    from coordinare.services.stage_contract import AnyStageRequest
    from coordinare.stage_provider_config import ProgettareProviderConfig

logger = structlog.get_logger(__name__)

# coordinare is imported on hosts without fcntl (Windows). A Progettare run cannot work
# there, and says so; nothing else may break.
_FCNTL: ModuleType | None
try:
    import fcntl
except ImportError:
    _FCNTL = None
else:
    _FCNTL = fcntl

#: The Progettare artifact contract version this module speaks (``progettare.contract``).
ARTIFACT_VERSION = 1
_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,127}$")
_READERS = ("implementer", "qa", "documenter")
#: Environment every run gets, when the daemon has it. Anything else is named in pass_env.
_BASE_ENV = (
    "PATH", "HOME", "USER", "LANG", "LC_ALL", "TMPDIR",
    # The runner is ``python -m coordinare...``: it imports coordinare as the daemon does.
    "PYTHONPATH", "VIRTUAL_ENV",
    # git, gh and the model calls go out the way the daemon's own requests do.
    "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "GIT_SSL_CAINFO",
)
_VERSION_TIMEOUT_SECONDS = 15
_INTAKE_HINT = (
    "Progettare reads the acceptance criteria from the issue body: answer by editing the "
    "issue's acceptance section, then reply on the card"
)


class ContractError(ValueError):
    """A run record this module does not speak."""


class ProgettareProvider:
    """A :class:`~coordinare.services.stage_contract.StageProvider` for Progettare."""

    def __init__(
        self,
        name: str,
        config: ProgettareProviderConfig,
        *,
        clone_url: Callable[[WorkItemRequest], str] | None = None,
        python: str = sys.executable,
        git_base_url: str = "https://github.com",
    ) -> None:
        self.name = name
        self._config = config
        base = git_base_url.rstrip("/")
        # The clone goes to the git host coordinare is configured with, never to a host a
        # card names: the issue's host is only checked against it.
        self._clone_url = clone_url or (lambda request: f"{base}/{request.repository}.git")
        self._python = python
        #: Runners this process started, kept referenced until they exit. Only a
        #: convenience: whether a run is in flight is always read from its lock.
        self._children: dict[str, asyncio.subprocess.Process] = {}

    @property
    def config(self) -> ProgettareProviderConfig:
        return self._config

    # -- the provider protocol ------------------------------------------------------

    async def submit(self, request: AnyStageRequest, request_id: str) -> StageJob:
        if not isinstance(request, WorkItemRequest):
            msg = "Progettare plans work items, not pull requests"
            raise TransportError(msg)
        found = self._inspect_or_retry(_checked_ref(request_id))
        if found is not None:
            return found
        token = self._token()
        await self._check_version()
        try:
            return await self._start(request, request_id, token)
        except OSError as exc:
            msg = f"Progettare: cannot start a run under {self._config.runs_dir}: {exc}"
            raise TransportError(msg) from exc

    async def poll(self, ref: str) -> StageJob:
        found = self._inspect_or_retry(_checked_ref(ref))
        if found is None:
            msg = f"Progettare has no run for request {ref} under {self._config.runs_dir}"
            raise TransportError(msg)
        return found

    async def health(self) -> tuple[bool, str]:
        """Installed at the pinned version, with its token, runs directory and config."""
        executable = shutil.which(self._config.command)
        if executable is None:
            return False, f"{self._config.command} is not installed or not on PATH"
        found = await self._installed_version()
        for problem in (
            self._version_problem(executable, found),
            _tools_problem(),
            self._environment_problem(),
            self._config_problem(),
            self._runs_problem(),
        ):
            if problem:
                return False, problem
        return True, f"progettare {found} at {executable}; runs in {self._config.runs_dir}"

    # -- starting a run -------------------------------------------------------------

    def _directory(self, ref: str) -> Path:
        return Path(self._config.runs_dir) / ref

    def _token(self) -> str:
        token = os.environ.get(self._config.gh_token_env, "").strip()
        if not token:
            msg = (
                f"stage provider {self.name!r}: environment variable {self._config.gh_token_env} "
                "is not set; it must hold a GitHub token that can read the issue and clone the "
                "repository"
            )
            raise PerformerAuthError(msg)
        return token

    async def _check_version(self) -> None:
        """Checked before every run, so an upgrade under a running daemon is noticed.

        A version that differs from the pin, or a CLI that is not there, is a configuration
        error and blocks. A CLI that does not answer in time is retried.
        """
        found = await self._installed_version()
        if found is None:
            msg = f"stage provider {self.name!r}: could not read the version of {self._config.command} in time"
            raise TransportError(msg)
        if found != self._config.version:
            msg = (
                f"stage provider {self.name!r}: {self._config.command} --version reports "
                f"{found or 'nothing (it could not be started; is it installed?)'}, but version "
                f"{self._config.version} is pinned; install "
                "the pinned release or change stage_providers."
                f"{self.name}.version"
            )
            raise PerformerAuthError(msg)

    async def _installed_version(self) -> str | None:
        """What ``<command> --version`` prints; empty when it cannot be run, None when it
        did not answer in time."""
        try:
            # The environment the run gets, so the check sees what the run will see.
            process = await asyncio.create_subprocess_exec(
                self._config.command, "--version",
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, env=without_coordinare_python(self._child_env(None, "github.com")),
            )
        except OSError:
            return ""
        try:
            out, err = await asyncio.wait_for(process.communicate(), _VERSION_TIMEOUT_SECONDS)
        except TimeoutError:
            process.kill()
            await process.wait()
            return None
        lines = out.decode("utf-8", "replace").strip().splitlines()
        if lines:
            return lines[-1].strip()
        why = err.decode("utf-8", "replace").strip().splitlines()[-1:] or [""]
        return f"nothing (exit {process.returncode}{': ' + why[0][:200] if why[0] else ''})"

    async def _start(self, request: WorkItemRequest, ref: str, token: str) -> StageJob:
        directory = self._directory(ref)
        try:
            directory.mkdir(parents=True, exist_ok=True)
            fd = os.open(directory / "lock", os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            msg = f"Progettare: cannot use {directory}: {exc}"
            raise TransportError(msg) from exc
        try:
            if not _try_lock(fd):
                return StageJob(ref=ref, state=JobState.PENDING, detail="running")
            for stale in ("runner.json", "stdout.txt", "stderr.txt"):
                (directory / stale).unlink(missing_ok=True)
            shutil.rmtree(directory / "runs", ignore_errors=True)
            write_json(directory / "request.json", {
                "request_id": ref,
                "issue": request.ref,
                "host": request.host,
                "clone_url": self._clone_url(request),
                "pin": request.pin,
                "command": self._config.command,
                "version": self._config.version,
                "config_path": self._config.config_path,
                "timeout_seconds": self._config.timeout_seconds,
                "created_at": datetime.now(UTC).isoformat(),
            })
            try:
                with (directory / "runner.log").open("wb") as log:
                    child = await asyncio.create_subprocess_exec(
                        self._python, "-m", "coordinare.services.progettare_runner", str(directory), str(fd),
                        stdin=asyncio.subprocess.DEVNULL, stdout=log, stderr=log, cwd=directory,
                        env=self._child_env(token, request.host), pass_fds=(fd,), start_new_session=True,
                    )
            except OSError as exc:
                (directory / "request.json").unlink(missing_ok=True)
                msg = f"Progettare: could not start the run: {exc}"
                raise TransportError(msg) from exc
            self._children[ref] = child
        finally:
            os.close(fd)
        logger.info("progettare.run_started", provider=self.name, request=ref, issue=request.ref)
        return StageJob(ref=ref, state=JobState.PENDING, detail="started")

    def _child_env(self, token: str | None, host: str) -> dict[str, str]:
        env = {k: os.environ[k] for k in (*_BASE_ENV, *self._config.pass_env) if k in os.environ}
        if token is not None:
            env["GH_TOKEN"] = token
        if host != "github.com":
            env["GH_HOST"] = host
        return env

    # -- reading a run --------------------------------------------------------------

    def _inspect_or_retry(self, ref: str) -> StageJob | None:
        """Read a request's directory; a filesystem error is a retryable system error."""
        try:
            return self._inspect(ref)
        except OSError as exc:
            msg = f"Progettare: cannot read request {ref} under {self._config.runs_dir}: {exc}"
            raise TransportError(msg) from exc

    def _inspect(self, ref: str) -> StageJob | None:
        child = self._children.get(ref)
        if child is not None and child.returncode is not None:
            del self._children[ref]  # reaped; the lock below says whether the run is over
        directory = self._directory(ref)
        if not directory.is_dir():
            return None
        if _locked(directory / "lock"):
            return StageJob(ref=ref, state=JobState.PENDING, detail="running")
        try:
            request = _read_json(directory / "request.json")
        except FileNotFoundError:
            return None  # a start that never got as far as recording the request
        except ContractError as exc:
            return StageJob(ref=ref, state=JobState.NEEDS_HUMAN, detail="unreadable", reason=str(exc))
        return self._ended(ref, directory, request)

    def _ended(self, ref: str, directory: Path, request: dict[str, Any]) -> StageJob:
        runner = _read_optional(directory / "runner.json")
        phase = str(runner.get("phase") or "")
        detail = str(runner.get("detail") or "").strip()
        if phase == "config":
            return StageJob(ref=ref, state=JobState.NEEDS_HUMAN, detail="config", reason=detail)
        if phase in ("clone", "timeout"):
            return _retry(ref, phase, f"{phase} failed: {detail}" if phase == "clone" else detail)
        run_dir = _run_dir(directory / "runs")
        if run_dir is None:
            log = "" if detail else _log_tail(directory / "runner.log")
            why = detail or (f"the runner's log ends: {log}" if log else (
                "the run stopped before it recorded anything (was the host restarted?)"
            ))
            return _retry(ref, phase or "interrupted", f"progettare recorded no run: {why}")
        try:
            manifest = _artifact(run_dir / "run.json", "run")
            status = str(manifest.get("status") or "")
            if status == "failed":
                return _retry(ref, "failed", (
                    f"progettare failed at {manifest.get('failing_stage')}: "
                    f"{manifest.get('failing_reason')}"
                ))
            intake = _artifact(run_dir / "intake.json", "intake")
            outcome = self._outcome(request, run_dir, manifest, intake, status)
        except (KeyError, TypeError, AttributeError, ValueError) as exc:
            # A record in a shape this module does not expect is refused for a person,
            # never raised into the monitor. ContractError is a ValueError.
            reason = f"Progettare's record in {run_dir} is refused: {exc}"
            return StageJob(ref=ref, state=JobState.NEEDS_HUMAN, detail="refused", reason=reason)
        return StageJob(ref=ref, state=JobState.DONE, outcome=outcome, detail=status)

    def _outcome(
        self, request: dict[str, Any], run_dir: Path, manifest: dict[str, Any],
        intake: dict[str, Any], status: str,
    ) -> StageOutcome:
        raw_issue = intake.get("issue")
        issue: dict[str, Any] = raw_issue if isinstance(raw_issue, dict) else {}
        digest = work_item_digest(str(issue.get("title") or ""), str(issue.get("body") or ""))
        pin = str(request.get("pin") or "")
        if status == "blocked":
            stage = str(manifest.get("failing_stage") or "")
            reason = str(manifest.get("failing_reason") or "").strip()
            questions = [str(q) for q in intake.get("blocked_questions") or [] if str(q).strip()]
            if stage == "intake" and questions:
                questions.append(_INTAKE_HINT)
            return StageOutcome(
                verdict=StageVerdict.NEEDS_HUMAN, reviewed_head=pin, input_digest=digest,
                summary=f"Progettare blocked at {stage or 'an unnamed stage'}",
                questions=questions or [reason or "Progettare blocked without a reason"],
                record_ref=str(run_dir),
            )
        if status != "complete":
            msg = f"run.json names status {status!r}, which is not complete, blocked or failed"
            raise ContractError(msg)
        blueprint = blueprint_from_run(run_dir, intake, manifest, provider=self.name)
        # What the plan answers, so a later dispatch can tell whether it still does.
        blueprint["source"]["pin"] = pin
        return StageOutcome(
            verdict=StageVerdict.PASSED, reviewed_head=pin, input_digest=digest,
            blueprint=blueprint, record_ref=str(run_dir),
            summary=f"Progettare planned {len(blueprint['milestones'])} milestone(s)",
        )

    # -- health ---------------------------------------------------------------------

    def _version_problem(self, executable: str, found: str | None) -> str | None:
        if found == self._config.version:
            return None
        if found is None:
            return f"{executable} --version did not answer within {_VERSION_TIMEOUT_SECONDS}s"
        return f"{executable} --version reports {found or 'nothing'}, but {self._config.version} is pinned"

    def _environment_problem(self) -> str | None:
        names = (self._config.gh_token_env, *self._config.pass_env)
        missing = [n for n in names if not os.environ.get(n, "").strip()]
        return f"environment variable(s) not set: {', '.join(missing)}" if missing else None

    def _config_problem(self) -> str | None:
        path = self._config.config_path
        return f"config_path {path} does not exist" if path and not Path(path).is_file() else None

    def _runs_problem(self) -> str | None:
        runs = Path(self._config.runs_dir)
        try:
            runs.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return f"runs_dir {runs} cannot be created: {exc.strerror or exc}"
        return None if os.access(runs, os.W_OK) else f"runs_dir {runs} is not writable"


def _tools_problem() -> str | None:
    missing = [tool for tool in ("git", "gh") if shutil.which(tool) is None]
    return f"{', '.join(missing)} not on PATH; Progettare runs need them" if missing else None


# -- the blueprint ----------------------------------------------------------------------


def blueprint_from_run(
    run_dir: Path, intake: dict[str, Any], manifest: dict[str, Any], *, provider: str = "progettare",
) -> dict[str, Any]:
    """The spec-165 blueprint coordinare's readers take their briefs from.

    Built from Progettare's own slices, so each reader gets what Progettare cut for it:
    the implementer's milestones, QA's testable criteria, the documenter's topics (only
    when Progettare's sizing turned the documenter on), and the size.
    """
    size = _artifact(run_dir / "size.json", "size")
    briefs = {
        reader: _artifact(path, f"{reader}_brief")
        for reader in _READERS
        if (path := run_dir / "briefs" / f"{reader}.json").is_file()
    }
    implementer = _brief(briefs, "implementer")
    milestones = implementer.get("milestones")
    if not isinstance(milestones, list) or not milestones:
        msg = "the implementer brief has no milestones"
        raise ContractError(msg)
    plan_milestones = []
    for position, milestone in enumerate(milestones):
        title = milestone.get("title") if isinstance(milestone, dict) else None
        changes = milestone.get("changes") if isinstance(milestone, dict) else None
        if not isinstance(title, str) or not title.strip() or not isinstance(changes, list) or not changes:
            msg = f"implementer milestone {position} has no title or no changes"
            raise ContractError(msg)
        plan_milestones.append({
            "goal": title.strip(),
            "scope": [str(c) for c in changes],
            "done_when": "every change in scope is in place",
        })
    criteria = _strings(_brief(briefs, "qa").get("testable_criteria"), "qa testable_criteria")
    topics = _strings(_brief(briefs, "documenter").get("topics"), "documenter topics") if "documenter" in briefs else []
    classification = size.get("classification")
    if classification not in ("single_turn", "milestone_loop"):
        msg = f"size.json names classification {classification!r}"
        raise ContractError(msg)
    raw_issue = intake.get("issue")
    issue: dict[str, Any] = raw_issue if isinstance(raw_issue, dict) else {}
    blueprint: dict[str, Any] = {
        "summary": str(issue.get("title") or "").strip(),
        "milestones": plan_milestones,
        # Progettare's implementer slice carries milestones only; the rest of its plan
        # stays in blueprint.json in the run directory, for people to read.
        "modules": [],
        "data_model": {"changes": []},
        "interfaces": [],
        "risks": [],
        "criteria": [{"surface": "", "action": c, "expected": "", "kind": "functional"} for c in criteria],
        # Progettare names topics, not places: the documenter places a topic with no
        # location under the repository's docs root, which only it can resolve.
        "docs": [{"topic": t[:120], "location": "", "say": t} for t in topics],
    }
    digest = json.dumps(blueprint, sort_keys=True, separators=(",", ":"))
    blueprint["size"] = "small" if classification == "single_turn" else "large"
    blueprint["blueprint_hash"] = hashlib.sha256(digest.encode()).hexdigest()[:16]
    blueprint["created_at"] = datetime.now(UTC).isoformat()
    blueprint["source"] = {
        "provider": provider,
        "progettare": str(manifest.get("progettare") or ""),
        "artifact_version": ARTIFACT_VERSION,
        "run_dir": str(run_dir),
    }
    return blueprint


def _brief(briefs: dict[str, dict[str, Any]], reader: str) -> dict[str, Any]:
    record = briefs.get(reader)
    brief = record.get("brief") if isinstance(record, dict) else None
    if not isinstance(brief, dict):
        msg = f"the run has no {reader} brief"
        raise ContractError(msg)
    return brief


def _strings(value: Any, what: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        msg = f"{what} is not a list of strings"
        raise ContractError(msg)
    return [v.strip() for v in value if v.strip()]


# -- files ------------------------------------------------------------------------------


def _read_json(path: Path) -> dict[str, Any]:
    # Another OSError (a missing file included) propagates: reading again may succeed, so
    # the caller retries. A file that is not readable text, or not a JSON object, is refused.
    try:
        text = path.read_text(encoding="utf-8")
    except (IsADirectoryError, PermissionError, UnicodeDecodeError) as exc:
        msg = f"{path.name} cannot be read: {exc}"
        raise ContractError(msg) from exc
    try:
        record = json.loads(text)
    except ValueError as exc:
        msg = f"{path.name} cannot be read: {exc}"
        raise ContractError(msg) from exc
    if not isinstance(record, dict):
        msg = f"{path.name} is not a JSON object"
        raise ContractError(msg)
    return record


def _read_optional(path: Path) -> dict[str, Any]:
    try:
        return _read_json(path)
    except (FileNotFoundError, ContractError):
        return {}


def _artifact(path: Path, kind: str) -> dict[str, Any]:
    """A run artifact, refused unless it is the kind expected at the contract version spoken."""
    try:
        record = _read_json(path)
    except FileNotFoundError:
        msg = f"{path.name} is missing"
        raise ContractError(msg) from None
    version = record.get("artifact_version")
    if version != ARTIFACT_VERSION:
        msg = (
            f"{path.name} names contract version {version}; coordinare speaks Progettare "
            f"contract version {ARTIFACT_VERSION}"
        )
        raise ContractError(msg)
    if record.get("artifact") != kind:
        msg = f"{path.name} is a {record.get('artifact')!r} artifact, not {kind!r}"
        raise ContractError(msg)
    return record


def _run_dir(runs: Path) -> Path | None:
    """The run Progettare recorded under ``runs``: the latest with a ``run.json``."""
    if not runs.is_dir():
        return None
    recorded = sorted(p for p in runs.iterdir() if (p / "run.json").is_file())
    return recorded[-1] if recorded else None


def _locked(path: Path) -> bool:
    """Whether a runner (or the Progettare it started) still holds the request's lock."""
    try:
        fd = os.open(path, os.O_RDWR)
    except FileNotFoundError:
        return False  # never started; any other error is the caller's retryable error
    try:
        if not _try_lock(fd):
            return True
        _unlock(fd)
        return False
    finally:
        os.close(fd)


def _try_lock(fd: int) -> bool:
    """Take the request's lock without waiting; False when someone holds it."""
    if _FCNTL is None:
        msg = "Progettare runs need POSIX file locks, which this host does not have"
        raise TransportError(msg)
    try:
        _FCNTL.flock(fd, _FCNTL.LOCK_EX | _FCNTL.LOCK_NB)
    except OSError as exc:
        if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
            return False
        raise
    return True


def _unlock(fd: int) -> None:
    if _FCNTL is not None:
        with contextlib.suppress(OSError):
            _FCNTL.flock(fd, _FCNTL.LOCK_UN)


def _log_tail(path: Path) -> str:
    """The end of the runner's own log, which says why a runner died before it reported."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    return text[-500:]


def _retry(ref: str, detail: str, reason: str) -> StageJob:
    return StageJob(ref=ref, state=JobState.RETRYABLE, detail=detail, reason=reason)


def _checked_ref(ref: str) -> str:
    if not _REF.match(ref or ""):
        msg = f"{ref!r} is not a request id this provider issues"
        raise TransportError(msg)
    return ref


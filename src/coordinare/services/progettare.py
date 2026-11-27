"""403: Progettare as the architecting stage's provider.

Progettare (ViviDynamics/progettare) plans one GitHub issue: a bounded survey, one
blueprint, sizing, and three reader briefs, written as version-stamped artifacts in a run
directory. It commits nothing and posts nothing. Its front doors are the CLI, an MCP stdio
server and a GitHub App; coordinare uses the released CLI at a pinned version:

    progettare blueprint --issue owner/repo#N --repo <checkout> --config <yaml> --runs-dir <dir>

Progettare has no job API and no request ids (ViviDynamics/progettare#69), so this
provider gives every request its own directory under ``runs_dir``, named by the request
id, and runs the CLI as a stage job (#423, :mod:`coordinare.services.stage_jobs`): an
ephemeral performer container, with that directory mounted, clones the default branch with
the performer's workspace layer and runs the CLI there. Coordinare's own host never clones.
The container lives exactly as long as the run. Looking a request up is reading its
directory, and asking Docker about its container only while it has not reported:

- no ``request.json``: never started;
- no ``runner.json`` and the container is running: the run is in flight;
- ``run.json`` with ``complete``: the plan, read from ``intake.json``, ``size.json`` and
  the per-reader briefs under ``briefs/`` after checking each is contract version 1;
- ``run.json`` with ``blocked``: the questions for a person;
- ``run.json`` with ``failed``, or no ``run.json`` at all (a clone that failed, a run that
  timed out, a container that was stopped): ended without a result. Progettare
  posts nothing, so every such attempt is safe to retry under the next request id.

A record whose contract version is not the one this module speaks is refused for a person,
never interpreted. This mirrors ``progettare.adapter``'s check; the adapter is not
imported, because Progettare runs as a CLI and is not installed in coordinare.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from coordinare.services import progettare_runner as runner
from coordinare.services.progettare_runner import write_json
from coordinare.services.stage_contract import (
    JobState,
    StageJob,
    StageOutcome,
    StageVerdict,
    WorkItemRequest,
    work_item_digest,
)
from coordinare.services.stage_jobs import (
    NEVER_STARTED,
    PROXY_ENV,
    RunState,
    StageJobLaunch,
    StageJobs,
    UnavailableStageJobs,
    never_started,
)
from coordinare.transport.base import TransportError
from coordinare.transport.http_transport import PerformerAuthError

if TYPE_CHECKING:
    from collections.abc import Callable

    from coordinare.services.stage_contract import AnyStageRequest
    from coordinare.stage_provider_config import ProgettareProviderConfig

logger = structlog.get_logger(__name__)

#: The Progettare artifact contract version this module speaks (``progettare.contract``).
ARTIFACT_VERSION = 1
_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,127}$")
_READERS = ("implementer", "qa", "documenter")
#: A version check starts a container, which may first pull the image.
_VERSION_TIMEOUT_SECONDS = 180
#: Tools a run needs in the performer beside the CLI.
_TOOLS = ("git", "gh")
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
        git_base_url: str = "https://github.com",
        jobs: StageJobs | None = None,
    ) -> None:
        self.name = name
        self._config = config
        base = git_base_url.rstrip("/")
        # The clone goes to the git host coordinare is configured with, never to a host a
        # card names: the issue's host is only checked against it.
        self._clone_url = clone_url or (lambda request: f"{base}/{request.repository}.git")
        #: Where the runs go: a performer container each (#423).
        self._jobs: StageJobs = jobs or UnavailableStageJobs(
            f"stage provider {name!r} has no performer to run Progettare in",
        )

    @property
    def config(self) -> ProgettareProviderConfig:
        return self._config

    # -- the provider protocol ------------------------------------------------------

    async def submit(self, request: AnyStageRequest, request_id: str) -> StageJob:
        if not isinstance(request, WorkItemRequest):
            msg = "Progettare plans work items, not pull requests"
            raise TransportError(msg)
        found = await self._inspect_or_retry(_checked_ref(request_id))
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
        found = await self._inspect_or_retry(_checked_ref(ref))
        if found is None:
            msg = f"Progettare has no run for request {ref} under {self._config.runs_dir}"
            raise TransportError(msg)
        return found

    async def health(self) -> tuple[bool, str]:
        """The performer has the pinned CLI and its tools; the token, the variables it
        passes, the runs directory and the config are in place."""
        try:
            tools, found = await self._probe_performer()
        except TransportError as exc:  # no performer could be run at all
            return False, str(exc)
        for problem in (
            self._version_problem(found),
            tools,
            self._environment_problem(),
            self._config_problem(),
            self._runs_problem(),
        ):
            if problem:
                return False, problem
        return True, f"progettare {found} in the performer; runs in {self._config.runs_dir}"

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

    def _env_names(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((*PROXY_ENV, *self._config.pass_env)))

    async def _check_version(self) -> None:
        """Checked before every run, so an image change under a running daemon is noticed.

        A version that differs from the pin, or a CLI the performer does not have, is a
        configuration error and blocks. A check that does not answer in time is retried.
        """
        code, out, err = await self._jobs.probe(
            [self._config.command, "--version"], env_names=self._env_names(), within=_VERSION_TIMEOUT_SECONDS,
        )
        found = _version_of(code, out, err)
        if found is None:
            msg = f"stage provider {self.name!r}: could not read the version of {self._config.command} in time"
            raise TransportError(msg)
        if not found:
            msg = f"stage provider {self.name!r}: {err}; use a performer image with progettare {self._config.version}"
            raise PerformerAuthError(msg)
        if found != self._config.version:
            msg = (
                f"stage provider {self.name!r}: {self._config.command} --version reports "
                f"{found}, but version {self._config.version} is pinned; use a performer image with "
                "the pinned release or change stage_providers."
                f"{self.name}.version"
            )
            raise PerformerAuthError(msg)

    async def _probe_performer(self) -> tuple[str | None, str | None]:
        """One container for health: which tools are missing, and the CLI's version."""
        script = (
            f'for t in {" ".join(_TOOLS)}; do command -v "$t" >/dev/null 2>&1 || echo "missing $t"; done; '
            'exec "$0" --version'
        )
        code, out, err = await self._jobs.probe(
            ["sh", "-c", script, self._config.command], env_names=self._env_names(),
            within=_VERSION_TIMEOUT_SECONDS,
        )
        missing = [line.split(" ", 1)[1] for line in out.splitlines() if line.startswith("missing ")]
        tools = f"{', '.join(missing)} not in the performer image; Progettare runs need them" if missing else None
        rest = "\n".join(line for line in out.splitlines() if not line.startswith("missing "))
        return tools, _version_of(code, rest, err)

    async def _start(self, request: WorkItemRequest, ref: str, token: str) -> StageJob:
        directory = self._directory(ref)
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            msg = f"Progettare: cannot use {directory}: {exc}"
            raise TransportError(msg) from exc
        if await self._jobs.state(directory) is RunState.RUNNING:
            return StageJob(ref=ref, state=JobState.PENDING, detail="running")
        for stale in runner.STALE_FILES:
            (directory / stale).unlink(missing_ok=True)
        shutil.rmtree(directory / runner.RUNS_DIR, ignore_errors=True)
        config_file = None
        if self._config.config_path:
            shutil.copyfile(self._config.config_path, directory / runner.CONFIG_FILE)
            config_file = runner.CONFIG_FILE
        write_json(directory / runner.REQUEST_FILE, {
            "request_id": ref,
            "issue": request.ref,
            "host": request.host,
            "clone_url": self._clone_url(request),
            "pin": request.pin,
            "command": self._config.command,
            "version": self._config.version,
            "config_path": self._config.config_path,
            "config_file": config_file,
            "pass_env": list(self._config.pass_env),
            "timeout_seconds": self._config.timeout_seconds,
            "created_at": datetime.now(UTC).isoformat(),
        })
        launch = StageJobLaunch(
            kind=runner.KIND, ref=ref, provider=self.name, job_dir=directory, token=token,
            env_names=self._env_names(),
        )
        try:
            await self._jobs.start(launch)
        except BaseException:
            # Nothing ran: the next dispatch finds no request and starts it afresh.
            with contextlib.suppress(Exception):
                await self._jobs.stop(directory)
            (directory / runner.REQUEST_FILE).unlink(missing_ok=True)
            raise
        logger.info("progettare.run_started", provider=self.name, request=ref, issue=request.ref)
        return StageJob(ref=ref, state=JobState.PENDING, detail="started")

    # -- reading a run --------------------------------------------------------------

    async def _inspect_or_retry(self, ref: str) -> StageJob | None:
        """Read a request's directory; a filesystem error is a retryable system error."""
        try:
            return await self._inspect(ref)
        except OSError as exc:
            msg = f"Progettare: cannot read request {ref} under {self._config.runs_dir}: {exc}"
            raise TransportError(msg) from exc

    async def _inspect(self, ref: str) -> StageJob | None:
        directory = self._directory(ref)
        if not directory.is_dir():
            return None
        try:
            request = _read_json(directory / runner.REQUEST_FILE)
        except FileNotFoundError:
            return None  # a start that never got as far as recording the request
        except ContractError as exc:
            return StageJob(ref=ref, state=JobState.NEEDS_HUMAN, detail="unreadable", reason=str(exc))
        # Not reported yet: in flight while its container runs. A Docker that cannot answer
        # is a TransportError, retried, never read as the run being over.
        if not (directory / runner.RESULT_FILE).exists() and await self._jobs.state(directory) is RunState.RUNNING:
            return StageJob(ref=ref, state=JobState.PENDING, detail="running")
        return self._ended(ref, directory, request)

    def _ended(self, ref: str, directory: Path, request: dict[str, Any]) -> StageJob:
        result = _read_optional(directory / runner.RESULT_FILE)
        phase = str(result.get("phase") or "")
        detail = str(result.get("detail") or "").strip()
        if phase == "config":
            return StageJob(ref=ref, state=JobState.NEEDS_HUMAN, detail="config", reason=detail)
        if phase in ("clone", "timeout"):
            return _retry(ref, phase, f"{phase} failed: {detail}" if phase == "clone" else detail)
        run_dir = _run_dir(directory / runner.RUNS_DIR)
        if run_dir is None:
            log = "" if detail else _log_tail(directory / runner.LOG_FILE)
            if detail:
                why = detail
            elif never_started(directory):
                why = NEVER_STARTED
            elif log.endswith("started"):
                why = "the run stopped before it recorded anything (was its performer stopped?)"
            else:
                why = f"the runner's log ends: {log}"
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

    def _version_problem(self, found: str | None) -> str | None:
        command = self._config.command
        if found == self._config.version:
            return None
        if found is None:
            return f"{command} --version did not answer within {_VERSION_TIMEOUT_SECONDS}s in the performer"
        if not found:
            return f"{command} is not installed in the performer image (it could not be started)"
        return f"{command} --version in the performer reports {found}, but {self._config.version} is pinned"

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


def _version_of(code: int | None, out: str, err: str) -> str | None:
    """A version check's answer: the version, empty when the CLI could not be run, None
    when it did not answer in time."""
    if code is None:
        return "" if err else None
    lines = out.strip().splitlines()
    if lines:
        return lines[-1].strip()
    why = err.strip().splitlines()[-1:] or [""]
    return f"nothing (exit {code}{': ' + why[0][:200] if why[0] else ''})"


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


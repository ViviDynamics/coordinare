"""404: QAre as the QA stage's provider.

QAre (ViviDynamics/qare) is a CLI with an orchestrator contract (``docs/orchestrator.md``),
not a service. This provider runs one QAre job per request id in a directory of its own
under ``work_dir``: :mod:`coordinare.services.qare_runner` fetches the pull request's head
and base, plans the work item's criteria with ``qare plan`` and runs them with ``qare run``
with posting off. The job directory is the job's whole state, so a restarted daemon polls
the same job, and submitting a request id again finds it instead of starting another.

Reading the result follows QAre's contract: ``schemaVersion`` is checked before anything
else is read, exit code 4 is never a verdict, and the exit code and the verdict must agree.

| QAre | stage contract |
| --- | --- |
| ``passed`` | pass (the adapter still requires every criterion proven) |
| ``failed`` | changes requested: the failed criteria and their evidence go to the implementer |
| ``blocked``, ``refused``, ``waived`` | needs a person, never a pass |
| exit 4, no result, a crash, a step before the verdict that failed | retryable |
| an unknown ``schemaVersion`` or verdict, a result that contradicts itself | needs a person |

The evidence is posted by coordinare (:mod:`coordinare.services.qare_publish`) once the
adapter has checked the head, never by QAre.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from coordinare.services import qare_publish
from coordinare.services import qare_runner as runner
from coordinare.services.stage_contract import (
    CriterionOutcome,
    JobState,
    StageJob,
    StageOutcome,
    StageRequest,
    StageVerdict,
)
from coordinare.transport.base import TransportError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import httpx

    from coordinare.stage_provider_config import QAreProviderConfig

logger = structlog.get_logger(__name__)

#: The only result.json version this coordinare reads (QAre ``docs/schemas.md``).
RESULT_SCHEMA_VERSION = "1"
#: ``qare run`` exits 4 when it reached no verdict at all.
NO_VERDICT_EXIT = 4
_EXIT_VERDICT = {0: "passed", 1: "failed", 2: "blocked", 3: "refused", 5: "waived"}
_VERDICTS = {
    "passed": StageVerdict.PASSED,
    "failed": StageVerdict.CHANGES_REQUESTED,
    "blocked": StageVerdict.NEEDS_HUMAN,
    "refused": StageVerdict.NEEDS_HUMAN,
    "waived": StageVerdict.NEEDS_HUMAN,
}
_OUTCOMES = frozenset({"proven", "failed", "unverified"})
_REF = re.compile(r"^[A-Za-z0-9._-]{1,200}$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
#: How long a job directory may exist without a launch record before it counts as dead.
_LAUNCH_GRACE_SECONDS = 120.0


class GitHubAccess:
    """How the provider reaches GitHub for one repository: its token and its URLs."""

    def __init__(
        self,
        token: Callable[[str], Awaitable[str]],
        api_base: Callable[[str], str],
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._token = token
        self._api_base = api_base
        #: Replaces the network in tests.
        self.transport = transport

    async def token(self, repository: str) -> str:
        return await self._token(repository)

    def api_base(self, repository: str) -> str:
        return self._api_base(repository).rstrip("/")

    def web_base(self, repository: str) -> str:
        return web_base_for(self.api_base(repository))


def web_base_for(api_base: str) -> str:
    """``https://api.github.com`` -> ``https://github.com``; GHE ``host/api/v3`` -> ``host``."""
    base = api_base.rstrip("/")
    if base.endswith("/api/v3"):
        return base[: -len("/api/v3")]
    return base.replace("://api.", "://", 1)


class QAreProvider:
    """A :class:`~coordinare.services.stage_contract.StageProvider` for QAre."""

    def __init__(
        self,
        name: str,
        config: QAreProviderConfig,
        *,
        work_dir: Path,
        github: GitHubAccess | None = None,
        runner_argv: list[str] | None = None,
    ) -> None:
        self.name = name
        self._config = config
        self._work_dir = work_dir
        self._github = github
        #: How to start the runner; tests run it in-process instead.
        self._runner_argv = runner_argv or [sys.executable, "-m", "coordinare.services.qare_runner"]
        #: Runner processes this daemon started, so a finished one is reaped, not left a
        #: zombie that looks alive. A runner from before a restart is not our child.
        self._children: dict[str, asyncio.subprocess.Process] = {}

    @property
    def config(self) -> QAreProviderConfig:
        return self._config

    @property
    def work_dir(self) -> Path:
        return self._work_dir

    def job_dir(self, ref: str) -> Path:
        if not _REF.match(ref):
            msg = f"QAre job {ref!r} is not one this provider names"
            raise TransportError(msg)
        return self._work_dir / ref

    async def submit(self, request: StageRequest, request_id: str) -> StageJob:
        job_dir = self.job_dir(request_id)
        try:
            job_dir.parent.mkdir(parents=True, exist_ok=True)
            job_dir.mkdir()
        except FileExistsError:
            return self._read(request_id)
        except OSError as exc:
            msg = f"QAre work_dir {self._work_dir} is not writable: {exc}"
            raise TransportError(msg) from exc
        try:
            token, spec = await self._spec(request, request_id)
            runner._write_atomic(job_dir / runner.REQUEST_FILE, json.dumps(spec, indent=2))
            await self._launch(request_id, job_dir, token)
        except BaseException:
            # Nothing ran: take the directory away, so the next dispatch starts this
            # request id afresh instead of finding a job that never existed.
            _remove_tree(job_dir)
            raise
        logger.info("qare.job_started", job=request_id, repository=request.repository, head_sha=request.head_sha)
        return StageJob(ref=request_id, state=JobState.PENDING, detail="running")

    async def poll(self, ref: str) -> StageJob:
        return self._read(ref)

    async def publish(self, ref: str, outcome: StageOutcome) -> StageOutcome:
        """Post the evidence on the pull request and the work item (QAre's posting is off).

        The adapter calls this only once the tested head is still the pull request's head.
        """
        if self._github is None:
            msg = "QAre provider has no GitHub access to publish its evidence"
            raise TransportError(msg)
        return await qare_publish.publish(
            self.job_dir(ref), self.request_spec(ref), outcome, self._github,
            transport=self._github.transport,
        )

    async def health(self) -> tuple[bool, str]:
        """QAre starts and prints the pinned version, and ``work_dir`` is writable."""
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._config.command, "--version",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            out, err = await asyncio.wait_for(proc.communicate(), timeout=30)
        except (FileNotFoundError, PermissionError) as exc:
            return False, f"{self._config.command[0]} could not be started: {exc.strerror or exc}"
        except TimeoutError:
            return False, "qare --version did not answer within 30 seconds"
        installed = out.decode(errors="replace").strip()
        if proc.returncode != 0 or installed != self._config.version:
            shown = installed or err.decode(errors="replace").strip()[:200]
            return False, f"qare --version printed {shown!r}; the pinned version is {self._config.version}"
        try:
            self._work_dir.mkdir(parents=True, exist_ok=True)
            probe = self._work_dir / ".coordinare-write-probe"
            probe.write_text("ok")
            probe.unlink()
        except OSError as exc:
            return False, f"work_dir {self._work_dir} is not writable: {exc}"
        return True, f"qare {installed}; work_dir {self._work_dir}"

    def request_spec(self, ref: str) -> dict[str, Any]:
        """The request a job was started with (no secret is in it)."""
        try:
            data = json.loads((self.job_dir(ref) / runner.REQUEST_FILE).read_text())
        except (OSError, ValueError) as exc:
            msg = f"QAre job {ref} has no readable request: {exc}"
            raise TransportError(msg) from exc
        if not isinstance(data, dict):
            msg = f"QAre job {ref} has no readable request"
            raise TransportError(msg)
        return data

    # -- internals -----------------------------------------------------------------

    async def _spec(self, request: StageRequest, request_id: str) -> tuple[str, dict[str, Any]]:
        if self._github is None:
            msg = "QAre provider has no GitHub access to fetch the pull request"
            raise TransportError(msg)
        try:
            token = await self._github.token(request.repository)
            api_base = self._github.api_base(request.repository)
        except TransportError:
            raise
        except Exception as exc:
            msg = f"could not get a GitHub token for {request.repository}: {type(exc).__name__}"
            raise TransportError(msg) from exc
        spec = {
            "request_id": request_id,
            "card_id": request.card_id,
            "repository": request.repository,
            "pr_number": request.pr_number,
            "pr_node_id": request.pr_node_id,
            "issue_number": request.issue_number,
            "issue_node_id": request.issue_node_id,
            "head_sha": request.head_sha,
            "base_sha": request.base_sha,
            "base_ref": request.base_ref,
            "criteria": [c.model_dump() for c in request.criteria],
            "api_base": api_base,
            "web_base": web_base_for(api_base),
            "command": list(self._config.command),
            "version": self._config.version,
            "profile": self._config.profile,
            "env_passthrough": list(self._config.env_passthrough),
            "timeout_seconds": self._config.timeout_seconds,
            "created_at": time.time(),
        }
        return token, spec

    async def _launch(self, ref: str, job_dir: Path, token: str) -> None:
        env = dict(os.environ)
        env[runner.CREDENTIAL_ENV] = token
        try:
            with (job_dir / runner.LOG_FILE).open("ab") as log:
                # A session of its own, so the run outlives a daemon restart; asyncio's
                # child watcher reaps it when it ends while this daemon still runs.
                proc = await asyncio.create_subprocess_exec(
                    *self._runner_argv, str(job_dir),
                    stdin=asyncio.subprocess.DEVNULL, stdout=log, stderr=log, env=env,
                    start_new_session=True, close_fds=True,
                )
        except OSError as exc:
            msg = f"could not start the QAre runner: {exc}"
            raise TransportError(msg) from exc
        self._children[ref] = proc
        runner._write_atomic(
            job_dir / runner.LAUNCH_FILE, json.dumps({"pid": proc.pid, "started_at": time.time()}),
        )

    def _read(self, ref: str) -> StageJob:
        job_dir = self.job_dir(ref)
        if not job_dir.is_dir():
            return StageJob(ref=ref, state=JobState.RETRYABLE, detail="missing",
                            reason="the job's directory is gone, so nothing of it remains")
        status = _load_json(job_dir / runner.STATUS_FILE)
        if status is None:
            if self._running(ref, job_dir):
                return StageJob(ref=ref, state=JobState.PENDING, detail="running")
            return StageJob(ref=ref, state=JobState.RETRYABLE, detail="ended",
                            reason="the QAre run ended without saying how (the runner stopped)")
        self._children.pop(ref, None)
        return job_from_status(ref, status, job_dir)

    def _running(self, ref: str, job_dir: Path) -> bool:
        child = self._children.get(ref)
        if child is not None:
            return child.returncode is None
        launch = _load_json(job_dir / runner.LAUNCH_FILE)
        if launch is None:
            # Launched this instant, or the daemon stopped between the two writes.
            age = time.time() - job_dir.stat().st_mtime
            return age < _LAUNCH_GRACE_SECONDS
        pid = launch.get("pid")
        return isinstance(pid, int) and _alive(pid, job_dir)


class _NoOutcomeError(Exception):
    """A run that produced no outcome the stage can take, and what the stage does instead."""

    def __init__(self, state: JobState, detail: str, reason: str) -> None:
        super().__init__(reason)
        self.state = state
        self.detail = detail
        self.reason = reason


class _OutsideContractError(Exception):
    """A result.json that is not what QAre's orchestrator contract promises."""


def job_from_status(ref: str, status: dict[str, Any], job_dir: Path) -> StageJob:
    """Map how a run ended (and, when it ran, its result.json) onto the stage contract."""
    try:
        outcome = _outcome_of(status, job_dir)
    except _NoOutcomeError as ended:
        return StageJob(ref=ref, state=ended.state, detail=ended.detail, reason=ended.reason)
    return StageJob(ref=ref, state=JobState.DONE, detail=outcome.provider_verdict, outcome=outcome)


def _outcome_of(status: dict[str, Any], job_dir: Path) -> StageOutcome:
    outcome = str(status.get("outcome") or "")
    reason = str(status.get("reason") or "").strip()
    step = str(status.get("step") or "")
    if outcome in (runner.NO_PROFILE, runner.NO_CRITERIA, runner.MISCONFIGURED):
        raise _NoOutcomeError(JobState.NEEDS_HUMAN, outcome, reason or outcome)
    if outcome != runner.RAN:
        why = f"{step}: {reason}" if reason else step or outcome or "unknown"
        raise _NoOutcomeError(JobState.RETRYABLE, outcome or "ended",
                              f"QAre run ended before a verdict ({outcome or 'no status'}, {why})")
    code = status.get("exit_code")
    if code == NO_VERDICT_EXIT:
        raise _NoOutcomeError(JobState.RETRYABLE, "no verdict",
                              f"qare run reached no verdict (exit 4){': ' + reason if reason else ''}")
    result_path = job_dir / runner.EVIDENCE_DIR / "result.json"
    try:
        spec = json.loads((job_dir / runner.REQUEST_FILE).read_text())
        text = result_path.read_text()
    except (OSError, ValueError) as exc:
        raise _NoOutcomeError(JobState.RETRYABLE, "no result",
                              f"qare run exited {code} and its result cannot be read ({type(exc).__name__})") from exc
    parsed, problem = parse_result(text, spec, exit_code=code)
    if parsed is None:
        raise _NoOutcomeError(JobState.NEEDS_HUMAN, "unreadable result",
                              f"QAre's result.json is outside its contract: {problem}; inspect {result_path}")
    return parsed


def parse_result(
    text: str, spec: dict[str, Any], *, exit_code: Any,
) -> tuple[StageOutcome | None, str]:
    """A result.json as a stage outcome, or None and what is wrong with it.

    ``schemaVersion`` is checked first, before anything else in the document is read.
    """
    try:
        return _parse(text, spec, exit_code), ""
    except _OutsideContractError as exc:
        return None, str(exc)


def _parse(text: str, spec: dict[str, Any], exit_code: Any) -> StageOutcome:
    try:
        document = json.loads(text)
    except ValueError as exc:
        msg = f"not JSON ({exc})"
        raise _OutsideContractError(msg) from exc
    if not isinstance(document, dict):
        msg = "not a JSON object"
        raise _OutsideContractError(msg)
    version = document.get("schemaVersion")
    if version != RESULT_SCHEMA_VERSION:
        msg = (
            f"unknown schemaVersion {version!r} (this coordinare reads {RESULT_SCHEMA_VERSION!r}; "
            "pin a QAre release that writes it)"
        )
        raise _OutsideContractError(msg)
    verdict_word = document.get("verdict")
    if verdict_word not in _VERDICTS:
        msg = f"unknown verdict {verdict_word!r}"
        raise _OutsideContractError(msg)
    if _EXIT_VERDICT.get(exit_code) != verdict_word:
        msg = f"qare run exited {exit_code!r} but its result says {verdict_word!r}"
        raise _OutsideContractError(msg)
    job = document.get("job")
    if isinstance(job, dict) and job.get("id") not in (None, spec.get("request_id")):
        msg = f"it is the result of job {job.get('id')!r}, not {spec.get('request_id')!r}"
        raise _OutsideContractError(msg)
    head = str(spec.get("head_sha") or "")
    if not _SHA.match(head):
        msg = "the job names no head"
        raise _OutsideContractError(msg)
    criteria = _criteria(document.get("criteria"), spec.get("criteria") or [])
    return StageOutcome(
        verdict=_VERDICTS[verdict_word],
        reviewed_head=head,
        summary=_summary(verdict_word, criteria, document),
        criteria=criteria,
        provider_verdict=verdict_word,
    )


def _criteria(raw: Any, asked: list[dict[str, Any]]) -> list[CriterionOutcome]:
    """Each criterion coordinare asked about, as QAre reported it (unverified if it did not)."""
    if not isinstance(raw, list):
        msg = "it carries no criteria array"
        raise _OutsideContractError(msg)
    reported: dict[str, dict[str, Any]] = {}
    for entry in raw:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or entry.get("outcome") not in _OUTCOMES:
            msg = "a criterion has no id or an unknown outcome"
            raise _OutsideContractError(msg)
        reported[entry["id"]] = entry
    criteria: list[CriterionOutcome] = []
    for item in asked:
        entry = reported.get(str(item.get("id")))
        if entry is None:
            criteria.append(CriterionOutcome(
                id=str(item.get("id")), text=str(item.get("text") or ""), outcome="unverified",
                reason="QAre reported nothing for this criterion",
            ))
            continue
        criteria.append(CriterionOutcome(
            id=entry["id"], text=str(item.get("text") or ""), outcome=entry["outcome"],
            reason=str(entry.get("reason") or ""),
            evidence=[str(p) for p in entry.get("evidence") or [] if isinstance(p, str)],
        ))
    return criteria


def _summary(verdict: str, criteria: list[CriterionOutcome], document: dict[str, Any]) -> str:
    def reasons(outcome: str) -> str:
        found = [f"{c.id}: {c.reason}" if c.reason else c.id for c in criteria if c.outcome == outcome]
        return "; ".join(found) or "no reason recorded"

    if verdict == "passed":
        return f"QAre proved {len(criteria)} criteria"
    if verdict == "failed":
        return f"QAre found failing criteria: {reasons('failed')}"
    if verdict == "blocked":
        return f"QAre was blocked by the environment, not a defect: {reasons('unverified')}"
    if verdict == "refused":
        return f"QAre refused the run: {reasons('unverified')}"
    waived = document.get("waived")
    who = ", ".join(
        f"{w.get('criterionId')} by {w.get('by')}" for w in waived if isinstance(w, dict)
    ) if isinstance(waived, list) else ""
    return (
        f"QAre recorded a human waiver ({who or 'no waiver record'}); a waiver is never a pass, "
        "so a person decides whether the card moves on"
    )


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _alive(pid: int, job_dir: Path) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False  # another user's process: the pid was reused
    cmdline = Path(f"/proc/{pid}/cmdline")
    if cmdline.exists():
        # Guard against a reused pid after the host restarted.
        try:
            return str(job_dir) in cmdline.read_bytes().decode(errors="replace")
        except OSError:
            return True
    return True


def _remove_tree(path: Path) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)

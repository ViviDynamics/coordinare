"""402: the stage contract a stage provider fills, and the adapter that runs one.

A lifecycle stage is normally run by a performer: coordinare starts a container, the
performer runs a harness, and the result comes back on the performer protocol. A stage
provider is an outside service that fills the same stage under its own identity, for
example Scrutare for reviewing (#402), Progettare for architecting (#403) or QAre for QA
(#404). The contract is the same for all of them:

- input: the repository, the pull request and the head SHA it should judge
  (:class:`StageRequest`), or, for a stage that plans (#403), the work item and a pin
  over the text the planner reads (:class:`WorkItemRequest`);
- output: a verdict, the findings, the head or pin that was actually judged and a
  reference to the record (:class:`StageOutcome`), plus the blueprint for a plan.

:class:`ExternalStageService` adapts any :class:`StageProvider` to the performer-service
protocol the dispatch and monitor nodes already speak (``dispatch_card``,
``check_status``, ``check_health``, ``relay_feedback``). Nothing downstream changes: the
verdict cache keyed by head (125), the feedback loop to the implementer, the system-error
retry budget and restart resumption all apply as they do to a performer.

Rules the adapter enforces for every provider:

- The request id is derived from the card, the stage, the head (or the work item's pin)
  and an attempt number, so a repeated submission (a restart, a re-dispatch) finds the
  same job instead of starting a second one.
- An attempt that ended without delivering anything is retried under the next attempt
  number. Any other finished attempt (a verdict, an escalation, a delivery that is
  confirmed or uncertain) is never resubmitted automatically: every dispatch finds it
  again. Only an operator restart of the stage starts the next attempt.
- An outcome whose judged head is not the pull request's current head, or a plan of
  issue text that has changed since, is refused as a retryable error, so a verdict is
  never applied to an input it did not see.
- Provider failures and unreachability are retryable system errors (a
  :class:`~coordinare.transport.base.TransportError`), never a pass.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any, Protocol

import structlog
from pydantic import BaseModel, ConfigDict, Field

from coordinare.stage_provider_config import normalize_login
from coordinare.transport.base import TransportError
from coordinare.transport.http_transport import PerformerAuthError

logger = structlog.get_logger(__name__)

#: Attempts the adapter walks when earlier attempts ended without delivery. A bound,
#: not a retry budget: the daemon's system-error budget decides how often to retry.
MAX_ATTEMPTS = 20

#: The stages a provider may fill today, and the performer-protocol status each contract
#: verdict maps to. #403 and #404 add their stages here with their own markers.
STAGE_MARKERS: dict[str, dict[str, str]] = {
    "reviewing": {
        "pass": "approved",
        "changes_requested": "changes_requested",
        "needs_human": "blocked",
    },
    # 403: a planner passes with a blueprint held as data; it never requests changes.
    "architecting": {
        "pass": "plan_committed",
        "changes_requested": "blocked",
        "needs_human": "blocked",
    },
}

#: 403: stages whose input is a work item (an issue), not a pull request.
WORK_ITEM_STAGES: frozenset[str] = frozenset({"architecting"})

_PR_URL = re.compile(r"/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)(?:[/?#].*)?$")
_SESSION = re.compile(
    r"^(?P<ref>[^@]+)@(?P<repository>[^@#]+)#(?P<number>\d+)@(?P<node>[^@]+)$",
)
_SHA = re.compile(r"^[0-9a-f]{40}$")
_ISSUE_URL = re.compile(
    r"^https?://(?P<host>[^/\s]+)/(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+)/issues/(?P<number>\d+)/?$",
)
#: The fields of a recorded clarification that carry what a person said.
_CLARIFICATION_CONTENT = ("questions", "answer", "comment_id", "body")
#: Planning a work item that is not a GitHub issue needs Progettare to take it as data.
WORK_ITEM_AS_DATA_ISSUE = "ViviDynamics/progettare#68"


class StageVerdict(StrEnum):
    PASSED = "pass"
    CHANGES_REQUESTED = "changes_requested"
    NEEDS_HUMAN = "needs_human"


class StageRequest(BaseModel):
    """What a stage provider is asked to judge."""

    model_config = ConfigDict(frozen=True)

    card_id: str = Field(min_length=1)
    stage: str = Field(min_length=1)
    repository: str = Field(pattern=r"^[^/\s]+/[^/\s]+$")
    pr_number: int = Field(gt=0)
    pr_node_id: str = Field(min_length=1)
    head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")

    def request_id(self, attempt: int) -> str:
        """The idempotency key of one attempt at this card, stage and head.

        Stable across restarts and re-dispatches, distinct for every head and attempt.
        The card id is hashed: it can be long, and its characters are not ours to choose.
        Only ``[A-Za-z0-9.-]`` appear, which every provider accepts.
        """
        if attempt < 1:
            msg = "attempt numbers start at 1"
            raise ValueError(msg)
        return _request_id(self.card_id, self.stage, self.head_sha, attempt)

    @property
    def pin(self) -> str:
        """What pins this request's input: the head under review."""
        return self.head_sha

    def session_tail(self) -> str:
        return f"{self.repository}#{self.pr_number}@{self.pr_node_id}"


class WorkItemRequest(BaseModel):
    """403: what a planning provider is asked to plan: a GitHub issue, pinned.

    The pin covers everything the planner reads that a person can change: the issue's
    title and body, and the clarifications coordinare recorded. A new answer or an
    edited body is a new request and starts a new run; a restart with neither finds the
    run already there.
    """

    model_config = ConfigDict(frozen=True)

    card_id: str = Field(min_length=1)
    stage: str = Field(min_length=1)
    repository: str = Field(pattern=r"^[^/\s]+/[^/\s]+$")
    number: int = Field(gt=0)
    node_id: str = Field(min_length=1, pattern=r"^[^@]+$")
    host: str = "github.com"
    pin: str = Field(pattern=r"^[0-9a-f]{40}$")

    def request_id(self, attempt: int) -> str:
        return _request_id(self.card_id, self.stage, self.pin, attempt)

    @property
    def ref(self) -> str:
        """The issue as ``owner/repo#N``."""
        return f"{self.repository}#{self.number}"

    def session_tail(self) -> str:
        return f"{self.repository}#{self.number}@{self.node_id}"


#: Either request a provider can be handed.
AnyStageRequest = StageRequest | WorkItemRequest


def _request_id(card_id: str, stage: str, pin: str, attempt: int) -> str:
    """The idempotency key of one attempt at a card, stage and pinned input.

    The card id is hashed: it can be long, and its characters are not ours to choose.
    Only ``[A-Za-z0-9.-]`` appear, which every provider accepts.
    """
    if attempt < 1:
        msg = "attempt numbers start at 1"
        raise ValueError(msg)
    card = hashlib.sha256(card_id.encode()).hexdigest()[:16]
    return f"coordinare.{stage}.{card}.{pin}.{attempt}"


class Finding(BaseModel):
    """One finding the provider recorded. ``path`` and ``line`` are empty for a summary."""

    body: str
    path: str = ""
    line: int | None = None


class StageOutcome(BaseModel):
    """What a stage provider decided, about which head, and where its record is."""

    verdict: StageVerdict
    reviewed_head: str = Field(pattern=r"^[0-9a-f]{40}$")
    summary: str = ""
    findings: list[Finding] = Field(default_factory=list)
    record_ref: str = ""
    #: The identity the provider wrote its record under (a GitHub login), when it did.
    author_login: str = ""
    #: 403: a planner's blueprint, in the spec-165 shape the readers' briefs are cut from.
    blueprint: dict[str, Any] | None = None
    #: 403: what a planner that blocked needs a person to answer.
    questions: list[str] = Field(default_factory=list)
    #: 403: the digest of the work item text the plan was made from (work_item_digest).
    input_digest: str = ""


class JobState(StrEnum):
    PENDING = "pending"
    DONE = "done"
    #: Ended without delivering anything: safe to try again under the next attempt.
    RETRYABLE = "retryable"
    #: Must not be retried automatically, for example a delivery that may have happened.
    NEEDS_HUMAN = "needs_human"


class StageJob(BaseModel):
    """A provider's job, as the provider reported it."""

    ref: str = Field(min_length=1, pattern=r"^[^@]+$")
    state: JobState
    outcome: StageOutcome | None = None
    reason: str = ""
    #: The provider's own status word, for logs and progress text.
    detail: str = ""


class StageProvider(Protocol):
    """An outside service that fills a lifecycle stage."""

    name: str

    async def submit(self, request: AnyStageRequest, request_id: str) -> StageJob:
        """Admit (or find) the job for ``request_id``. Raise TransportError when unreachable."""
        ...

    async def poll(self, ref: str) -> StageJob:
        """Read a job. Raise TransportError when unreachable."""
        ...

    async def health(self) -> tuple[bool, str]:
        """Whether the service answers, with a short reason."""
        ...


class PullRequestSnapshot(BaseModel):
    """The pull request as GitHub shows it now: its head, and its reviews."""

    head_sha: str
    reviews: list[dict[str, Any]] = Field(default_factory=list)


#: Reads a pull request's current head and reviews: ``(repository, pr_node_id)``.
PullRequestReader = Callable[[str, str], Awaitable[PullRequestSnapshot]]


class WorkItemSnapshot(BaseModel):
    """403: the issue as GitHub shows it now."""

    title: str = ""
    body: str = ""
    state: str = ""


#: Reads an issue's current text: ``(repository, issue_node_id)``.
WorkItemReader = Callable[[str, str], Awaitable[WorkItemSnapshot]]


def parse_issue_url(issue_url: str) -> tuple[str, str, int] | None:
    """The host, ``owner/repo`` and number of a GitHub issue URL, or None."""
    match = _ISSUE_URL.match((issue_url or "").strip())
    if match is None:
        return None
    return match["host"].lower(), f"{match['owner']}/{match['repo']}", int(match["number"])


def work_item_digest(title: str, body: str) -> str:
    """The digest of an issue's text, as a planner reads it."""
    text = "\x00".join(part.replace("\r\n", "\n") for part in (title or "", body or ""))
    return hashlib.sha256(text.encode()).hexdigest()


def work_item_pin(digest: str, clarifications: Any) -> str:
    """The pin of a planning request: the issue text and the answers recorded so far.

    Feedback routed back to the architect is not pinned. The planner reads the issue,
    not coordinare's feedback, so the feedback is logged as not forwarded (as for any
    provider) and the plan of the unchanged input stands; coordinare's feedback-cycle
    budget bounds the loop. Editing the issue, answering on the card or an operator
    restart plans again.
    """
    # What a clarification says, in either shape coordinare records: a blocked card's
    # answer (questions, answer) or a routed comment (comment_id, body). Bookkeeping
    # fields stay out, so they cannot make a restart look like a new request.
    answers = [
        {key: c.get(key) for key in _CLARIFICATION_CONTENT if c.get(key) not in (None, "", [])}
        for c in (clarifications if isinstance(clarifications, list) else [])
        if isinstance(c, dict)
    ]
    payload = json.dumps([digest, answers], sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:40]


def parse_pr_url(pr_url: str) -> tuple[str, int] | None:
    """``owner/repo`` and the number from a pull request URL, or None."""
    match = _PR_URL.search(pr_url.strip())
    if match is None:
        return None
    return f"{match['owner']}/{match['repo']}", int(match["number"])


def stage_provider_id(name: str, stage: str) -> str:
    """The performer id a provider's service for one stage is registered under."""
    return f"stage-provider:{name}:{stage}"


class ExternalStageService:
    """Runs one lifecycle stage on a :class:`StageProvider`.

    Speaks the performer-service protocol, so dispatch and monitor treat it like any
    performer. The session id carries the job reference and the pull request, so a
    restarted daemon polls the same job without any extra persisted state.
    """

    def __init__(
        self,
        provider: StageProvider,
        *,
        stage: str,
        pr_reader: PullRequestReader,
    ) -> None:
        if stage not in STAGE_MARKERS:
            msg = f"no stage provider contract for stage {stage!r}"
            raise ValueError(msg)
        if stage in WORK_ITEM_STAGES and not isinstance(self, WorkItemStageService):
            msg = f"the {stage} stage plans a work item; it runs on a WorkItemStageService"
            raise ValueError(msg)
        self._provider = provider
        self._stage = stage
        self._read_pr = pr_reader
        self.performer_id = stage_provider_id(provider.name, stage)
        #: Cards whose current dispatch carries an operator restart of this stage. Set or
        #: cleared on every dispatch from the persisted restart override, and consumed by
        #: the submit, so a permission never outlives the dispatch it was given for.
        self._fresh: set[str] = set()
        #: First attempt worth asking for, per card and head, once an operator restart
        #: walked past a finished attempt. In memory: after a daemon restart, dispatch
        #: starts at attempt 1 again and finds the finished job, the side that never
        #: posts twice.
        self._floor: dict[tuple[str, str], int] = {}

    #: Dispatch needs no workspace, env cache or performer slot: the provider works from GitHub.
    external_stage = True

    def set_fresh_attempt(self, card_id: str, *, allowed: bool) -> None:
        """Whether this dispatch of the card carries an operator restart of the stage."""
        if allowed:
            self._fresh.add(card_id)
        else:
            self._fresh.discard(card_id)

    @property
    def provider_name(self) -> str:
        return self._provider.name

    @property
    def stage(self) -> str:
        return self._stage

    # -- performer-service protocol -------------------------------------------------

    async def dispatch_card(
        self,
        card_context: dict[str, Any],
        workspace_info: Any = None,
        **_ignored: Any,
    ) -> dict[str, Any]:
        """Submit the card's pull request at its current head, or find the job already there."""
        del workspace_info  # a provider works from GitHub, not from a checkout
        request = await self._request_for(card_context)
        queued = card_context.get("relay_feedback")
        if isinstance(queued, list) and queued:
            # A provider judges each head afresh and takes no relayed feedback. Say so,
            # rather than let the queued items look delivered.
            logger.warning(
                "stage_provider.feedback_not_forwarded",
                provider=self._provider.name,
                stage=self._stage,
                card_id=request.card_id,
                items=len(queued),
            )
        fresh = request.card_id in self._fresh
        self._fresh.discard(request.card_id)
        job = await self._submit(request, fresh=fresh)
        logger.info(
            "stage_provider.dispatched",
            provider=self._provider.name,
            stage=self._stage,
            card_id=request.card_id,
            head_sha=request.pin,
            job=job.ref,
            job_state=str(job.state),
        )
        return {
            "status": "accepted",
            "session_id": _session_id(job.ref, request),
            "performer_id": self.performer_id,
            "backend": self._provider.name,
        }

    async def check_status(
        self, session_id: str, *, payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del payload
        match = _SESSION.match(session_id or "")
        if match is None:
            msg = f"{self._provider.name}: unrecognised session {session_id!r}; dispatching again"
            raise TransportError(msg)
        ref, repository, node = match["ref"], match["repository"], match["node"]
        job = await self._provider.poll(ref)
        if job.state is JobState.PENDING:
            return {
                "status": "working",
                "session_id": session_id,
                "backend": self._provider.name,
                "progress": f"{self._provider.name} job {ref} is {job.detail or 'pending'}",
            }
        if job.state is JobState.RETRYABLE:
            msg = f"{self._provider.name} job {ref} ended without a result: {job.reason or job.detail}"
            raise TransportError(msg)
        if job.state is JobState.NEEDS_HUMAN:
            return self._blocked(session_id, f"{self._provider.name} job {ref}: {job.reason or job.detail}")
        if job.outcome is None:
            msg = f"{self._provider.name} job {ref} finished without an outcome"
            raise TransportError(msg)
        return await self._finish(session_id, ref, repository, node, job.outcome)

    async def check_health(self) -> dict[str, Any]:
        try:
            ok, reason = await self._provider.health()
        except TransportError as exc:
            ok, reason = False, str(exc)
        return {"status": "healthy" if ok else "unhealthy", "reason": reason}

    async def relay_feedback(self, review_payload: dict[str, Any]) -> dict[str, Any]:
        del review_payload
        return {
            "status": "error",
            "reason": f"{self._provider.name} takes no relayed feedback; it reviews each head afresh",
        }

    def get_agent_logs(self) -> list[str]:
        return []

    def bind_stop_event(self, stop_event: Any) -> None:
        """Nothing to stop: the provider runs elsewhere."""
        del stop_event

    # -- internals -----------------------------------------------------------------

    async def _request_for(self, card_context: dict[str, Any]) -> AnyStageRequest:
        card_id = str(card_context.get("id") or "").strip()
        pr_url = str(card_context.get("pr_url") or "").strip()
        node = str(card_context.get("pr_node_id") or "").strip()
        target = parse_pr_url(pr_url)
        if not card_id or target is None or not node:
            # Dispatch recovers PR identifiers before this point; reaching here means it
            # could not. Retry rather than judge nothing.
            msg = (
                f"{self._provider.name}: the card has no pull request to judge "
                f"(pr_url={pr_url!r}, pr_node_id set={bool(node)})"
            )
            raise TransportError(msg)
        repository, number = target
        snapshot = await self._snapshot(repository, node)
        return StageRequest(
            card_id=card_id,
            stage=self._stage,
            repository=repository,
            pr_number=number,
            pr_node_id=node,
            head_sha=snapshot.head_sha,
        )

    async def _snapshot(self, repository: str, node: str) -> PullRequestSnapshot:
        try:
            snapshot = await self._read_pr(repository, node)
        except TransportError:
            raise
        except Exception as exc:
            msg = f"could not read the pull request head from GitHub: {type(exc).__name__}: {exc}"
            raise TransportError(msg) from exc
        if not _SHA.match(snapshot.head_sha or ""):
            msg = f"GitHub returned no usable head for {repository} ({snapshot.head_sha!r})"
            raise TransportError(msg)
        return snapshot

    async def _submit(self, request: AnyStageRequest, *, fresh: bool) -> StageJob:
        """Find the live job for this head, walking past attempts that are finished with.

        An attempt that delivered nothing is always finished with. Any other finished
        attempt is finished with only when an operator restarted the stage (``fresh``);
        otherwise it is returned, and the card gets its verdict or block again.
        """
        key = (request.card_id, request.pin)
        for stale in [k for k in self._floor if k[0] == request.card_id and k != key]:
            del self._floor[stale]
        passed_block = False
        for attempt in range(self._floor.get(key, 1), MAX_ATTEMPTS + 1):
            job = await self._provider.submit(request, request.request_id(attempt))
            if not _finished_with(job, fresh=fresh):
                if passed_block:
                    self._floor[key] = attempt
                return job
            passed_block = passed_block or job.state is not JobState.RETRYABLE
            logger.info(
                "stage_provider.attempt_spent",
                provider=self._provider.name,
                stage=self._stage,
                attempt=attempt,
                job=job.ref,
                job_state=str(job.state),
                reason=job.reason or job.detail,
            )
        msg = (
            f"{self._provider.name}: {MAX_ATTEMPTS} attempts at {_subject(request)} "
            "are spent"
        )
        raise TransportError(msg)

    async def _finish(
        self, session_id: str, ref: str, repository: str, node: str, outcome: StageOutcome,
    ) -> dict[str, Any]:
        snapshot = await self._snapshot(repository, node)
        if outcome.reviewed_head != snapshot.head_sha:
            logger.warning(
                "stage_provider.stale_head_refused",
                provider=self._provider.name,
                stage=self._stage,
                job=ref,
                reviewed_head=outcome.reviewed_head,
                current_head=snapshot.head_sha,
            )
            msg = (
                f"{self._provider.name} judged {outcome.reviewed_head[:12]} but the pull request "
                f"head is now {snapshot.head_sha[:12]}; the result is refused and the current "
                "head will be judged"
            )
            raise TransportError(msg)
        outcome = _with_posted_review(outcome, snapshot.reviews)
        marker = STAGE_MARKERS[self._stage][str(outcome.verdict)]
        record = {
            "provider": self._provider.name,
            "job": ref,
            "verdict": str(outcome.verdict),
            "reviewed_head": outcome.reviewed_head,
            "record_ref": outcome.record_ref,
            "author_login": outcome.author_login,
            "findings": [f.model_dump() for f in outcome.findings],
        }
        logger.info(
            "stage_provider.outcome",
            provider=self._provider.name,
            stage=self._stage,
            job=ref,
            verdict=str(outcome.verdict),
            reviewed_head=outcome.reviewed_head,
            findings=len(outcome.findings),
        )
        if outcome.verdict is StageVerdict.NEEDS_HUMAN:
            where = f" ({outcome.record_ref})" if outcome.record_ref else ""
            escalated = self._blocked(
                session_id,
                f"{self._provider.name} escalated head {outcome.reviewed_head[:12]} to a human "
                f"reviewer{where}",
            )
            escalated["report"] = {"stage_record": record}
            return escalated
        reply: dict[str, Any] = {
            "status": marker,
            "session_id": session_id,
            "backend": self._provider.name,
            "head_sha": outcome.reviewed_head,
            "report": {"stage_record": record},
            # The provider posted its own record on the pull request this turn.
            "bot_pr_comment_delta": 1 if outcome.record_ref else 0,
        }
        if outcome.verdict is StageVerdict.CHANGES_REQUESTED:
            reply["comments"] = [
                {
                    "body": f.body,
                    "path": f.path,
                    "line": f.line,
                    "author_login": outcome.author_login or self._provider.name,
                }
                for f in outcome.findings
            ]
            reply["body"] = outcome.summary
        return reply

    def _blocked(self, session_id: str, reason: str) -> dict[str, Any]:
        return {
            "status": "blocked",
            "session_id": session_id,
            "backend": self._provider.name,
            "reason": reason,
            "questions": [reason],
        }


def _finished_with(job: StageJob, *, fresh: bool) -> bool:
    if job.state is JobState.RETRYABLE:
        return True
    # An operator restart always asks again (125 V1), whatever the last attempt said.
    return fresh and job.state in (JobState.NEEDS_HUMAN, JobState.DONE)


def _subject(request: AnyStageRequest) -> str:
    kind = "head" if isinstance(request, StageRequest) else f"{request.ref} pin"
    return f"{kind} {request.pin[:12]}"


def _session_id(ref: str, request: AnyStageRequest) -> str:
    return f"{ref}@{request.session_tail()}"


def _with_posted_review(outcome: StageOutcome, reviews: list[dict[str, Any]]) -> StageOutcome:
    """Fill findings and summary from the review the provider posted, when it gave none.

    A provider that posts a GitHub review (Scrutare) returns a receipt, not its findings;
    the findings are that review's inline comments. Matched on author and head, the
    latest such review wins.
    """
    if outcome.findings or not outcome.author_login:
        return outcome
    login = normalize_login(outcome.author_login)
    mine = [
        r for r in reviews
        if isinstance(r, dict)
        and normalize_login(str(r.get("author_login", ""))) == login
        and str(r.get("commit_oid", "")) in ("", outcome.reviewed_head)
    ]
    if not mine:
        return outcome
    review = mine[-1]
    findings = [
        Finding(body=str(c.get("body", "")), path=str(c.get("path") or ""), line=_line(c.get("line")))
        for c in review.get("comments") or []
        if isinstance(c, dict) and str(c.get("body", "")).strip()
    ]
    summary = outcome.summary or str(review.get("body") or "")
    return outcome.model_copy(update={"findings": findings, "summary": summary})


def _line(value: Any) -> int | None:
    return value if isinstance(value, int) and value > 0 else None



class WorkItemStageService(ExternalStageService):
    """403: runs a stage whose input is a work item (an issue) on a :class:`StageProvider`.

    The attempt walk, the retry and block rules and the session format are the
    reviewer's. What differs is the subject: the request pins the issue's text and the
    answers recorded so far instead of a head, a finished plan must be of the issue text
    as it is now, and a pass carries the blueprint the readers' briefs are cut from.
    """

    def __init__(
        self,
        provider: StageProvider,
        *,
        stage: str,
        work_item_reader: WorkItemReader,
    ) -> None:
        if stage not in WORK_ITEM_STAGES:
            msg = f"the {stage} stage judges a pull request, not a work item"
            raise ValueError(msg)
        super().__init__(provider, stage=stage, pr_reader=_no_pull_request)
        self._read_item = work_item_reader

    async def relay_feedback(self, review_payload: dict[str, Any]) -> dict[str, Any]:
        del review_payload
        return {
            "status": "error",
            "reason": f"{self._provider.name} takes no relayed feedback; it plans each request afresh",
        }

    async def _request_for(self, card_context: dict[str, Any]) -> WorkItemRequest:
        card_id = str(card_context.get("id") or "").strip()
        issue_url = str(card_context.get("issue_url") or "").strip()
        node = str(card_context.get("issue_id") or "").strip()
        target = parse_issue_url(issue_url)
        if target is None or not node or "@" in node:
            # Not a retry: the card is what it is. Blocks with the reason (config error).
            msg = (
                f"{self._provider.name} plans GitHub issues only, and this card's work item is "
                f"not one (issue_url={issue_url!r}). Planning a work item handed in as data "
                f"needs {WORK_ITEM_AS_DATA_ISSUE}; until then, run the architect on a "
                "performer for this symphony"
            )
            raise PerformerAuthError(msg)
        if not card_id:
            msg = f"{self._provider.name}: the card has no id"
            raise TransportError(msg)
        host, repository, number = target
        snapshot = await self._item(repository, node)
        return WorkItemRequest(
            card_id=card_id,
            stage=self._stage,
            repository=repository,
            number=number,
            node_id=node,
            host=host,
            pin=work_item_pin(work_item_digest(snapshot.title, snapshot.body), card_context.get("clarifications")),
        )

    async def _item(self, repository: str, node: str) -> WorkItemSnapshot:
        try:
            return await self._read_item(repository, node)
        except TransportError:
            raise
        except Exception as exc:
            msg = f"could not read the work item from GitHub: {type(exc).__name__}: {exc}"
            raise TransportError(msg) from exc

    async def _finish(
        self, session_id: str, ref: str, repository: str, node: str, outcome: StageOutcome,
    ) -> dict[str, Any]:
        snapshot = await self._item(repository, node)
        current = work_item_digest(snapshot.title, snapshot.body)
        if outcome.input_digest != current:
            logger.warning(
                "stage_provider.stale_work_item_refused",
                provider=self._provider.name,
                stage=self._stage,
                job=ref,
            )
            msg = (
                f"{self._provider.name} planned {repository} from text that has changed since; "
                "the plan is refused and the current text will be planned"
            )
            raise TransportError(msg)
        record = {
            "provider": self._provider.name,
            "job": ref,
            "verdict": str(outcome.verdict),
            "pin": outcome.reviewed_head,
            "record_ref": outcome.record_ref,
        }
        logger.info(
            "stage_provider.outcome",
            provider=self._provider.name,
            stage=self._stage,
            job=ref,
            verdict=str(outcome.verdict),
            pin=outcome.reviewed_head,
            milestones=len((outcome.blueprint or {}).get("milestones") or []),
        )
        if outcome.verdict is StageVerdict.PASSED and outcome.blueprint:
            return {
                "status": STAGE_MARKERS[self._stage]["pass"],
                "session_id": session_id,
                "backend": self._provider.name,
                "report": {"blueprint": dict(outcome.blueprint), "stage_record": record},
                "progress": f"{self._provider.name} blueprint ({outcome.blueprint.get('size') or 'unsized'})",
            }
        if outcome.verdict is StageVerdict.PASSED:
            reason = f"{self._provider.name} reported a plan without a blueprint"
            blocked = self._blocked(session_id, reason)
        else:
            where = f" ({outcome.record_ref})" if outcome.record_ref else ""
            reason = f"{self._provider.name} needs a person: {outcome.summary or 'see the questions'}{where}"
            blocked = self._blocked(session_id, reason)
            if outcome.questions:
                blocked["questions"] = list(outcome.questions)
        blocked["report"] = {"stage_record": record}
        return blocked


async def _no_pull_request(repository: str, node: str) -> PullRequestSnapshot:
    msg = f"a work-item stage reads no pull request ({repository}, {node})"
    raise TransportError(msg)

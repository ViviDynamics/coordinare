"""Advocate role workflow (spec 173): intake, docs, triage, classify, gate, act, report.

The advocate answers inbound GitHub issues from people outside the project. It
runs in a performer, dispatched by coordinare with no card, and coordinare itself
classifies nothing and posts nothing.

The rule that shapes everything else: an answer is posted only when every
document it cites is a document this run actually read. That check is possible
only because the persona travels as instruction and the documentation travels
as evidence, in separate values. When they were one blob, an answer could
"cite" a heading that was an instruction, and nothing could tell the difference.

An issue matching a sensitive keyword escalates before any model call, so a
legal complaint reaches a human whether or not the gateway is up.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import structlog

from performer.models import BackendEvent, BackendEventType
from performer.workflows.advocate.act import (
    Poster,
    apply_outcome,
    redirect_body,
    reply_body,
)
from performer.workflows.advocate.classify import classify_issues
from performer.workflows.advocate.docs import read_documents, readable, render
from performer.workflows.advocate.gate import (
    accept_classification,
    answer_is_grounded,
    confident_enough,
)
from performer.workflows.advocate.intake import select_candidates
from performer.workflows.advocate.models import AdvocateRecord, IssueCandidate
from performer.workflows.advocate.report import build_report
from performer.workflows.advocate.settings import AdvocateSettings
from performer.workflows.advocate.triage import matches_sensitive_keyword
from performer.workflows.base import WorkflowResult

if TYPE_CHECKING:
    from performer.models import Score, Stand

log = structlog.get_logger(__name__)

STATES: tuple[str, ...] = ("intake", "docs", "triage", "classify", "gate", "act", "report")

#: Escalation reasons. Preserved verbatim from the service this replaces, so
#: existing dashboards and notification rules keep working.
REASON_SENSITIVE = "sensitive_keyword"
REASON_NO_DOCS = "no_documentation_configured"
REASON_NO_MATCH = "no_documentation_match"
REASON_LOW_CONFIDENCE = "low_confidence"
REASON_COMPLAINT = "complaint"
REASON_UNCLASSIFIED = "classification_unavailable"


class AdvocateWorkflow:
    """Sequences the advocate steps; only code advances the sequence."""

    name = "advocate"
    #: 343: ordered steps, the single source for the wire and the latch.
    steps = STATES

    def __init__(self, lister=None, poster=None) -> None:
        self._lister = lister
        self._poster = poster

    @staticmethod
    def _step(toolkit: Any, name: str, detail: str = "") -> None:
        emit = getattr(toolkit, "emit", None)
        if callable(emit):
            emit(BackendEvent(type=BackendEventType.progress, text=f"advocate.{name}", detail=detail))

    async def run(self, stand: "Stand", score: "Score", toolkit: Any) -> WorkflowResult:
        metrics = toolkit.metrics
        durations = metrics.step_durations_ms
        settings = AdvocateSettings.from_env(getattr(score, "workflow_env", None) or {})
        record = AdvocateRecord()

        def timed(name: str, started: float) -> None:
            durations[name] = int((time.monotonic() - started) * 1000)

        def finish() -> WorkflowResult:
            self._step(toolkit, "report", record.verdict)
            record.model_calls = metrics.model_calls
            record.workflow_metrics = {"model_calls": metrics.model_calls}
            raw_events = getattr(toolkit, "events", [])
            events = list(raw_events() if callable(raw_events) else raw_events)
            log.info(
                "advocate.done",
                verdict=record.verdict,
                issues=record.issues_seen,
                withheld=len(record.withheld),
                calls=record.model_calls,
            )
            return WorkflowResult(
                report=build_report(record, metrics), findings=[], events=events, metrics=metrics,
            )

        def blocked(reason: str) -> WorkflowResult:
            record.verdict = "env_blocked"
            record.error = reason
            return finish()

        owner_repo = str(getattr(score, "owner_repo", "") or "")
        owner, _, repo = owner_repo.partition("/")
        token = str(getattr(score, "github_token", "") or "")
        lister = self._lister
        poster = self._poster
        if lister is None or poster is None:
            from performer.github import list_open_issues

            if not (owner and repo):
                return blocked("the run has no owner/repo to read issues from")
            lister = lister or (lambda: list_open_issues(owner, repo, token))
            poster = poster or Poster(owner, repo, token)

        # -- intake ------------------------------------------------------
        started = time.monotonic()
        try:
            raw_issues = await lister()
        except Exception as exc:  # noqa: BLE001
            return blocked(f"could not list open issues: {exc}")
        candidates = select_candidates(
            raw_issues,
            handled_label=settings.handled_label,
            escalation_label=settings.escalation_label,
        )[: settings.max_issues_per_run]
        record.issues_seen = len(candidates)
        timed("intake", started)
        self._step(toolkit, "intake", f"{len(candidates)} unhandled")
        log.info("advocate.intake", seen=len(raw_issues), unhandled=len(candidates))

        if not candidates:
            # Nothing to do costs nothing: no documents read, no model call.
            return finish()

        # -- docs --------------------------------------------------------
        started = time.monotonic()
        documents = read_documents(stand.path, settings.doc_sources)
        record.documents_read = [d.path for d in readable(documents)]
        timed("docs", started)
        self._step(toolkit, "docs", f"{len(record.documents_read)} read")

        # -- triage ------------------------------------------------------
        started = time.monotonic()
        outcomes = []
        to_classify: list[IssueCandidate] = []
        for issue in candidates:
            hit = matches_sensitive_keyword(issue.text, settings.sensitive_keywords)
            if hit:
                outcomes.append(await self._escalate(poster, issue, REASON_SENSITIVE, settings))
            else:
                to_classify.append(issue)
        timed("triage", started)
        self._step(toolkit, "triage", f"{len(outcomes)} escalated, {len(to_classify)} to classify")

        if to_classify and not record.documents_read:
            # No readable documentation: every remaining issue escalates rather
            # than being answered from nothing.
            for issue in to_classify:
                outcomes.append(await self._escalate(poster, issue, REASON_NO_DOCS, settings))
            record.outcomes = outcomes
            return finish()

        # -- classify ----------------------------------------------------
        started = time.monotonic()
        sent_ids = {i.issue_id for i in to_classify}
        classifications = await classify_issues(
            to_classify,
            render(documents),
            toolkit,
            max_per_call=settings.max_issues_per_call,
        )
        timed("classify", started)
        self._step(toolkit, "classify", f"{len(classifications)} returned")

        # -- gate + act ---------------------------------------------------
        started = time.monotonic()
        by_id = {}
        for c in classifications:
            if not accept_classification(c, sent_ids):
                log.warning("advocate.unsent_judgement", issue=c.issue_id)
                continue
            by_id.setdefault(c.issue_id, c)

        for issue in to_classify:
            classification = by_id.get(issue.issue_id)
            if classification is None:
                outcomes.append(await self._escalate(poster, issue, REASON_UNCLASSIFIED, settings))
                continue
            outcomes.append(
                await self._decide(poster, issue, classification, settings, record, documents),
            )
        record.outcomes = outcomes
        timed("gate", started)
        self._step(toolkit, "gate", f"{len(record.withheld)} withheld")

        # -- write-free proof ---------------------------------------------
        runner = getattr(toolkit, "run_command", None)
        if callable(runner):
            try:
                result = await runner("git status --porcelain")
                record.write_free_check = str(getattr(result, "stdout", "") or "")
            except Exception:  # noqa: BLE001 - proof is evidence, not a gate
                record.write_free_check = ""
        return finish()

    async def _escalate(self, poster, issue, reason: str, settings: AdvocateSettings):
        """Hand an issue to a human, with the reason on the record."""
        return await apply_outcome(
            poster, issue,
            action="escalated",
            label=settings.escalation_label,
            body=settings.holding_comment_template,
            settings=settings,
            escalation_reason=reason,
        )

    async def _decide(self, poster, issue, classification, settings, record, documents):
        """Turn one classification into one action. All of this is code."""
        kind = classification.classification

        if kind == "complaint":
            return await self._escalate(poster, issue, REASON_COMPLAINT, settings)

        if kind == "feature_request":
            return await apply_outcome(
                poster, issue, action="acknowledged", label=settings.handled_label,
                body=settings.acknowledgement_template, settings=settings,
                classification=classification,
            )

        if kind == "bug_report":
            # Labelled, no comment: preserved from the behaviour this replaces.
            return await apply_outcome(
                poster, issue, action="triaged", label=settings.handled_label,
                body=None, settings=settings, classification=classification,
            )

        if kind == "off_topic":
            return await apply_outcome(
                poster, issue, action="redirected", label=settings.handled_label,
                body=redirect_body(settings), settings=settings,
                classification=classification,
            )

        # question / confusion: the only kinds that may receive an answer.
        if not confident_enough(classification, settings.confidence_threshold):
            return await self._escalate(poster, issue, REASON_LOW_CONFIDENCE, settings)

        grounded, why = answer_is_grounded(classification, documents)
        if not grounded:
            record.withheld.append({
                "issue_id": issue.issue_id,
                "reason": why,
                "cited_documents": list(classification.cited_documents),
            })
            return await self._escalate(poster, issue, REASON_NO_MATCH, settings)

        return await apply_outcome(
            poster, issue, action="replied", label=settings.handled_label,
            body=reply_body(classification, settings), settings=settings,
            classification=classification,
        )

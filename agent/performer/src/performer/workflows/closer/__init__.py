"""Closer role workflow (spec 172): intake, classify, judge, gate, post, act, report.

The closing review confirms one thing: that every review thread is resolved
before a human is asked to look. GitHub already answers that for most threads,
so code classifies them all and only the genuinely ambiguous ones (unresolved,
not outdated, answered by someone other than the raiser) reach one
schema-guarded model call. Every judgement must quote the thread's own words or
it is discarded. The verdict is derived by code, one review is posted, and only
then are the earned threads resolved. A card whose threads are all resolved
makes no model call at all.

Remote CI is never consulted here: spec 064's rollup gate owns it, and closers
rejecting on pending checks is what caused the bounce loop that directive exists
to prevent. Selected by ``workflow: closer`` on the closer role.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import structlog

from performer.models import BackendEvent, BackendEventType
from performer.workflows.base import WorkflowResult
from performer.workflows.closer.budgets import CloserBudgets
from performer.workflows.closer.classify import classify_thread
from performer.workflows.closer.gate import run_gate
from performer.workflows.closer.judge import run_judge_step
from performer.workflows.closer.models import ClosingRecord, Thread
from performer.workflows.closer.post import post_closing_review
from performer.workflows.closer.report import build_report

if TYPE_CHECKING:
    from performer.models import Score, Stand

log = structlog.get_logger(__name__)

STATES: tuple[str, ...] = ("intake", "classify", "judge", "gate", "post", "act", "report")


class CloserWorkflow:
    """Sequences the closer steps; only code advances the sequence."""

    name = "closer"

    def __init__(self, fetcher=None, resolver=None, poster=None) -> None:
        self._fetcher = fetcher
        self._resolver = resolver
        self._poster = poster

    @staticmethod
    def _step(toolkit, name: str, detail: str = "") -> None:
        emit = getattr(toolkit, "emit", None)
        if callable(emit):
            emit(BackendEvent(type=BackendEventType.progress, text=f"closer.{name}", detail=detail))

    async def run(self, stand: "Stand", score: "Score", toolkit: Any) -> WorkflowResult:
        metrics = toolkit.metrics
        durations = metrics.step_durations_ms
        budgets = CloserBudgets.from_env(getattr(score, "workflow_env", None) or {})

        def timed(name: str, started: float) -> None:
            durations[name] = int((time.monotonic() - started) * 1000)

        def finish(record: ClosingRecord) -> WorkflowResult:
            self._step(toolkit, "report", record.verdict)
            record.workflow_metrics = {"model_calls": metrics.model_calls}
            raw_events = getattr(toolkit, "events", [])
            events = list(raw_events() if callable(raw_events) else raw_events)
            return WorkflowResult(report=build_report(record, metrics), findings=[], events=events, metrics=metrics)

        # intake: the threads are the whole input
        self._step(toolkit, "intake")
        t = time.monotonic()
        pr_url = str(getattr(score, "pr_url", "") or "")
        try:
            threads, pages = await self._fetch(score, budgets)
        except Exception as exc:  # noqa: BLE001 - the hold names the failure
            timed("intake", t)
            log.warning("closer.fetch_failed", error=str(exc)[:300])
            return finish(ClosingRecord(threads_read=0, classifications=[], verdict="env_blocked",
                                        hold_reason=f"could not read the review threads: {str(exc)[:300]}"))
        timed("intake", t)
        log.info("closer.intake", threads=len(threads), pages=pages, pr=pr_url)

        # classify: pure rules over what GitHub reported
        self._step(toolkit, "classify", f"{len(threads)} thread(s)")
        t = time.monotonic()
        by_id = {th.id: th for th in threads}
        classifications = [classify_thread(th) for th in threads]
        answered = [by_id[c.thread_id] for c in classifications if c.state == "answered"]
        timed("classify", t)
        counts: dict[str, int] = {}
        for c in classifications:
            counts[c.state] = counts.get(c.state, 0) + 1
        log.info("closer.classify", **counts)

        # judge: only the ambiguous threads, only when there are any
        judged: list[Any] = []
        sent_ids: list[str] = []
        if answered:
            self._step(toolkit, "judge", f"{len(answered)} ambiguous thread(s)")
            t = time.monotonic()
            sent = answered[: budgets.max_threads_per_call]
            sent_ids = [th.id for th in sent]
            out = await run_judge_step(toolkit, sent, budgets.max_threads_per_call)
            judged = list(getattr(out, "judgements", []) or [])
            timed("judge", t)
            log.info("closer.judge", sent=len(sent_ids), returned=len(judged), deferred=len(answered) - len(sent))
        else:
            log.info("closer.judge_skipped", reason="no ambiguous threads")

        # gate: accept what is traceable, derive the verdict
        self._step(toolkit, "gate")
        t = time.monotonic()
        outcome = run_gate(classifications, judged, sent_ids, by_id)
        timed("gate", t)
        open_threads = [by_id[i] for i in outcome.open_thread_ids if i in by_id]
        log.info("closer.gate", verdict=outcome.verdict, open=len(open_threads),
                 accepted=sum(1 for j in outcome.judgements if j.accepted),
                 discarded=[j.discard_reason for j in outcome.judgements if j.discard_reason])

        record = ClosingRecord(
            head_sha=str(getattr(score, "head_sha", "") or "") or None,
            threads_read=len(threads), pages_read=pages, classifications=classifications, judgements=outcome.judgements,
            open_threads=[{"thread_id": th.id, "path": th.path, "line": th.line,
                           "excerpt": (th.comments[0].body.strip()[:300] if th.comments else "")} for th in open_threads],
            verdict=outcome.verdict,
        )

        # post: one review, before anything is resolved (FR-007)
        self._step(toolkit, "post", record.verdict)
        t = time.monotonic()
        posted = await post_closing_review(score, outcome.resolve if record.verdict == "approved" else [], open_threads, poster=self._poster)
        timed("post", t)
        log.info("closer.post", verdict=record.verdict, posted=posted.ok, url=posted.url, error=posted.error)
        if not posted.ok:
            record.verdict = "env_blocked"
            record.hold_reason = posted.error
            return finish(record)
        record.posted_review_url = posted.url

        # act: resolve only what earned it, and only on a passing verdict (FR-008)
        if record.verdict == "approved" and outcome.resolve:
            self._step(toolkit, "act", f"{len(outcome.resolve)} thread(s)")
            t = time.monotonic()
            ids = [tid for tid, _reason in outcome.resolve]
            try:
                resolved_ids, failures = await self._resolve(score, ids)
            except Exception as exc:  # noqa: BLE001 - the hold names the failure
                resolved_ids, failures = [], [{"error": str(exc)[:300]}]
            timed("act", t)
            record.resolved = [{"thread_id": tid, "reason": reason} for tid, reason in outcome.resolve if tid in set(resolved_ids)]
            missing = [tid for tid in ids if tid not in set(resolved_ids)]
            log.info("closer.act", resolved=len(resolved_ids), failed=len(missing))
            if missing:
                # FR-009: never approve carrying a thread the closer believed closed.
                record.verdict = "env_blocked"
                record.hold_reason = f"could not resolve {len(missing)} thread(s): {failures[:2]}"
        return finish(record)

    async def _fetch(self, score, budgets) -> tuple[list[Thread], int]:
        if self._fetcher is not None:
            return await self._fetcher(score, budgets)
        from performer.github import fetch_review_threads  # noqa: PLC0415 - late import keeps the workflow importable without network deps
        from performer.workflows.reviewer.post import pr_number_from_url  # noqa: PLC0415

        number = pr_number_from_url(getattr(score, "pr_url", "") or "")
        if number <= 0:
            raise ValueError(f"pr_url is missing or invalid ({getattr(score, 'pr_url', None)!r})")
        owner, repo = score.owner_repo
        raw, pages = await fetch_review_threads(owner, repo, number, score.effective_github_token, max_pages=budgets.max_pages)
        return [Thread.model_validate(t) for t in raw], pages

    async def _resolve(self, score, ids: list[str]) -> tuple[list[str], list[dict]]:
        if self._resolver is not None:
            return await self._resolver(score, ids)
        from performer.github import resolve_review_threads  # noqa: PLC0415

        owner, repo = score.owner_repo
        return await resolve_review_threads(owner, repo, ids, score.effective_github_token)


__all__ = ["CloserWorkflow", "STATES"]

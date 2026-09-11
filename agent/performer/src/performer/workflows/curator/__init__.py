"""Curator role workflow (spec 173): intake, judge, gate, act, report.

The curator proposes work. It reads the repository's open issues, judges each
against the configured selection criteria with one guarded call, and adds the
ones that qualify to the board's BACKLOG with a label and a comment saying why.
It never puts a card in the column coordinare dispatches from, because a human
decides what becomes work.

A judgement whose stated reason quotes text the issue does not contain is
rejected and the issue is left alone: the quote is what a human checks the
proposal against.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import structlog

from performer.models import BackendEvent, BackendEventType
from performer.workflows.base import WorkflowResult
from performer.workflows.budget import Budget
from performer.workflows.curator.candidates import select_candidates
from performer.workflows.curator.gate import accept_judgement
from performer.workflows.curator.models import (
    CurationOutcome,
    CurationRecord,
    SelectionBatch,
)
from performer.workflows.curator.personas import render_candidates, render_persona
from performer.workflows.curator.settings import CuratorSettings

if TYPE_CHECKING:
    from performer.models import Score, Stand

log = structlog.get_logger(__name__)

STATES: tuple[str, ...] = ("intake", "judge", "gate", "act", "report")


class CuratorWorkflow:
    """Sequences the curator steps; only code advances the sequence."""

    name = "curator"
    #: 343: ordered steps, the single source for the wire and the latch.
    steps = STATES

    def __init__(self, lister=None, board=None) -> None:
        self._lister = lister
        self._board = board

    @staticmethod
    def _step(toolkit: Any, name: str, detail: str = "") -> None:
        emit = getattr(toolkit, "emit", None)
        if callable(emit):
            emit(BackendEvent(type=BackendEventType.progress, text=f"curator.{name}", detail=detail))

    async def run(self, stand: "Stand", score: "Score", toolkit: Any) -> WorkflowResult:
        metrics = toolkit.metrics
        durations = metrics.step_durations_ms
        settings = CuratorSettings.from_env(getattr(score, "workflow_env", None) or {})
        record = CurationRecord()

        def timed(name: str, started: float) -> None:
            durations[name] = int((time.monotonic() - started) * 1000)

        def finish() -> WorkflowResult:
            self._step(toolkit, "report", record.verdict)
            record.model_calls = metrics.model_calls
            record.workflow_metrics = {"model_calls": metrics.model_calls}
            raw_events = getattr(toolkit, "events", [])
            events = list(raw_events() if callable(raw_events) else raw_events)
            log.info(
                "curator.done",
                verdict=record.verdict,
                candidates=record.candidates_seen,
                added=sum(1 for o in record.outcomes if o.action == "added"),
                rejected=len(record.rejected_judgements),
            )
            return WorkflowResult(
                report={
                    "curation": record.model_dump(mode="json"),
                    "write_free_check": record.write_free_check,
                    "workflow_metrics": {
                        "model_calls": metrics.model_calls,
                        "step_durations_ms": dict(durations),
                    },
                },
                findings=[], events=events, metrics=metrics,
            )

        def blocked(reason: str) -> WorkflowResult:
            record.verdict = "env_blocked"
            record.error = reason
            return finish()

        project_id = str(getattr(score, "project_id", "") or "")
        if not project_id and self._board is None:
            # Reported, not skipped: a curator with no board did nothing, and
            # saying "no candidates" would hide the misconfiguration.
            return blocked("no project board id was supplied, so nothing can be proposed")

        board = self._board
        lister = self._lister
        if lister is None or board is None:
            owner_repo = str(getattr(score, "owner_repo", "") or "")
            owner, _, repo = owner_repo.partition("/")
            token = str(getattr(score, "github_token", "") or "")
            if not (owner and repo):
                return blocked("the run has no owner/repo to read issues from")
            from performer.github import add_item_to_project, add_labels, list_open_issues, post_issue_comment

            class _Board:
                async def add(self, issue_id: str) -> str:
                    return await add_item_to_project(project_id, issue_id, token)

                async def label(self, issue_id: str, label: str) -> None:
                    await add_labels(owner, repo, issue_id, [label], token)

                async def comment(self, number: int, body: str) -> None:
                    await post_issue_comment(owner, repo, number, body, token)

            lister = lister or (lambda: list_open_issues(owner, repo, token))
            board = board or _Board()

        # -- intake ------------------------------------------------------
        started = time.monotonic()
        try:
            raw = await lister()
        except Exception as exc:  # noqa: BLE001
            return blocked(f"could not list open issues: {exc}")
        on_board = set(await board.on_board_ids()) if hasattr(board, "on_board_ids") else set()
        candidates = select_candidates(
            raw, curator_label=settings.label, on_board_ids=on_board,
            max_per_run=settings.max_per_run,
        )
        record.candidates_seen = len(candidates)
        timed("intake", started)
        self._step(toolkit, "intake", f"{len(candidates)} candidates")
        if not candidates:
            return finish()

        # -- judge -------------------------------------------------------
        started = time.monotonic()
        by_id = {c.issue_id: c for c in candidates}
        content = render_candidates(candidates)
        persona = render_persona(settings.criteria)

        try:
            parsed = await toolkit.call_model(
                persona=persona,
                schema=SelectionBatch,
                content=[{"type": "text", "text": content}],
                budget=Budget.for_step("curator_judge"),
            )
            judgements = parsed.judgements
        except Exception as exc:  # noqa: BLE001
            return blocked(f"the selection judgement could not be read: {exc}")
        timed("judge", started)
        self._step(toolkit, "judge", f"{len(judgements)} returned")

        # -- gate + act ---------------------------------------------------
        started = time.monotonic()
        for judgement in judgements:
            ok, why = accept_judgement(judgement, by_id)
            if not ok:
                record.rejected_judgements.append({"issue_id": judgement.issue_id, "reason": why})
                continue
            issue = by_id[judgement.issue_id]
            if not judgement.qualifies:
                record.outcomes.append(CurationOutcome(
                    issue_id=issue.issue_id, number=issue.number,
                    action="skipped", reason=judgement.reason,
                ))
                continue
            record.outcomes.append(await self._promote(board, issue, judgement, settings))
        timed("act", started)
        self._step(toolkit, "act", f"{sum(1 for o in record.outcomes if o.action == 'added')} added")

        runner = getattr(toolkit, "run_command", None)
        if callable(runner):
            try:
                result = await runner("git status --porcelain")
                record.write_free_check = str(getattr(result, "stdout", "") or "")
            except Exception:  # noqa: BLE001
                record.write_free_check = ""
        return finish()

    async def _promote(self, board, issue, judgement, settings) -> CurationOutcome:
        """Add to the backlog, label, and say why. Never to the dispatch column."""
        outcome = CurationOutcome(
            issue_id=issue.issue_id, number=issue.number, action="added",
            reason=judgement.reason, quote=judgement.quote,
            column=settings.backlog_column,
        )
        try:
            outcome.board_item_id = await board.add(issue.issue_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("curator.board_add_failed", issue=issue.issue_id, error=str(exc))
            return CurationOutcome(
                issue_id=issue.issue_id, number=issue.number, action="rejected",
                reason=f"could not add to the board: {exc}",
            )
        try:
            await board.label(issue.issue_id, settings.label)
        except Exception as exc:  # noqa: BLE001
            log.warning("curator.label_failed", issue=issue.issue_id, error=str(exc))
        try:
            await board.comment(
                issue.number,
                f"Proposed for the board backlog: {judgement.reason}\n\n"
                f"> {judgement.quote}\n\n"
                f"A maintainer decides whether this becomes work.",
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("curator.comment_failed", issue=issue.issue_id, error=str(exc))
        return outcome

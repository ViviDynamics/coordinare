"""Curator role workflow (spec 173): intake, judge, gate, act, report.

The curator proposes work. It reads the repository's open issues, judges each
against the configured selection criteria with one guarded call, and adds the
ones that qualify to the board's BACKLOG with a label and a comment saying why.
It never puts a card in the column coordinare dispatches from, because a human
decides what becomes work.

A judgement whose stated reason quotes text the issue does not contain is
rejected and the issue is left alone: the quote is what a human checks the
proposal against.

416 closes the loop the first version left open: declined issues are labelled
so the scan actually advances, sensitive topics are triaged at intake and
before any public prose, the board write sets the backlog column, and one
failed gateway conversation no longer sinks the batches that never ran.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import structlog

from performer.models import BackendEvent, BackendEventType
from performer.workflows.advocate.triage import matches_sensitive_keyword
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
            # effective_github_token is the documented source: the payload
            # token first, then the injected GITHUB_TOKEN. Reading the raw
            # field left the Kubernetes transport env-blocked with valid
            # credentials available.
            token = str(getattr(score, "effective_github_token", None)
                        or getattr(score, "github_token", "") or "")
            # Score.owner_repo is a property parsing repo_url; 416's tests are
            # the first to run this path, and the old str()/partition() here
            # turned the tuple "('o', 'r')" into an empty repo and a blocked
            # run for every production invocation.
            try:
                owner, repo = getattr(score, "owner_repo", ())
            except (TypeError, ValueError):
                owner = repo = ""
            if not (owner and repo):
                return blocked("the run has no owner/repo to read issues from")
            from performer.github import (
                add_item_to_project,
                add_labels,
                ensure_label,
                list_open_issues,
                list_project_item_ids,
                post_issue_comment,
                set_project_item_field,
            )

            class _Board:
                """Production board: the GitHub calls the scan loop needs.

                416: ``on_board_ids`` is what makes the dedup check real and
                ``set_column`` is what keeps curated items out of the column
                coordinare dispatches from -- GitHub's own "item added" rule
                otherwise files them wherever the field last pointed, which is
                how curator work ended up in Todo.
                """

                async def add(self, issue_id: str) -> str:
                    return await add_item_to_project(project_id, issue_id, token)

                async def label(self, issue_id: str, label: str) -> None:
                    # 416: the skipped and escalation labels only advance the
                    # scan when they exist, so a missing label is created (the
                    # advocate Poster's contract) instead of leaving the run
                    # stuck on the same newest page.
                    try:
                        await add_labels(owner, repo, issue_id, [label], token)
                    except Exception:
                        await ensure_label(owner, repo, label, token)
                        await add_labels(owner, repo, issue_id, [label], token)

                async def comment(self, number: int, body: str) -> None:
                    await post_issue_comment(owner, repo, number, body, token)

                async def on_board_ids(self) -> list[str]:
                    return await list_project_item_ids(project_id, token)

                async def set_column(self, item_id: str, column: str) -> None:
                    await set_project_item_field(
                        project_id, item_id, column, token, field_name="Status",
                    )

            lister = lister or (lambda: list_open_issues(owner, repo, token))
            board = board or _Board()

        # -- intake ------------------------------------------------------
        started = time.monotonic()
        raw, on_board, failure = await self._scan_inputs(lister, board)
        if failure:
            return blocked(failure)
        candidates = select_candidates(
            raw, curator_label=settings.label, on_board_ids=on_board,
            max_per_run=settings.max_per_run,
            excluded_labels=(settings.skipped_label, settings.escalation_label),
        )
        record.candidates_seen = len(candidates)
        timed("intake", started)
        self._step(toolkit, "intake", f"{len(candidates)} candidates")
        if not candidates:
            return finish()

        # -- intake triage (416) ------------------------------------------
        judging = await self._escalate_sensitive(board, candidates, settings, record)

        # -- judge -------------------------------------------------------
        by_id = {c.issue_id: c for c in judging}
        persona = render_persona(settings.criteria)

        # 416: the scan is judged in bounded slices. One gateway conversation
        # that returns malformed JSON costs its slice, not the run's whole
        # judgement budget, so the issues behind it are still proposed.
        judgements: list[Any] = []
        last_error: Exception | None = None
        started = time.monotonic()
        for index in range(0, len(judging), max(1, settings.max_per_call)):
            batch_slice = judging[index:index + max(1, settings.max_per_call)]
            try:
                parsed = await toolkit.call_model(
                    persona=persona,
                    schema=SelectionBatch,
                    content=[{"type": "text", "text": render_candidates(batch_slice)}],
                    budget=Budget.for_step("curator_judge"),
                )
                batch_ids = {c.issue_id for c in batch_slice}
                kept: list[Any] = []
                for judgement in parsed.judgements:
                    # A response for one slice cannot name an issue whose
                    # content was not in that slice's prompt: the quote gate
                    # would still check it, but against text the model never
                    # saw, which is trust the prompt does not buy.
                    if judgement.issue_id not in batch_ids:
                        record.rejected_judgements.append({
                            "issue_id": judgement.issue_id,
                            "reason": "named an issue that was not in this batch's prompt",
                        })
                        continue
                    kept.append(judgement)
                judgements.extend(kept)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                log.warning(
                    "curator.judgement_batch_failed",
                    size=len(batch_slice), error=str(exc),
                )
        if not judgements and last_error is not None:
            return blocked(f"the selection judgement could not be read: {last_error}")
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
                # 416: a skip is durable. Without the label the newest
                # non-qualifying issues were re-judged every cycle and the
                # rest of the queue was never reached.
                try:
                    await board.label(issue.issue_id, settings.skipped_label)
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "curator.skip_label_failed", issue=issue.issue_id, error=str(exc),
                    )
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

    async def _scan_inputs(self, lister, board) -> tuple[list, set, str]:
        """The issues and the board ids, or the reason the run cannot proceed.

        A transient board failure must read as blocked, not as "no issues are
        on the board yet": silently degrading would run the dedup check with
        an empty set and re-propose duplicates.
        """
        try:
            raw = await lister()
        except Exception as exc:  # noqa: BLE001
            return [], set(), f"could not list open issues: {exc}"
        try:
            on_board = set(await board.on_board_ids()) if hasattr(board, "on_board_ids") else set()
        except Exception as exc:  # noqa: BLE001
            return raw, set(), f"could not consult the board for on-board issues: {exc}"
        else:
            return raw, on_board, ""

    async def _escalate_sensitive(
        self, board, candidates: list, settings: CuratorSettings, record: CurationRecord,
    ) -> list[Any]:
        """Label and set aside the candidates triage refuses to touch (416).

        The advocate's rule, at the only point where it is cheap: a legal
        report becomes work nobody asked for if it slips past triage, so it is
        labelled for a human here, before any model call and before any public
        write that prose could accompany.  Returns what should still be judged.
        """
        judging: list[Any] = []
        for candidate in candidates:
            keyword = matches_sensitive_keyword(candidate.text, settings.sensitive_keywords)
            if not keyword:
                judging.append(candidate)
                continue
            try:
                await board.label(candidate.issue_id, settings.escalation_label)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "curator.escalation_label_failed",
                    issue=candidate.issue_id, error=str(exc),
                )
            record.outcomes.append(CurationOutcome(
                issue_id=candidate.issue_id, number=candidate.number,
                action="escalated",
                reason=f"escalated before judging: sensitive topic ({keyword})",
            ))
        return judging

    async def _promote(self, board, issue, judgement, settings) -> CurationOutcome:
        """Add to the backlog, set the column, label, and say why.

        The board writes come before the comment and the comment only goes
        out if the same triage the intake run passes. A failure anywhere in
        the public prose path is recorded, not raised: the board is the
        durable record, the comment is the courtesy.
        """
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

        # 416: addProjectV2ItemById only adds. Without this write GitHub's own
        # "item added" rule leaves the item wherever the Status field last
        # pointed, which is how curated work landed in a dispatch column.
        column_ok = True
        set_column = getattr(board, "set_column", None)
        if callable(set_column) and outcome.board_item_id:
            try:
                await set_column(outcome.board_item_id, settings.backlog_column)
            except Exception as exc:  # noqa: BLE001
                column_ok = False
                log.warning(
                    "curator.column_set_failed", issue=issue.issue_id, error=str(exc),
                )

        try:
            await board.label(issue.issue_id, settings.label)
        except Exception as exc:  # noqa: BLE001
            log.warning("curator.label_failed", issue=issue.issue_id, error=str(exc))

        # 416: a comment that says "proposed for the backlog" while the item
        # still sits in a dispatch column invites a human to a place the
        # workflow failed to write, so the prose waits for the board.
        if not column_ok:
            outcome.comment_withheld_reason = "column_not_set"
            log.warning(
                "curator.comment_withheld", issue=issue.issue_id, reason="column_not_set",
            )
            return outcome

        # The reason is model prose and the quote is issue text. Triage both
        # before anything public: a promotion that quotes a security report
        # verbatim in a comment is the same leak as an answer would be.
        haystack = f"{issue.text}\n{judgement.reason}\n{judgement.quote}"
        keyword = matches_sensitive_keyword(haystack, settings.sensitive_keywords)
        if keyword:
            outcome.comment_withheld_reason = "sensitive_keyword"
            log.warning(
                "curator.comment_withheld",
                issue=issue.issue_id, reason="sensitive_keyword", keyword=keyword,
            )
            return outcome

        quote = str(judgement.quote or "").strip()
        # A multi-line quote must stay a blockquote: unindented lines would
        # otherwise render as prose outside the quotation.
        quoted = "\n".join(f"> {line}" if line else ">" for line in quote.splitlines())
        try:
            await board.comment(
                issue.number,
                f"Proposed for the board backlog: {judgement.reason}\n\n"
                f"{quoted}\n\n"
                f"A maintainer decides whether this becomes work.",
            )
            outcome.comment_posted = True
        except Exception as exc:  # noqa: BLE001
            log.warning("curator.comment_failed", issue=issue.issue_id, error=str(exc))
        return outcome

"""Backend UI refresh and assessor decline handling (435)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from coordinare.graph.attribution import coordinare_attribution
from coordinare.graph.state import _retire_active_session
from coordinare.services.board_provider import move_card_or_warn

if TYPE_CHECKING:
    from coordinare.graph.state import CoordinareState

logger = structlog.get_logger(__name__)


_STATS_POLL_INTERVAL_SECONDS = 30


async def _refresh_backend_ui(
    state: CoordinareState,
    service: Any,
    card_id: str,
) -> None:
    """Discover the backend UI URL from stderr logs and optionally poll session stats.

    Best-effort: any error is swallowed and logged at DEBUG so the performer
    session is never interrupted.  Stats polling is throttled to at most once
    per _STATS_POLL_INTERVAL_SECONDS to avoid hammering the local HTTP server.
    """
    from coordinare.transport.subprocess_transport import (
        _discover_backend_ui_url,
        _fetch_session_stats,
    )

    try:
        getter = getattr(service, "get_agent_logs", None)
        if not callable(getter):
            return
        logs = getter()
        if not isinstance(logs, list):
            return

        # Prefer fresh discovery; fall back to stored URL so stats keep updating
        # even after the opencode server line scrolls out of the log buffer.
        url = _discover_backend_ui_url(logs) or state.get("backend_ui_url")
        if not url:
            return

        state["backend_ui_url"] = url

        # Throttle stats polling to at most once per 30 seconds.
        last_fetched = state.get("_backend_stats_fetched_at")
        now = datetime.now(UTC)
        if last_fetched is not None:
            elapsed = (now - last_fetched).total_seconds()
            if elapsed < _STATS_POLL_INTERVAL_SECONDS:
                return

        # Record the attempt time before fetching so failed polls are also throttled.
        state["_backend_stats_fetched_at"] = now
        stats = await _fetch_session_stats(url)
        if stats is not None:
            state["session_stats"] = stats
    except Exception as exc:
        logger.debug("monitor_performer.backend_ui_refresh_failed", card_id=card_id, error=str(exc))


async def _apply_assessor_decline(
    state: CoordinareState,
    card: dict[str, Any],
    card_id: str,
    marker: str,
    status: dict[str, Any],
    board_provider: Any,
    github: Any,
) -> CoordinareState:
    """Board outcome for an assessor verdict that declines the card (410).

    not_work: comment the reasoning, close the issue AND move the project item
    to DONE (closing the issue alone leaves the project column in TODO, where
    the next poll would admit the card again), then retire the run.
    needs_split: comment the proposed split, move the card back to BACKLOG,
    retire the run. The board comment is the durable record of the decision;
    the in-state assessment rides only the current daemon run. Both outcomes
    are best-effort on the board side: a missing GitHub service or node id
    logs and continues, because the run's retirement must not depend on a
    mutation succeeding.
    """
    raw_report = status.get("report")
    report = raw_report if isinstance(raw_report, dict) else {}
    raw_assessment = report.get("assessment")
    assessment = raw_assessment if isinstance(raw_assessment, dict) else {}
    reason = str(assessment.get("expected_behavior") or assessment.get("goal") or "the assessor declined this card").strip()
    verdict = "not_work" if marker == "assessment_not_work" else "needs_split"

    recorded = dict(assessment)
    if assessment:
        recorded["recorded_at"] = datetime.now(UTC).isoformat()
    state["assessment"] = recorded
    logger.info(
        "monitor_performer.assessment_declined",
        card_id=card_id,
        verdict=verdict,
        reason_length=len(reason),
    )

    issue_id = str(card.get("issue_id") or "")
    if github is not None and hasattr(github, "add_comment") and issue_id:
        try:
            header = coordinare_attribution(state.get("config"), None)
            if verdict == "not_work":
                body = (
                    f"{header}\n\n**Assessor verdict: not work**\n\n{reason}\n\n"
                    "Closing this issue without building it. Correct the premise and reopen, or split the work."
                )
            else:
                body = (
                    f"{header}\n\n**Assessor verdict: needs split**\n\n{reason}\n\n"
                    "The card moved back to the backlog. Split it into independent issues and bring them back one at a time."
                )

            await github.add_comment(issue_id, body)
        except Exception as exc:
            logger.warning("monitor_performer.assessment_decline_comment_failed", card_id=card_id, error=str(exc))

    if verdict == "not_work" and github is not None and hasattr(github, "close_issue") and issue_id:
        try:
            await github.close_issue(issue_id)
        except Exception as exc:
            logger.warning("monitor_performer.assessment_close_failed", card_id=card_id, error=str(exc))
        # Closing the issue does not move the project item: without this the
        # next poll still sees the card in TODO and admits it again.
        if board_provider is not None:
            try:
                await move_card_or_warn(board_provider, card_id, "DONE")
            except Exception as exc:
                logger.warning("monitor_performer.assessment_done_move_failed", card_id=card_id, error=str(exc))

    if board_provider is not None and verdict == "needs_split":
        try:
            await move_card_or_warn(board_provider, card_id, "BACKLOG")
        except Exception as exc:
            logger.warning("monitor_performer.assessment_backlog_move_failed", card_id=card_id, error=str(exc))

    lifecycle_seq = state.get("lifecycle_sequence") or ["implementing"]
    state["phase"] = "idle"
    _retire_active_session(state, trigger="assessment_declined")
    state["agent_dispatch"] = {}
    state["agent_dispatch_at"] = None
    state["relay_feedback"] = []
    state["observer_correction"] = None
    state["pending_reviews"] = []
    state["open_questions"] = []
    state["performer_stage"] = lifecycle_seq[0] if lifecycle_seq else "implementing"
    state["system_error_count"] = 0
    state["system_error_reason"] = None
    return state


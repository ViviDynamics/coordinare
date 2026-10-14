"""Activity-feed attribution and recording for the monitor (435, 138)."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

from coordinare.services.workflow_step import is_step_event

if TYPE_CHECKING:
    from coordinare.graph.state import CoordinareState


# 138: backend event type → activity type. `output` is the backend's fallback
# for an unrecognised line, so it reads as plain progress in the feed.
_ACTIVITY_TYPE_BY_EVENT = {
    "progress": "progress",
    "tool_use": "tool_use",
    "thinking": "thinking",
    "cost": "cost",
    "error": "error",
    "output": "progress",
    "completed": "completed",
    "completion": "completed",
    "blocked": "blocked",
    "failed": "error",
    "failure": "error",
}


def _activity_attribution(state: CoordinareState, card_id: str, stage: str) -> dict[str, Any]:
    """138: the four attribution fields every feed entry carries (FR-005).

    ``card_id``/``stage`` are locals at the push sites; title and number are
    not — they come off ``current_card``.
    """
    card = state.get("current_card") or {}
    return {
        "card_id": card_id,
        "card_number": card.get("issue_number"),
        "card_title": card.get("title", ""),
        "stage": stage,
        "session_id": str((state.get("agent_dispatch") or {}).get("session_id") or ""),
        "performer_id": str((state.get("agent_dispatch") or {}).get("performer_id") or stage),
    }


def _record_activity(
    state: CoordinareState,
    activity_type: str,
    text: str,
    *,
    card_id: str,
    stage: str,
) -> None:
    """138: push one feed entry. No-ops without a log (tests, no dashboard)."""
    log = state.get("activity_log")
    if log is None:
        return
    with contextlib.suppress(Exception):
        log.record(
            activity_type=activity_type,
            text=text,
            **_activity_attribution(state, card_id, stage),
        )


def _record_activity_batch(
    state: CoordinareState,
    events: list[Any],
    *,
    card_id: str,
    stage: str,
) -> None:
    """138 T023: push a poll's worth of backend events as one batch."""
    log = state.get("activity_log")
    if log is None:
        return
    attribution = _activity_attribution(state, card_id, stage)
    with contextlib.suppress(Exception):
        log.record_many([
            {
                **_feed_fields(ev),
                "text": ev.get("text") or ev.get("detail") or "",
                "is_delta": ev.get("is_delta") is True,
                "stream_id": ev.get("stream_id", ""),
                "source_event_id": str(ev.get("timestamp") or ""),
                **attribution,
            }
            for ev in events
            if isinstance(ev, dict)
        ])


def _tool_summary(ev: dict[str, Any]) -> str:
    """148: "Bash · uv run pytest -q" from the performer's tool name and argument.

    A backend that predates the ``tool``/``arg`` fields only has its text,
    which then stands in as the summary. The log redacts it on ingest.
    """
    name = str(ev.get("tool") or ev.get("text") or "tool")
    arg = str(ev.get("arg") or "")
    return f"{name} · {arg}" if arg else name


def _feed_fields(ev: dict[str, Any]) -> dict[str, str]:
    """148: the activity type, summary and status one backend event becomes.

    A tool call is ``tool_use`` with a summary. A tool result is ``tool_result``
    carrying its outcome. Narration stays ``progress``, a step boundary stays
    ``workflow_step``, and everything else keeps its 138 mapping.
    """
    if is_step_event(ev):
        # 343: a step boundary is its own kind of entry, not generic
        # progress. Classified from the same predicate the latch uses,
        # so the feed and the trail can never disagree about what
        # counts as a step.
        return {"activity_type": "workflow_step"}
    event_type = str(ev.get("type", ""))
    status = str(ev.get("status") or "")
    if event_type == "tool_use" and status:
        return {"activity_type": "tool_result", "summary": f"result {status}", "status": status}
    if event_type == "tool_use":
        return {"activity_type": "tool_use", "summary": _tool_summary(ev)}
    return {"activity_type": _ACTIVITY_TYPE_BY_EVENT.get(event_type, "progress")}


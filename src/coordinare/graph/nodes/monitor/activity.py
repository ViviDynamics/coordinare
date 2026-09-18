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
                # 343: a step boundary is its own kind of entry, not generic
                # progress. Classified from the same predicate the latch uses,
                # so the feed and the trail can never disagree about what
                # counts as a step.
                "activity_type": (
                    "workflow_step"
                    if is_step_event(ev)
                    else _ACTIVITY_TYPE_BY_EVENT.get(str(ev.get("type", "")), "progress")
                ),
                "text": ev.get("text") or ev.get("detail") or "",
                "is_delta": ev.get("is_delta") is True,
                "stream_id": ev.get("stream_id", ""),
                "source_event_id": str(ev.get("timestamp") or ""),
                **attribution,
            }
            for ev in events
            if isinstance(ev, dict)
        ])


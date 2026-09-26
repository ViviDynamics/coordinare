"""A budget for relays that produce nothing (#390).

Card #160 on 2026-09-12 ran the implementer twice, reached the identical
verdict both times, and was redispatched immediately after each. Roughly 25
minutes of wall clock and a 620-second baseline suite run per cycle, with no
ceiling in sight.

``bounce_counter`` did not engage. It is keyed by head SHA, and a run reporting
``partial_progress`` pushes nothing, so the branch head never moves and every
retry reads as the first attempt at that head.

Three paths in ``monitor_performer`` detect that a turn produced nothing and
answer by dispatching again -- partial_progress, blocked-with-no-commits, and
the review-stage zero-progress guardrail. Each relays a stronger directive,
which is reasonable once. None of them counted, which made all three unbounded.

The existing ceilings do not cover this, and it is worth being precise about
why. The session ceiling (#382) measures from the last thing the performer
PRODUCED, and these runs produce plenty: files changed, tool calls, a completed
turn. The observer's stall fold (430, successor of the #389 convergence ask)
only runs when that floor trips. Both
are aimed at a performer that has stopped working. This is the opposite case --
a performer working hard, finishing cleanly, and getting nowhere, in a loop
coordinare itself drives.

Consecutive, not lifetime: a run that actually commits has made progress and
earns a fresh budget. What must terminate is the *repetition*.
"""
from __future__ import annotations

from typing import Any

__all__ = [
    "MAX_NO_PROGRESS_RELAYS",
    "note_no_progress",
    "note_progress",
    "relays_spent",
    "should_block",
]

#: Consecutive empty relays allowed per stage before the card is blocked. One
#: retry with a stronger directive is worth having; the measured case repeated
#: the same verdict indefinitely, and a very large budget is indistinguishable
#: from having none.
MAX_NO_PROGRESS_RELAYS = 2

_KEY = "no_progress_relays"


def _counters(state: Any) -> dict[str, int]:
    """The per-stage counters, tolerating whatever the snapshot actually holds.

    This runs on the path that decides whether to dispatch, reading state that
    has round-tripped through a JSON snapshot and older schema versions. A
    monitor that raises here takes the supervisor down with it, so anything
    unreadable reads as "no relays spent" -- which costs at most one extra
    dispatch, where raising costs the daemon.
    """
    if not isinstance(state, dict):
        return {}
    raw = state.get(_KEY)
    return raw if isinstance(raw, dict) else {}


def relays_spent(state: Any, stage: str) -> int:
    """Consecutive empty relays recorded for *stage*."""
    value = _counters(state).get(stage)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return 0
    return value


def note_no_progress(state: Any, stage: str) -> int:
    """Record that *stage* relayed without producing anything. Returns the new count."""
    if not isinstance(state, dict):
        return 0
    counters = dict(_counters(state))
    counters[stage] = relays_spent(state, stage) + 1
    state[_KEY] = counters
    return counters[stage]


def note_progress(state: Any, stage: str) -> None:
    """Record that *stage* produced something; its budget starts fresh."""
    if not isinstance(state, dict):
        return
    counters = dict(_counters(state))
    if counters.pop(stage, None) is not None:
        state[_KEY] = counters


def should_block(state: Any, stage: str) -> bool:
    """Whether *stage* has spent its budget and the card must stop."""
    return relays_spent(state, stage) >= MAX_NO_PROGRESS_RELAYS

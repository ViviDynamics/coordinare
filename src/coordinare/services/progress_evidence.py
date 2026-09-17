"""What a performer has PRODUCED, as distinct from what it has emitted (#380).

Two wall-clock ceilings end a performer run, and both are blind stopwatches:
they ask how long it has been, never whether anything is happening. The obvious
repair -- reset the timer while events keep arriving -- is worse than the
disease, and two runs one card apart on the website symphony measured why.

    the stuck turn      events arriving continuously (164 progress deltas),
                        zero tool_use, files_changed=0, ~2M tokens, 45 minutes,
                        ended only by the clock
    a healthy run       ZERO events for 14 minutes -- a whole-suite run plus two
                        model calls, container CPU 0.67% -- then 49 tool_use

A timer that resets while events arrive never fires on the first. A timer that
fires when events stop kills the second. The same field, read the same way,
gets both cases wrong, and they are one card apart on the same board.

So the floor is what was produced. The separation is exact on the measured
pair: zero tool_use against 49. A performer that is thinking emits `progress`
and `thinking`; one that is working runs commands and finishes steps.

This module is deliberately mechanical and has no model call in it. The
gateway being wedged is itself a cause of stalled performers, so a stuck
detector that needs the gateway fails at exactly the moment it is most needed.
The model's judgement layers on top of this, and this keeps working without it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import datetime

__all__ = [
    "PRODUCING_EVENT_TYPES",
    "ProductionEvidence",
    "StallVerdict",
    "evaluate_stall",
    "production_fingerprint",
    "read_evidence",
]


#: Event types that mean the performer changed something outside its own head.
#: From coordinare's own event vocabulary (``_ACTIVITY_TYPE_BY_EVENT``), not from
#: any language or tool -- this is protocol, not stack knowledge.
#:
#: `progress`, `thinking`, `cost` and `output` are deliberately absent. They are
#: the model narrating, and the stuck turn produced 164 of them.
#: Running a command. The single clearest "it acted" signal in the measured
#: pair: 0 in the stuck turn, 49 in the healthy one.
COMMAND_EVENT_TYPES = frozenset({"tool_use"})

#: Finishing a step. Production without a command of its own.
COMPLETION_EVENT_TYPES = frozenset({"completed", "completion"})

PRODUCING_EVENT_TYPES = COMMAND_EVENT_TYPES | COMPLETION_EVENT_TYPES


@dataclass(frozen=True)
class ProductionEvidence:
    """What the accumulated event stream shows the performer has done."""

    tool_uses: int = 0
    completions: int = 0
    talk_events: int = 0
    total_events: int = 0

    @property
    def produced_anything(self) -> bool:
        return bool(self.tool_uses or self.completions)


def _event_type(event: Any) -> str:
    """The type of one event, or "" for anything unreadable.

    Events arrive off the wire from a backend coordinare does not control. A
    monitor that raises on a malformed one takes the supervisor down with it,
    so every shape that is not a dict with a string type reads as untyped --
    and untyped is never counted as production.
    """
    if not isinstance(event, dict):
        return ""
    kind = event.get("type")
    return kind if isinstance(kind, str) else ""


def read_evidence(events: Iterable[Any] | None) -> ProductionEvidence:
    """Read an accumulated performer event stream."""
    tool_uses = completions = talk = total = 0
    for event in events or ():
        total += 1
        kind = _event_type(event)
        if kind in COMMAND_EVENT_TYPES:
            tool_uses += 1
        elif kind in COMPLETION_EVENT_TYPES:
            completions += 1
        else:
            # Unknown types land here on purpose. Treating an unrecognised type
            # as production would let a future backend's chatter read as work,
            # which is the exact failure this module exists to prevent.
            talk += 1
    return ProductionEvidence(
        tool_uses=tool_uses, completions=completions, talk_events=talk, total_events=total,
    )


def production_fingerprint(events: Iterable[Any] | None) -> tuple[int, int]:
    """A value that changes only when something new has been produced.

    Compared across polls: unchanged means the performer has emitted, thought
    and spent tokens, but not acted. Counts rather than a hash, because the
    stream is append-mostly and a count cannot collide the way a digest of a
    truncated buffer can.
    """
    evidence = read_evidence(events)
    return (evidence.tool_uses, evidence.completions)


@dataclass(frozen=True)
class StallVerdict:
    """Whether the run has gone too long without producing anything."""

    expired: bool
    elapsed_s: float
    #: Which instant the clock was measured from: "production" once the
    #: performer has done something, "dispatch" until then, "absolute" when the
    #: backstop fired on a producing-but-looping run, "none" when there is
    #: nothing to measure.
    anchor: str


def evaluate_stall(
    *,
    now: datetime,
    dispatch_at: datetime | None,
    last_production_at: datetime | None,
    timeout_secs: float,
    absolute_timeout_secs: float | None = None,
) -> StallVerdict:
    """The ceiling, measured from the last thing the performer actually did.

    The ceiling exists to stop infinite loops, which is a real need. Anchoring
    it on dispatch makes it a blind stopwatch: it kills work that is
    progressing slowly, and grants a full budget to work that is going
    nowhere. Anchored on production it does neither -- a performer that keeps
    running commands keeps its budget, and one that has produced nothing since
    dispatch is measured from dispatch exactly as before.

    A performer with nothing to measure from is never killed on that basis:
    an absent timestamp is missing information, not evidence of a stall.
    """
    if timeout_secs <= 0:
        return StallVerdict(expired=False, elapsed_s=0.0, anchor="none")

    # The backstop. Production resetting the clock is right for a performer
    # that is working, but a performer LOOPING is also producing: every retry
    # of a failing command is a real tool_use, so the stall clock resets
    # forever and the run never ends. Anchoring only on production removed the
    # supervisor's last absolute bound, which is the one thing a ceiling must
    # never lose. Wider than the stall ceiling, because it has to tolerate
    # legitimate long work -- but never absent.
    if absolute_timeout_secs and absolute_timeout_secs > 0 and dispatch_at is not None:
        since_dispatch = (now - dispatch_at).total_seconds()
        if since_dispatch > absolute_timeout_secs:
            return StallVerdict(expired=True, elapsed_s=since_dispatch, anchor="absolute")

    # A production stamp from before this run began cannot belong to it. The
    # clock is per-performer-run and is cleared when a run ends, but relying on
    # every future caller to remember that is how the first cut of this shipped
    # a stage that inherited the previous stage's clock and died on its first
    # poll. The invariant is cheap to enforce here and holds however the state
    # was assembled.
    if last_production_at and dispatch_at and last_production_at < dispatch_at:
        last_production_at = None

    anchor_at = last_production_at or dispatch_at
    if anchor_at is None:
        return StallVerdict(expired=False, elapsed_s=0.0, anchor="none")
    elapsed = (now - anchor_at).total_seconds()
    return StallVerdict(
        expired=elapsed > timeout_secs,
        elapsed_s=elapsed,
        anchor="production" if last_production_at else "dispatch",
    )


def production_advanced(
    new: tuple[int, int], old: tuple[int, int] | None,
) -> bool:
    """Whether *new* shows strictly more produced than *old*.

    Strictly more, not merely different, for two reasons that both bit the
    first cut of this:

    * ``old`` is None before the first poll, and a run that has produced
      nothing fingerprints as ``(0, 0)``. "Different from None" made every
      first poll look like production, which reset the clock for exactly the
      stuck performer the ceiling is meant to catch.
    * the event buffer is capped, so a long run's early ``tool_use`` entries
      roll off the back and the counts go DOWN. A decrease means events were
      forgotten, never that work was done.
    """
    previous = old or (0, 0)
    return any(n > p for n, p in zip(new, previous, strict=False))

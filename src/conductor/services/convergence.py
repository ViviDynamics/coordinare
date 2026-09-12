"""Asking whether a stalled performer is getting anywhere (#380).

The mechanical floor decides WHEN to look: ``progress_evidence.evaluate_stall``
measures from the last thing the performer produced rather than from dispatch,
so a run that keeps acting keeps its budget and one that only talks does not.
What the floor cannot do is tell a turn that is genuinely stuck from one that is
waiting on something slow and legitimate. Both look identical to a clock.

Measured on the website symphony, one card apart:

    the stuck turn   164 progress deltas, zero commands, zero files, 45 minutes
    a healthy run    no events at all for 14 minutes -- a whole-suite run plus
                     two model calls -- then 49 commands in the same window

A person watching either stream would have called it correctly in seconds, from
the evidence rather than the clock. That is a judgement, and per #364 a
judgement is the model's.

**This can only ever grant a reprieve.** It cannot cause a kill that would not
otherwise have happened, and when the gateway is unreachable the floor decides
exactly as it does without this module. The direction is deliberate: the gateway
being wedged is itself a cause of stalled performers, so a judge that could kill
would fail hardest at the moment it is least trustworthy.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = ["MAX_REPRIEVES", "ConvergenceVerdict", "build_question", "parse_verdict", "persona"]


#: How many times one performer run may be granted more time. An unbounded
#: reprieve is no ceiling at all, which is the failure this issue exists to fix.
MAX_REPRIEVES = 1

#: Caps on what goes to the gateway. The event stream is unbounded and arrives
#: from a backend coordinare does not control.
_MAX_SNIPPETS = 6
_MAX_SNIPPET_CHARS = 300


@dataclass(frozen=True)
class ConvergenceVerdict:
    """Whether the run looks like it is getting somewhere."""

    converging: bool
    reason: str = ""


def persona() -> str:
    """Judge progress from evidence. Names no language, tool or runner."""
    return (
        "You are supervising an autonomous agent working on a software task. "
        "It has produced nothing for a while and you must decide whether to let "
        "it continue.\n\n"
        "Produced means it changed the world: it ran a command, or finished a "
        "step. Emitting text is not producing. An agent can narrate "
        "continuously while accomplishing nothing, and it can also go quiet for "
        "a long time while waiting on something slow and entirely legitimate.\n\n"
        "Say it is converging when the evidence suggests real work is in "
        "progress or about to land -- for example it has been running commands "
        "steadily and one may simply be slow.\n\n"
        "Say it is NOT converging when the evidence suggests it is going "
        "nowhere: nothing produced at all, the same ground covered repeatedly, "
        "or a great deal of output with nothing to show for it. When you cannot "
        "tell, say it is not converging: the agent will be stopped and asked "
        "again with what it failed to do, which is cheaper than letting it run."
    )


def build_question(
    *,
    stage: str,
    elapsed_s: float,
    tool_uses: int,
    completions: int,
    total_events: int,
    recent_text: list[str] | None = None,
) -> str:
    """The evidence, as a person watching the stream would have seen it."""
    snippets = [str(t)[:_MAX_SNIPPET_CHARS] for t in (recent_text or [])[:_MAX_SNIPPETS]]
    tail = "\n".join(f"- {s}" for s in snippets) or "(no recent output)"
    return (
        f"The agent is at the '{stage}' step and has produced nothing for "
        f"{round(elapsed_s / 60)} minutes.\n\n"
        f"In its recent activity window:\n"
        f"- commands run: {tool_uses}\n"
        f"- steps completed: {completions}\n"
        f"- total events emitted: {total_events}\n\n"
        f"The most recent things it said:\n{tail}\n\n"
        "Is it converging? Answer as JSON with 'converging' (true or false) and "
        "'reason'."
    )


def parse_verdict(answer: Any) -> ConvergenceVerdict:
    """Read the backend's answer, failing closed.

    Anything that is not an explicit boolean ``true`` is "not converging". A
    truthy string is not a verdict, and an answer that cannot be read is not a
    reason to keep running a performer that has produced nothing.
    """
    data = answer.get("data") if isinstance(answer, dict) else None
    if not isinstance(data, dict):
        return ConvergenceVerdict(converging=False, reason="no readable answer")
    converging = data.get("converging")
    if converging is not True:
        reason = data.get("reason")
        return ConvergenceVerdict(
            converging=False,
            reason=str(reason)[:300] if isinstance(reason, str) else "not converging",
        )
    reason = data.get("reason")
    return ConvergenceVerdict(
        converging=True, reason=str(reason)[:300] if isinstance(reason, str) else ""
    )

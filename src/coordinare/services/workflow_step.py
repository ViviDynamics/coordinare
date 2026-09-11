"""Latch the workflow step a performer is in, and when it entered it (spec 343).

Every spec-164 workflow emits a named, non-delta ``progress`` event at each step
boundary -- ``implementer.baseline``, ``architect.blueprint`` and so on. The
signal exists at the source; what was missing was anywhere for an operator to
read it. The job API's own ``progress`` field latches only two role prefixes,
coordinare never consumed it, and the activity feed evicts the markers during
exactly the long turns where the question matters.

So coordinare latches transitions at the moment it observes them, into per-card
session state that is persisted. That ordering is what makes the value durable:
it is captured before feed eviction can drop it, it survives a daemon restart,
and it survives the job being reaped, so a finished or wedged card can still be
asked "where did it get to?".

Deliberately *not* a list of every step the workflow will run: that tuple lives
in the performer package, and coordinare importing performer internals is the
mirror image of the #339 defect. The observed trail is built from what actually
happened. A self-describing step list is a separate slice over the wire
contract.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

#: A step marker is ``<role>.<step>``: lowercase, underscores, exactly one dot.
#: Anchored, so free-form model prose that merely contains a dot never latches.
_STEP_PATTERN = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")

#: Role prefixes that emit step markers. Matching the prefix as well as the
#: shape keeps sentence fragments and file names ("main.py", "spec.md") from
#: being mistaken for steps -- the shape alone is not selective enough.
KNOWN_STEP_PREFIXES: frozenset[str] = frozenset({
    "implementer",
    "architect",
    "assessor",
    "qa",
    "reviewer",
    "security",
    "documenter",
    "closer",
    "advocate",
    "curator",
    "env_bootstrap",
})

#: How many observed transitions a session keeps. The longest declared workflow
#: is the implementer's, whose milestone steps repeat per milestone, so this is
#: sized for a long multi-milestone run rather than the static step count.
MAX_STEP_TRAIL = 40


def is_step_event(event: Any) -> bool:
    """Whether *event* is a workflow step-boundary marker.

    Requires a non-delta ``progress`` event whose text is ``<known_role>.<step>``.
    Streamed model output is ``is_delta``-true and is never a boundary, so the
    delta check alone excludes the overwhelming majority of traffic cheaply.
    """
    if not isinstance(event, Mapping):
        return False
    if event.get("is_delta"):
        return False
    if event.get("type") != "progress":
        return False
    text = event.get("text")
    if not isinstance(text, str):
        return False
    text = text.strip()
    if not _STEP_PATTERN.match(text):
        return False
    return text.split(".", 1)[0] in KNOWN_STEP_PREFIXES


def _event_time(event: Mapping, *, observed_at: datetime) -> datetime:
    """The step's entry time: the performer's stamp, else when we saw it.

    Both clocks are host-derived under docker and k8s, and the error when
    falling back is bounded by one poll interval -- acceptable for an elapsed
    display, and honest in the direction that matters (a fallback can only
    make a step look *younger* than it is, never staler).
    """
    raw = event.get("timestamp")
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=UTC)
    if isinstance(raw, str) and raw:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return observed_at
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return observed_at


def latch_workflow_step(
    state: dict[str, Any],
    events: list[Any],
    *,
    observed_at: datetime | None = None,
) -> bool:
    """Update the session's workflow step from *events*. Returns True if it moved.

    ``events`` is the backend's reported list, which is cumulative -- so this
    re-reads markers it has already seen. That is why the write is guarded on an
    actual change: re-latching the same step must not reset its elapsed timer,
    which would make a wedged step look permanently fresh and defeat the whole
    point of showing elapsed time.
    """
    observed_at = observed_at or datetime.now(UTC)
    steps = [e for e in events if is_step_event(e)]
    if not steps:
        return False

    latest = steps[-1]
    name = str(latest.get("text", "")).strip()
    current = state.get("workflow_step")
    if current == name:
        return False

    entered_at = _event_time(latest, observed_at=observed_at)
    state["workflow_step"] = name
    state["workflow_step_entered_at"] = entered_at

    trail = list(state.get("workflow_step_trail") or [])
    # The trail is the observed history, so a step legitimately recurs (the
    # implementer runs milestone steps once per milestone). Append rather than
    # deduplicate; only collapse an immediate repeat, which would be the same
    # entry twice.
    if not trail or trail[-1].get("step") != name:
        trail.append({"step": name, "entered_at": entered_at.isoformat()})
    state["workflow_step_trail"] = trail[-MAX_STEP_TRAIL:]

    logger.info(
        "workflow_step.entered",
        workflow_step=name,
        previous_step=current,
        entered_at=entered_at.isoformat(),
    )
    return True

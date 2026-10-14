"""Why a card that needs a person is stopped, named by its class (#142).

The Blocked column holds three kinds of hold. A dependency wait and a capacity
wait are named by ``todo_waits`` and ``session_skip_reasons`` (#149). This
module names the third kind, a stop that needs a person, from the daemon's own
markers. It returns one code from a fixed set, never the text a performer wrote:
the text of an open question already reaches the snapshot as ``open_questions``
(redacted there), so nothing new is added to the snapshot here.

A card with no known class returns ``None``, and the dashboard says "Needs human"
without inventing a reason.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from coordinare.services.assessor_failure import classify_assessor_failure, is_format_failure
from coordinare.services.closed_pr import is_closed_pr_block

if TYPE_CHECKING:
    from collections.abc import Mapping

CAUSE_CLOSED_PR = "closed_pr"
CAUSE_ENV_BLOCKED = "env_blocked"
CAUSE_OUTPUT_TRUNCATED = "output_truncated"
CAUSE_FORMAT_EXHAUSTED = "format_exhausted"
CAUSE_SYSTEM_ERROR = "system_error"

#: The fixed reason the token-cap stop records (monitor/body.py). Its own cause
#: code, so no other text that happens to say "truncated" can be mistaken for it.
OUTPUT_CAP_REASON = "The performer hit its output token cap."

#: Markers a truncated model answer carries explicitly. A bare word such as
#: "truncated" is not one of them: a log or a proxy can say it about anything.
_EXPLICIT_TRUNCATION_MARKERS = ("finish_reason=length", "finish reason: length")

#: The phases a stopped card's session holds. A session in any other phase is
#: working, so no stop is named for it, unless its reason is a closed PR.
STOPPED_PHASES = frozenset({"blocked", "idle", "system_error"})


def block_cause(session: Mapping[str, Any]) -> str | None:
    """The class of a stop that needs a person, or ``None`` when it is not known.

    A closed PR is a durable operator park, so it is named whatever the phase:
    monitor_pr parks one in monitoring_performer while a writer is still owned.
    Every other class needs a stopped phase.
    """
    reason = session.get("system_error_reason")
    if is_closed_pr_block(reason):
        return CAUSE_CLOSED_PR
    if str(session.get("phase") or "") not in STOPPED_PHASES:
        return None
    if session.get("env_blocked"):
        return CAUSE_ENV_BLOCKED
    return _reason_class(reason, exhausted=bool(session.get("system_error_notified")))


def _reason_class(reason: object, *, exhausted: bool) -> str | None:
    """The class of a recorded error reason, or ``None`` when there is none.

    A format failure is "retries exhausted" only once the exhaustion path has
    run (``system_error_notified``). Until then it is still being retried, and
    it is named as the performer's error.
    """
    if not isinstance(reason, str) or not reason.strip():
        return None
    if _is_truncation(reason):
        return CAUSE_OUTPUT_TRUNCATED
    if exhausted and is_format_failure(reason):
        return CAUSE_FORMAT_EXHAUSTED
    return CAUSE_SYSTEM_ERROR


def _is_truncation(reason: str) -> bool:
    """True only for an explicit truncation: the token-cap stop, a finish-reason marker, or a
    performer format failure whose structured part says the answer was cut short."""
    if reason == OUTPUT_CAP_REASON:
        return True
    lowered = reason.lower()
    if any(marker in lowered for marker in _EXPLICIT_TRUNCATION_MARKERS):
        return True
    return is_format_failure(reason) and classify_assessor_failure(reason) == "truncated"

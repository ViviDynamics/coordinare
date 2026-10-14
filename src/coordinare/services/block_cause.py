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

#: The phases a stopped card's session holds. A session in any other phase is
#: working, so no stop is named for it.
STOPPED_PHASES = frozenset({"blocked", "idle", "system_error"})


def block_cause(session: Mapping[str, Any]) -> str | None:
    """The class of a stop that needs a person, or ``None`` when it is not known.

    The checks run from the most specific marker to the least. A closed PR is a
    durable operator park, so it wins over a later retry reason.
    """
    if str(session.get("phase") or "") not in STOPPED_PHASES:
        return None
    reason = session.get("system_error_reason")
    if is_closed_pr_block(reason):
        return CAUSE_CLOSED_PR
    if session.get("env_blocked"):
        return CAUSE_ENV_BLOCKED
    return _reason_class(reason)


def _reason_class(reason: object) -> str | None:
    """The class of a recorded performer or conductor error reason, or ``None`` when there is none."""
    if not isinstance(reason, str) or not reason.strip():
        return None
    if classify_assessor_failure(reason) == "truncated":
        return CAUSE_OUTPUT_TRUNCATED
    if is_format_failure(reason):
        return CAUSE_FORMAT_EXHAUSTED
    return CAUSE_SYSTEM_ERROR
